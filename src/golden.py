"""Generate the synthetic golden set: questions first, then execution-verified gold SQL.

Run with: uv run python -m src.golden [--target 200]

Phase 1 (questions): a strong teacher model reads the database briefing (schema, column meanings,
real coded values), reasons about what an analyst would ask, and writes questions by tier and theme.
It writes no SQL, so questions are not shaped by what is easy to query.

Phase 2 (gold SQL): for each question the teacher writes SQL independently, twice. A question is kept
only when the queries run, return 1-100 rows, and agree on the result (a third sample breaks a tie).
Disagreement usually means the question is ambiguous, so it is dropped.

Tiers come from the gold SQL, by a fixed rule (see `sql_tier`). Near-duplicates, and anything close to
the dev questions, are dropped. Writes eval/golden_questions.json in the same shape as the dev
questions. The given data/ folder is never modified.

Provenance of eval/golden_questions.json: generated on 2026-11-25 with kimi-k3
(accounts/fireworks/models/kimi-k3) as the teacher for both phases, for about $11.27 at list prices
(a 45-question trial at $1.31, then the full run at $9.96). 289 questions were drafted, 275 verified,
and 200 kept (tiers 75/65/60). 190 agreed on the first two SQL samples, 10 needed a third. Each
question's `source` field records the teacher and agreement. The default teacher is now glm-5p3-flash,
the cheapest option; estimate the cost before switching to a stronger one.
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from typing import Any

from dotenv import find_dotenv, load_dotenv
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from src import fireworks
from src.chat import ReplyError, SQLReply, connect_readonly, guard_sql, run_sql
from src.context import build_briefing
from src.eval import PRICES, Question, rows_match
from src.fireworks import Message, Usage
from src.utils import ROOT

TEACHER = "accounts/fireworks/models/glm-5p3-flash"  # see "Provenance" above: the current set came from kimi-k3
OUT = ROOT / "eval" / "golden_questions.json"
DEV = ROOT / "data" / "dev_questions_with_answers.json"
NEAR_DUPLICATE = 0.7  # token overlap (Jaccard) at or above which two questions count as the same
TIER_SHARE = {1: 0.4, 2: 0.3, 3: 0.3}  # same mix as the dev set
OVERSAMPLE = 1.6
BATCH = 15
MAX_ROWS = 100
THEMES = (
    "accounts and the districts/regions they are in",
    "clients: gender, age, where they live",
    "loans: amounts, durations, payments and repayment status",
    "credit cards and their types",
    "transactions: credits, withdrawals, operations, balances, dates",
    "standing (permanent) orders and their purposes",
    "district demographics (population, salary, unemployment, crime) linked to banking activity",
    "account ownership vs. disponents, and statement frequency",
)
TIERS = {
    1: "Tier 1, simple: one table, or a single filter plus a count/sum/average.",
    2: "Tier 2, one join: two tables joined, with a filter or a simple aggregate.",
    3: "Tier 3, complex: several joins plus grouping, ranking (top N), or comparison across groups.",
}

QUESTION_PROMPT = """\
You are building an evaluation set for a text-to-SQL assistant used by bank analysts.

First reason about the database below: what the entities are, how they relate, what the coded values \
mean, and what an analyst would realistically want to know. Then write {n} questions.

Requirements for every question:
- {tier}
- Theme: {theme}.
- Answerable from this database alone, with exactly one correct answer. Say what to return \
(a count, a total, a list of names...) and, when listing, the order and how many.
- Plain English, the way an analyst talks. Use real values from the data in plain words \
(e.g. "the Prague region", "weekly statements", "loans that are finished with no problems"), \
never column names or codes.
- No SQL, no hints about tables or columns.
- Vary the shape: counts, totals, averages, minimum/maximum, lists, comparisons between groups, \
date ranges, percentages. Vary the values you ask about. Do not repeat a template with a new value.

# Database
{briefing}
"""

SQL_PROMPT = """\
You are an expert SQLite analyst. Write ONE read-only query that answers the question exactly, \
using only the tables and columns described. Map the question's words to the stored codes listed \
under "all values". Dates are text 'YYYY-MM-DD'. Use COUNT(DISTINCT ...) when joins could double count. \
Return only the columns the question asks for. Reply as JSON: {{"reasoning": "...", "sql": "..."}}

