"""Evaluate every mode on the same questions. Run with: uv run python -m src.eval [--set dev|golden|followup|all]

Modes compared:
  baseline       a naive one-line prompt "Convert this question to SQL: {question}"; no schema, no repair
  chat    v0.1: full briefing, one model call, one repair on error
  agent          v0.2: Strands agent with schema tools; follow-ups see the previous result's rows
  agent-no-rows  agent mode without the previous result's rows (follow-up comparison)

The followup set is multi-turn: each conversation runs in one session, with the model's own earlier
turns as history (as in the CLI), and every turn is scored. The baseline has no history, so it skips it.

A prediction is correct when its rows match the gold query's rows (execution accuracy, as in BIRD).
Writes eval/report_<set>.json; with the dev set, also eval/dev_answers.json.
"""

from __future__ import annotations

import argparse
import os
import re
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Any

from dotenv import find_dotenv, load_dotenv
from pydantic import BaseModel, ConfigDict, TypeAdapter
from sqlalchemy.engine import Engine

from src import db, fireworks
from src.chat import (
    DEFAULT_MODEL,
    MODES,
    Config,
    ReplyError,
    Turn,
    guard_sql,
    make_session,
)
from src.context import Table, load_tables
from src.fireworks import Message
from src.utils import ROOT

QUESTION_SETS = {"dev": ROOT / "data" / "dev_questions_with_answers.json", "golden": ROOT / "eval" / "golden_questions.json"}
FOLLOWUP_SET = ROOT / "eval" / "followup_questions.json"
MODE_SETTINGS: dict[str, dict[str, Any]] = {
    "baseline": {"mode": "chat"},
    "chat": {"mode": "chat"},
    "agent": {"mode": "agent"},
    "agent-no-rows": {"mode": "agent", "history_rows": 0},
}
FOLLOWUP_MODES = ["chat", "agent", "agent-no-rows"]
BASELINE_PROMPT = "Convert this question to SQL:\n{question}"
ORDER_BY = re.compile(r"\border\s+by\b", re.IGNORECASE)
PARENS = re.compile(r"\([^()]*\)")
QUERIES_PER_DAY = 30_000


class Price(BaseModel):
    """US dollars per 1M tokens."""

    model_config = ConfigDict(frozen=True)
    input: float
    cached: float
    output: float


PRICES = {  # Fireworks serverless list prices, checked 2026-11-25
    "accounts/fireworks/models/glm-5p3-flash": Price(input=0.15, cached=0.03, output=0.50),
    "accounts/fireworks/models/kimi-k3": Price(input=3.00, cached=0.30, output=15.00),
}


