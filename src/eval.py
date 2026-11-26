"""Evaluate the agent on the dev questions. Run with: uv run python -m src.eval

Compares three versions on the same questions, as described in docs/solution.md:
  baseline  the naive prompt "Convert this question to SQL: {question}"; no briefing, no repair
  grounded  our agent (briefing + JSON output), no repair
  repair    our agent with one error-feedback repair (the CLI's default)

A prediction is correct when its rows match the gold query's rows (execution accuracy, as in BIRD).
Writes eval/report.json (per-question records and a summary) and dev_answers.json.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import statistics
import sys
import time
from dataclasses import replace
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

from src import fireworks
from src.agent import DEFAULT_MODEL, Config, ReplyError, answer, connect_readonly, guard_sql, run_sql
from src.context import build_briefing

ROOT = Path(__file__).resolve().parent.parent
QUESTIONS = ROOT / "data" / "dev_questions_with_answers.json"
BASELINE_PROMPT = "Convert this question to SQL:\n{question}"
ORDER_BY = re.compile(r"\border\s+by\b", re.IGNORECASE)


def main() -> None:
    load_dotenv(find_dotenv(usecwd=True))
    api_key = os.environ.get("FIREWORKS_API_KEY") or sys.exit("FIREWORKS_API_KEY is not set.")
    model = os.environ.get("FIREWORKS_MODEL", DEFAULT_MODEL)
    questions = json.loads(QUESTIONS.read_text())
    conn = connect_readonly(str(ROOT / "data" / "financial.sqlite"))
    briefing = build_briefing(conn)
    gold = {q["id"]: check_gold(conn, q) for q in questions}

    cfg = Config(model=model)
    versions = {
        "baseline": lambda q: run_baseline(q, conn, cfg, api_key),
        "grounded": lambda q: run_agent(q, conn, briefing, replace(cfg, max_repairs=0), api_key),
        "repair": lambda q: run_agent(q, conn, briefing, cfg, api_key),
    }
    records = []
    for name, run in versions.items():
        for q in questions:
            rec = run(q)
            rec.update(version=name, id=q["id"], tier=q["tier"], correct=rec["error"] is None
                       and rows_match(rec.pop("rows"), gold[q["id"]], ordered=bool(ORDER_BY.search(q["gold_sql"]))))
            records.append(rec)
            print(f"{name:9} {q['id']} {'PASS' if rec['correct'] else 'FAIL'} {rec['total_s']:.2f}s"
                  + (f"  ({rec['error'][:80]})" if rec["error"] else ""))

    summary = {name: summarize([r for r in records if r["version"] == name]) for name in versions}
    out = ROOT / "eval"
    out.mkdir(exist_ok=True)
    (out / "report.json").write_text(json.dumps({"model": model, "summary": summary, "records": records}, indent=1))
    write_dev_answers([r for r in records if r["version"] == "repair"])
    print(json.dumps(summary, indent=1))


def check_gold(conn: sqlite3.Connection, q: dict) -> list[tuple]:
    """Run the gold SQL and confirm it reproduces the answer key's expected_result."""
    _, rows, _ = run_sql(conn, q["gold_sql"], timeout_s=60, max_rows=100_000)
    expected = [tuple(r.values()) for r in q["expected_result"]]
    if not rows_match(rows, expected, ordered=False):
        raise SystemExit(f"Gold SQL for {q['id']} does not reproduce expected_result")
    return list(rows)


def run_agent(q: dict, conn, briefing: str, cfg: Config, api_key: str) -> dict:
    turn = answer(q["question"], history=(), conn=conn, briefing=briefing, cfg=cfg, api_key=api_key)
    return record(turn.sql, list(turn.rows), turn.columns, turn.error, turn.attempts, turn.usage,
                  turn.llm_s, turn.sql_s, turn.total_s)


def run_baseline(q: dict, conn, cfg: Config, api_key: str) -> dict:
    """A naive baseline: one bare prompt, take the SQL as-is, no schema, no retry."""
    start = time.perf_counter()
    c = fireworks.chat([{"role": "user", "content": BASELINE_PROMPT.format(question=q["question"])}],
                       model=cfg.model, api_key=api_key, reasoning_effort=cfg.reasoning_effort)
    sql, rows, columns, error, sql_s = extract_sql(c.content), [], (), None, 0.0
    t = time.perf_counter()
    try:
        columns, rows, _ = run_sql(conn, guard_sql(sql), timeout_s=cfg.sql_timeout_s, max_rows=cfg.max_rows)
        rows = list(rows)
    except (ReplyError, sqlite3.Error) as exc:
        error = str(exc)
    sql_s = time.perf_counter() - t
    return record(sql, rows, columns, error, 1, c.usage, c.latency_s, sql_s, time.perf_counter() - start)