# Database
{briefing}
"""


class DraftQuestion(BaseModel):
    model_config = ConfigDict(frozen=True)
    question: str
    tier: int = Field(ge=1, le=3)


class DraftBatch(BaseModel):
    questions: list[DraftQuestion]


class Verified(BaseModel):
    model_config = ConfigDict(frozen=True)
    draft: DraftQuestion
    sql: str
    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    agreement: str


class SqlSample(BaseModel):
    """One teacher-written query and what it returned."""

    model_config = ConfigDict(frozen=True)
    sql: str
    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]


def cost_usd(usage: Usage) -> float:
    """Teacher spend for a usage total, at list prices."""
    p, u = PRICES[TEACHER], usage
    return ((u.prompt_tokens - u.cached_tokens) * p.input + u.cached_tokens * p.cached + u.completion_tokens * p.output) / 1e6


def total(usages) -> Usage:
    return sum(usages, Usage())


def main() -> None:
    load_dotenv(find_dotenv(usecwd=True))
    args = parse_args()
    api_key = os.environ.get("FIREWORKS_API_KEY") or sys.exit("FIREWORKS_API_KEY is not set.")
    with closing(connect_readonly()) as conn:
        briefing = build_briefing(conn)

    drafts, usage = generate_questions(briefing, args.target, api_key, args.workers)
    print(f"Phase 1: {len(drafts)} distinct questions  (spend ${cost_usd(usage):.2f})")
    if cost_usd(usage) > args.max_usd:
        sys.exit(f"Stopped after phase 1: spend ${cost_usd(usage):.2f} passed the ${args.max_usd:.2f} cap.")

    kept, usage = verify_all(drafts, briefing, api_key, args.workers, usage, args.max_usd)
    print(f"Phase 2: {len(kept)} verified, {len(drafts) - len(kept)} dropped or not reached  (spend ${cost_usd(usage):.2f})")

    chosen = balance(kept, args.target)
    questions = [to_question(i, v) for i, v in enumerate(chosen, 1)]
    OUT.write_text(TypeAdapter(list[Question]).dump_json(questions, indent=2).decode() + "\n")
    tiers = Counter(q.tier for q in questions)
    print(f"Wrote {len(questions)} questions to {OUT.relative_to(ROOT)}  tiers {dict(sorted(tiers.items()))}  "
          f"total spend ${cost_usd(usage):.2f}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="golden", description="Generate the synthetic golden question set.")
    p.add_argument("--target", type=int, default=200)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--max-usd", type=float, default=2.0, help="stop if teacher spend passes this")
    return p.parse_args()


# --- phase 1: questions, no SQL -----------------------------------------------------------------


def generate_questions(briefing: str, target: int, api_key: str, workers: int) -> tuple[list[DraftQuestion], Usage]:
    jobs = [
        (tier, THEMES[i % len(THEMES)])
        for tier, share in TIER_SHARE.items()
        for i in range(max(1, round(target * share * OVERSAMPLE / BATCH)))
    ]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        batches = list(pool.map(lambda job: ask_questions(job[0], job[1], briefing, api_key), jobs))
    return dedupe([q for batch, _ in batches for q in batch]), total(u for _, u in batches)


def ask_questions(tier: int, theme: str, briefing: str, api_key: str) -> tuple[list[DraftQuestion], Usage]:
    prompt = QUESTION_PROMPT.format(n=BATCH, tier=TIERS[tier], theme=theme, briefing=briefing)
    c = fireworks.chat([Message(role="user", content=prompt)], model=TEACHER, api_key=api_key, temperature=0.8,
                       response_format=fireworks.json_schema_format("batch", DraftBatch.model_json_schema()),
                       max_tokens=8000, timeout_s=180)
    try:
        drafts = [q.model_copy(update={"tier": tier}) for q in DraftBatch.model_validate_json(c.content).questions]
    except ValidationError:
        drafts = []
    return drafts, c.usage


def tokens(text: str) -> frozenset[str]:
    return frozenset(re.findall(r"[a-z0-9]+", text.lower()))


def similar(a: frozenset[str], b: frozenset[str]) -> bool:
    return len(a & b) / max(len(a | b), 1) >= NEAR_DUPLICATE


def dedupe(drafts: list[DraftQuestion]) -> list[DraftQuestion]:
    """Drop near-duplicates of each other and of the dev questions (the golden set must not leak into dev)."""
    seen = [tokens(q.question) for q in TypeAdapter(list[Question]).validate_json(DEV.read_text())]
    out = []
    for d in drafts:
        t = tokens(d.question)
        if not any(similar(t, s) for s in seen):
            seen.append(t)
            out.append(d)
    return out


def sql_tier(sql: str) -> int:
    """Difficulty from the gold SQL: 1 = no joins or grouping, 2 = one or two joins,
    3 = three or more joins, or grouping, ranking or a subquery on top of a join."""
    s = sql.lower()
    joins = len(re.findall(r"\bjoin\b", s))
    shaped = bool(re.search(r"group\s+by|\blimit\b|\bover\s*\(", s)) or s.count("select") > 1
    if joins >= 3 or (joins and shaped):
        return 3
    if joins:
        return 2
    return 2 if shaped else 1


# --- phase 2: gold SQL by agreement -------------------------------------------------------------


def verify_all(drafts: list[DraftQuestion], briefing: str, api_key: str, workers: int,
               usage: Usage, max_usd: float) -> tuple[list[Verified], Usage]:
    """Verify in chunks, stopping between chunks once spend passes the cap. Returns what was verified."""
    kept: list[Verified] = []
    chunk = workers * 4
    for start in range(0, len(drafts), chunk):
        if cost_usd(usage) > max_usd:
            print(f"Stopped phase 2 at {start}/{len(drafts)}: spend ${cost_usd(usage):.2f} passed the ${max_usd:.2f} cap.")
            break
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(lambda d: verify(d, briefing, api_key), drafts[start:start + chunk]))
        kept += [v for v, _ in results if v is not None]
        usage = usage + total(u for _, u in results)
    return kept, usage


def verify(draft: DraftQuestion, briefing: str, api_key: str) -> tuple[Verified | None, Usage]:
    """Sample SQL until two samples agree on a non-empty result (at most three samples)."""
    samples: list[SqlSample] = []
    usage = Usage()
    for _ in range(3):
        sample, used = sample_sql(draft.question, briefing, api_key)
        usage = usage + used
        if sample is None:
            continue
        match = next((s for s in samples if rows_match(sample.rows, s.rows, ordered=False)), None)
        if match:
            tiered = draft.model_copy(update={"tier": sql_tier(match.sql)})
            agreement = f"2/{len(samples) + 1}"
            return Verified(draft=tiered, sql=match.sql, columns=match.columns, rows=match.rows, agreement=agreement), usage
        samples.append(sample)
    return None, usage


def sample_sql(question: str, briefing: str, api_key: str) -> tuple[SqlSample | None, Usage]:
    messages = [Message(role="system", content=SQL_PROMPT.format(briefing=briefing)), Message(role="user", content=question)]
    try:
        c = fireworks.chat(messages, model=TEACHER, api_key=api_key, temperature=0.6, max_tokens=4000, timeout_s=120,
                           response_format=fireworks.json_schema_format("sql_reply", SQLReply.model_json_schema()),
                           session_id="golden-sql")
    except fireworks.FireworksError:
        return None, Usage()
    try:
        sql = guard_sql(SQLReply.model_validate_json(c.content).sql)
        with closing(connect_readonly()) as conn:
            cols, rows, truncated = run_sql(conn, sql, timeout_s=30, max_rows=MAX_ROWS)
    except (ValidationError, ReplyError, sqlite3.Error):
        return None, c.usage
    if not rows or truncated or all(v is None for r in rows for v in r):
        return None, c.usage
    return SqlSample(sql=sql, columns=cols, rows=rows), c.usage


def balance(kept: list[Verified], target: int) -> list[Verified]:
    """Take up to the target share per tier, then top up from any tier if one ran short."""
    by_tier = {t: [v for v in kept if v.draft.tier == t] for t in TIER_SHARE}
    chosen = [v for t, share in TIER_SHARE.items() for v in by_tier[t][: round(target * share)]]
    leftovers = [v for v in kept if v not in chosen]
    return sorted([*chosen, *leftovers[: max(0, target - len(chosen))]], key=lambda v: v.draft.tier)


def unique_names(columns: tuple[str, ...]) -> list[str]:
    """Column names as dict keys: a repeated name gets a suffix so no value is lost."""
    seen: Counter[str] = Counter()
    out = []
    for c in columns:
        seen[c] += 1
        out.append(c if seen[c] == 1 else f"{c}_{seen[c]}")
    return out


def to_question(i: int, v: Verified) -> Question:
    return Question(
        id=f"g_{i:03d}", question=v.draft.question, tier=v.draft.tier, gold_sql=v.sql,
        expected_result=[dict(zip(unique_names(v.columns), r)) for r in v.rows],
        source=f"synthetic:{TEACHER.rsplit('/', 1)[-1]}:agreement {v.agreement}",
    )


if __name__ == "__main__":
    main()