class Question(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    id: str
    question: str
    tier: int
    gold_sql: str
    expected_result: list[dict[str, Any]] = []
    gold_answer: str = ""
    source: str = "dev"


class FollowupTurn(BaseModel):
    model_config = ConfigDict(frozen=True)
    question: str
    gold_sql: str


class Conversation(BaseModel):
    """A multi-turn conversation; later turns refer to earlier questions or their results."""

    model_config = ConfigDict(frozen=True)
    id: str
    tier: int
    turns: tuple[FollowupTurn, ...]


class Record(BaseModel):
    model_config = ConfigDict(frozen=True)
    mode: str
    id: str
    tier: int
    turn: int = 0  # position in a follow-up conversation; 0 for a single question
    sql: str
    columns: list[str]
    answer: str
    error: str | None
    correct: bool
    attempts: int
    tool_calls: int
    prompt_tokens: int
    cached_tokens: int
    completion_tokens: int
    cost_usd: float
    llm_s: float
    sql_s: float
    total_s: float


class Summary(BaseModel):
    model_config = ConfigDict(frozen=True)
    correct: int
    total: int
    accuracy: float
    by_tier: dict[str, str]
    errors: int
    p50_s: float
    p95_s: float
    max_s: float
    avg_prompt_tokens: int
    avg_cached_tokens: int
    avg_completion_tokens: int
    avg_tool_calls: float
    cost_per_query_usd: float
    cost_per_day_usd: float
    by_turn: dict[str, str] = {}  # follow-up set: correct/total at each turn position
    conversations: str = ""  # follow-up set: conversations with every turn correct


class Report(BaseModel):
    model_config = ConfigDict(frozen=True)
    model: str
    question_set: str
    run_date: str
    summary: dict[str, Summary]
    records: list[Record]


class DevAnswer(BaseModel):
    model_config = ConfigDict(frozen=True)
    sql: str
    answer: str


def main() -> None:
    load_dotenv(find_dotenv(usecwd=True))
    args = parse_args()
    if args.rescore:
        return rescore(args.set)
    api_key = os.environ.get("FIREWORKS_API_KEY") or sys.exit("FIREWORKS_API_KEY is not set.")
    followup = args.set == "followup"
    modes = args.modes or (FOLLOWUP_MODES if followup else ["baseline", *MODES])
    engine = db.connect()  # the bundled SQLite benchmark; gold SQL is written for SQLite
    tables = load_tables(engine)
    if followup:
        items: list = load_conversations()[: args.limit or None]
        gold = {f"{c.id}.{i}": gold_rows(engine, t.gold_sql) for c in items for i, t in enumerate(c.turns, 1)}
    else:
        items = load_questions(args.set)[: args.limit or None]
        gold = {q.id: check_gold(engine, q) for q in items}

    records: list[Record] = []
    for mode in modes:
        cfg = Config(model=args.model, **MODE_SETTINGS[mode])
        if followup:
            run = lambda c, m=mode, cf=cfg: run_conversation(m, c, cf, tables, api_key, gold)
        else:
            run = lambda q, m=mode, cf=cfg: [run_one(m, q, cf, tables, api_key, gold[q.id])]
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            done = [r for batch in pool.map(run, items) for r in batch]
        records += done
        s = summarize(done, args.model)
        extra = f"  by turn {s.by_turn}  conversations {s.conversations}" if followup else ""
        print(f"{mode:13} {s.correct}/{s.total} ({s.accuracy:.0%})  P50 {s.p50_s:.2f}s  P95 {s.p95_s:.2f}s  "
              f"${s.cost_per_query_usd:.6f}/query  tools {s.avg_tool_calls:.1f}{extra}")

    out = ROOT / "eval" / f"report_{args.set}.json"
    if out.exists():  # rerunning some modes keeps the other modes' saved results
        kept = [r for r in Report.model_validate_json(out.read_text()).records if r.mode not in modes]
        records, modes = kept + records, list(dict.fromkeys([*(r.mode for r in kept), *modes]))
    report = Report(model=args.model, question_set=args.set, run_date=date.today().isoformat(),
                    summary={m: summarize([r for r in records if r.mode == m], args.model) for m in modes},
                    records=records)
    out.parent.mkdir(exist_ok=True)
    out.write_text(report.model_dump_json(indent=1))
    if args.set == "dev" and args.answers_from in modes:
        write_dev_answers([r for r in records if r.mode == args.answers_from])
    print(f"Wrote {out.relative_to(ROOT)}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="eval", description="Compare modes by execution accuracy, latency and cost.")
    p.add_argument("--set", choices=[*QUESTION_SETS, "followup", "all"], default="dev")
    p.add_argument("--modes", type=lambda s: s.split(","), default=None,
                   help=f"comma-separated, from {', '.join(MODE_SETTINGS)}; default: all that apply to the set")
    p.add_argument("--model", default=os.environ.get("FIREWORKS_MODEL", DEFAULT_MODEL))
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--limit", type=int, default=0, help="only the first N questions")
    p.add_argument("--answers-from", default="chat", help="mode whose dev answers go to eval/dev_answers.json")
    p.add_argument("--rescore", action="store_true", help="re-grade the saved report's SQL locally; no model calls")
    return p.parse_args(argv)


def load_questions(name: str) -> list[Question]:
    names = list(QUESTION_SETS) if name == "all" else [name]
    adapter = TypeAdapter(list[Question])
    return [q for n in names for q in adapter.validate_json(QUESTION_SETS[n].read_text())]


def rescore(name: str) -> None:
    """Re-run the SQL saved in eval/report_<set>.json and grade it again, e.g. after a grader fix. Free: no model calls."""
    path = ROOT / "eval" / f"report_{name}.json"
    report = Report.model_validate_json(path.read_text())
    if name == "followup":
        gold_sql = {f"{c.id}.{i}": t.gold_sql for c in load_conversations() for i, t in enumerate(c.turns, 1)}
    else:
        gold_sql = {q.id: q.gold_sql for q in load_questions(name)}
    engine = db.connect()
    gold = {qid: gold_rows(engine, sql) for qid, sql in gold_sql.items()}
    records = [r.model_copy(update={"correct": regrade(engine, r, gold_sql[r.id], gold[r.id])}) for r in report.records]
    modes = list(report.summary)
    changed = sum(a.correct != b.correct for a, b in zip(report.records, records))
    report = report.model_copy(update={
        "records": records,
        "summary": {m: summarize([r for r in records if r.mode == m], report.model) for m in modes},
    })
    path.write_text(report.model_dump_json(indent=1))
    for m, s in report.summary.items():
        print(f"{m:13} {s.correct}/{s.total} ({s.accuracy:.0%})  by tier {s.by_tier}" + (f"  by turn {s.by_turn}  conversations {s.conversations}" if s.by_turn else ""))
    print(f"Rescored {path.relative_to(ROOT)}: {changed} records changed")


def regrade(engine: Engine, r: Record, gold_sql: str, gold: list[tuple]) -> bool:
    if r.error is not None or not r.sql:
        return False
    try:
        _, rows, _ = db.query(engine, guard_sql(r.sql), timeout_s=60, max_rows=Config().max_rows)
    except (ReplyError, db.QueryError):
        return False
    return rows_match(rows, gold, ordered=sorts_result(gold_sql))


def load_conversations() -> list[Conversation]:
    return TypeAdapter(list[Conversation]).validate_json(FOLLOWUP_SET.read_text())


def gold_rows(engine: Engine, sql: str) -> list[tuple]:
    _, rows, _ = db.query(engine, sql, timeout_s=60, max_rows=100_000)
    return list(rows)


def check_gold(engine: Engine, q: Question) -> list[tuple]:
    """Run the gold SQL; when the question carries an expected result, confirm the gold SQL reproduces it."""
    _, rows, _ = db.query(engine, q.gold_sql, timeout_s=60, max_rows=100_000)
    if q.expected_result and not rows_match(rows, [tuple(r.values()) for r in q.expected_result], ordered=False):
        raise SystemExit(f"Gold SQL for {q.id} does not reproduce expected_result")
    return list(rows)


def run_one(mode: str, q: Question, cfg: Config, tables: tuple[Table, ...], api_key: str, gold: list[tuple]) -> Record:
    """One question in a fresh session."""
    engine = db.connect()
    if mode == "baseline":
        turn = run_baseline(q.question, engine, cfg, api_key)
    else:
        turn = make_session(cfg, engine, tables, api_key)(q.question, ())
    return to_record(mode, q.id, q.tier, 0, turn, q.gold_sql, gold, cfg.model)


def run_conversation(mode: str, conv: Conversation, cfg: Config, tables: tuple[Table, ...], api_key: str,
                     gold: dict[str, list[tuple]]) -> list[Record]:
    """Every turn of one conversation in one session, with the model's own earlier turns as history."""
    records: list[Record] = []
    history: tuple[Turn, ...] = ()
    engine = db.connect()
    ask = make_session(cfg, engine, tables, api_key)
    for n, t in enumerate(conv.turns, 1):
        turn = ask(t.question, history)
        history = (*history, turn)
        records.append(to_record(mode, f"{conv.id}.{n}", conv.tier, n, turn, t.gold_sql, gold[f"{conv.id}.{n}"], cfg.model))
    return records


def to_record(mode: str, qid: str, tier: int, n: int, turn: Turn, gold_sql: str, gold: list[tuple], model: str) -> Record:
    ordered = sorts_result(gold_sql)
    return Record(
        mode=mode, id=qid, tier=tier, turn=n, sql=turn.sql, columns=list(turn.columns),
        answer=summarize_rows(turn), error=turn.error,
        correct=turn.ok and rows_match(turn.rows, gold, ordered=ordered),
        attempts=turn.attempts, tool_calls=turn.tool_calls,
        prompt_tokens=turn.usage.prompt_tokens, cached_tokens=turn.usage.cached_tokens,
        completion_tokens=turn.usage.completion_tokens, cost_usd=cost(turn, model),
        llm_s=round(turn.llm_s, 3), sql_s=round(turn.sql_s, 3), total_s=round(turn.total_s, 3),
    )


def sorts_result(sql: str) -> bool:
    """True when the query's final result is ordered: an ORDER BY outside every subquery.
    An ORDER BY inside a subquery (e.g. to pick a top 5) says nothing about the order of the rows returned."""
    while PARENS.search(sql):
        sql = PARENS.sub("", sql)
    return bool(ORDER_BY.search(sql))


def run_baseline(question: str, engine: Engine, cfg: Config, api_key: str) -> Turn:
    """A naive baseline: one bare prompt, take the SQL as-is, no schema, no retry."""
    start = time.perf_counter()
    c = fireworks.chat([Message(role="user", content=BASELINE_PROMPT.format(question=question))],
                       model=cfg.model, api_key=api_key, reasoning_effort=cfg.reasoning_effort)
    sql, t = extract_sql(c.content), time.perf_counter()
    try:
        columns, rows, truncated = db.query(engine, guard_sql(sql), timeout_s=cfg.sql_timeout_s, max_rows=cfg.max_rows)
    except (ReplyError, db.QueryError) as exc:
        return Turn(question=question, sql=sql, error=str(exc), usage=c.usage, llm_s=c.latency_s,
                    sql_s=time.perf_counter() - t, total_s=time.perf_counter() - start)
    return Turn(question=question, sql=sql, columns=columns, rows=rows, truncated=truncated, usage=c.usage,
                llm_s=c.latency_s, sql_s=time.perf_counter() - t, total_s=time.perf_counter() - start)


def extract_sql(text: str) -> str:
    """Pull SQL out of a free-text reply: a fenced block if present, else from the first SELECT/WITH."""
    fence = re.search(r"```(?:sql)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        return fence.group(1).strip()
    start = re.search(r"\b(select|with)\b", text, re.IGNORECASE)
    return text[start.start():].split("\n\n")[0].strip() if start else text.strip()


def normalize(value: Any) -> Any:
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


def cost(turn: Turn, model: str) -> float:
    price = PRICES.get(model)
    if price is None:
        return 0.0
    u = turn.usage
    uncached = max(u.prompt_tokens - u.cached_tokens, 0)
    return (uncached * price.input + u.cached_tokens * price.cached + u.completion_tokens * price.output) / 1e6


def summarize_rows(turn: Turn) -> str:
    if turn.error:
        return f"Error: {turn.error}"
    if not turn.rows:
        return "No rows returned."
    if len(turn.rows) == 1 and len(turn.rows[0]) == 1:
        return f"{turn.columns[0]} = {turn.rows[0][0]}"
    head = "; ".join(", ".join(f"{c}={v}" for c, v in zip(turn.columns, r)) for r in turn.rows[:5])
    return f"{len(turn.rows)} rows: {head}" + (" ..." if len(turn.rows) > 5 else "")


def summarize(recs: list[Record], model: str) -> Summary:
    lat = sorted(r.total_s for r in recs)
    tiers = sorted({r.tier for r in recs})
    per_query = statistics.mean(r.cost_usd for r in recs)
    correct = sum(r.correct for r in recs)
    return Summary(
        correct=correct, total=len(recs), accuracy=round(correct / len(recs), 4),
        by_tier={str(t): f"{sum(r.correct for r in recs if r.tier == t)}/{sum(r.tier == t for r in recs)}" for t in tiers},
        errors=sum(r.error is not None for r in recs),
        p50_s=round(statistics.median(lat), 2),
        p95_s=round(lat[min(len(lat) - 1, round(0.95 * (len(lat) - 1)))], 2),
        max_s=round(lat[-1], 2),
        avg_prompt_tokens=round(statistics.mean(r.prompt_tokens for r in recs)),
        avg_cached_tokens=round(statistics.mean(r.cached_tokens for r in recs)),
        avg_completion_tokens=round(statistics.mean(r.completion_tokens for r in recs)),
        avg_tool_calls=round(statistics.mean(r.tool_calls for r in recs), 2),
        cost_per_query_usd=round(per_query, 7),
        cost_per_day_usd=round(per_query * QUERIES_PER_DAY, 2),
        by_turn=_by_turn(recs),
        conversations=_conversations(recs),
    )


def _by_turn(recs: list[Record]) -> dict[str, str]:
    turns = sorted({r.turn for r in recs if r.turn})
    return {str(t): f"{sum(r.correct for r in recs if r.turn == t)}/{sum(r.turn == t for r in recs)}" for t in turns}


def _conversations(recs: list[Record]) -> str:
    ids = {r.id.split(".")[0] for r in recs if r.turn}
    if not ids:
        return ""
    full = sum(all(r.correct for r in recs if r.id.split(".")[0] == c) for c in ids)
    return f"{full}/{len(ids)}"


def write_dev_answers(recs: list[Record]) -> None:
    answers = {r.id: DevAnswer(sql=r.sql, answer=r.answer) for r in recs}
    (ROOT / "eval" / "dev_answers.json").write_text(TypeAdapter(dict[str, DevAnswer]).dump_json(answers, indent=2).decode() + "\n")


if __name__ == "__main__":
    main()