def record(sql, rows, columns, error, attempts, usage, llm_s, sql_s, total_s) -> dict:
    return {"sql": sql, "rows": rows, "columns": list(columns), "answer": summarize_rows(columns, rows, error),
            "error": error, "attempts": attempts, "prompt_tokens": usage.prompt_tokens,
            "cached_tokens": usage.cached_tokens, "completion_tokens": usage.completion_tokens,
            "llm_s": round(llm_s, 3), "sql_s": round(sql_s, 3), "total_s": round(total_s, 3)}


def extract_sql(text: str) -> str:
    """Pull SQL out of a free-text reply: a fenced block if present, else from the first SELECT/WITH."""
    fence = re.search(r"```(?:sql)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        return fence.group(1).strip()
    start = re.search(r"\b(select|with)\b", text, re.IGNORECASE)
    return text[start.start():].split("\n\n")[0].strip() if start else text.strip()


def normalize(value):
    if isinstance(value, float):
        return round(value, 2) if value != int(value) else int(value)
    if isinstance(value, str):
        return value.strip()
    return value


def rows_match(pred, gold, *, ordered: bool) -> bool:
    """Compare result sets ignoring column names. With ORDER BY in the gold SQL, the order must match too,
    except among tied rows."""
    p = [tuple(normalize(v) for v in r) for r in pred]
    g = [tuple(normalize(v) for v in r) for r in gold]
    if sorted(p, key=repr) != sorted(g, key=repr):
        return False
    if not ordered or p == g:
        return True
    # Tied rows have no defined order: pass if a column the gold result is sorted by lines up exactly.
    sort_cols = [c for c in range(len(g[0])) if _monotonic([r[c] for r in g])] if g else []
    return any([r[c] for r in p] == [r[c] for r in g] for c in sort_cols)


def _monotonic(values: list) -> bool:
    try:
        return len(set(values)) > 1 and (values == sorted(values) or values == sorted(values, reverse=True))
    except TypeError:
        return False


def summarize_rows(columns, rows, error) -> str:
    if error:
        return f"Error: {error}"
    if not rows:
        return "No rows returned."
    if len(rows) == 1 and len(rows[0]) == 1:
        return f"{columns[0]} = {rows[0][0]}"
    head = "; ".join(", ".join(f"{c}={v}" for c, v in zip(columns, r)) for r in rows[:5])
    return f"{len(rows)} rows: {head}" + (" ..." if len(rows) > 5 else "")


def summarize(recs: list[dict]) -> dict:
    lat = sorted(r["total_s"] for r in recs)
    by_tier = {t: [r for r in recs if r["tier"] == t] for t in sorted({r["tier"] for r in recs})}
    return {
        "correct": sum(r["correct"] for r in recs), "total": len(recs),
        "by_tier": {str(t): f"{sum(r['correct'] for r in rs)}/{len(rs)}" for t, rs in by_tier.items()},
        "sql_errors": sum(r["error"] is not None for r in recs),
        "repaired": sum(r["attempts"] > 1 and r["error"] is None for r in recs),
        "p50_s": round(statistics.median(lat), 2),
        "p95_s": round(lat[min(len(lat) - 1, round(0.95 * (len(lat) - 1)))], 2),
        "max_s": round(lat[-1], 2),
        "avg_prompt_tokens": round(statistics.mean(r["prompt_tokens"] for r in recs)),
        "avg_cached_tokens": round(statistics.mean(r["cached_tokens"] for r in recs)),
        "avg_completion_tokens": round(statistics.mean(r["completion_tokens"] for r in recs)),
    }


def write_dev_answers(recs: list[dict]) -> None:
    answers = {r["id"]: {"sql": r["sql"], "answer": r["answer"]} for r in recs}
    (ROOT / "dev_answers.json").write_text(json.dumps(answers, indent=2) + "\n")


if __name__ == "__main__":
    main()
