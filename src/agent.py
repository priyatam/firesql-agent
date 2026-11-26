"""Text-to-SQL agent: question (+ conversation) -> SQL -> rows, with one repair attempt.

Pipeline: build messages -> ask the model for {reasoning, sql} -> check it is read-only
-> run it -> on any failure, send the error back once and retry.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

from src import fireworks
from src.fireworks import Usage
from src.utils import DEFAULT_DB_PATH

DEFAULT_MODEL = "accounts/fireworks/models/glm-5p3-flash"

SYSTEM_PROMPT = """\
You are a careful data analyst. Write ONE read-only SQLite query that answers the user's \
question, using only the tables and columns described below.

Rules:
- Use only the tables and columns listed. Never invent names.
- SQLite dialect. Double-quote identifiers that are SQL keywords, e.g. the table "order".
- Filter on the exact stored values listed under "all values" (codes such as 'POPLATEK TYDNE'), \
using the notes to map the question's words to codes.
- Dates are stored as text 'YYYY-MM-DD'; use strftime('%Y', col) or string comparisons.
- Columns marked "numbers stored as TEXT" must be CAST before comparing or doing math.
- When counting entities reached through joins, use COUNT(DISTINCT ...) to avoid double counting.
- Select only the columns needed to answer. Use ORDER BY for "top/most/least" questions and LIMIT \
for "top N".
- For follow-up questions, build on the previous query.

Reply as JSON: {"reasoning": "<one or two short sentences>", "sql": "<the query>"}

# Database
"""

READ_ONLY_START = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)
WRITE_KEYWORDS = re.compile(r"\b(insert|update|delete|drop|alter|create|attach|detach|pragma|vacuum)\b", re.I)


class SQLReply(BaseModel):
    """The model's structured reply."""

    model_config = ConfigDict(frozen=True)
    reasoning: str = ""
    sql: str


REPLY_FORMAT = fireworks.json_schema_format("sql_reply", SQLReply.model_json_schema())


class ReplyError(ValueError):
    """The model's reply could not be used (bad JSON, cut off, or not a safe query)."""


@dataclass(frozen=True)
class Config:
    model: str = DEFAULT_MODEL
    reasoning_effort: str | None = "low"
    max_repairs: int = 1
    history_turns: int = 3
    max_rows: int = 1000
    sql_timeout_s: float = 15.0


@dataclass(frozen=True)
class Turn:
    question: str
    sql: str
    columns: tuple[str, ...] = ()
    rows: tuple[tuple, ...] = ()
    truncated: bool = False
    error: str | None = None
    attempts: int = 1
    usage: Usage = Usage()
    llm_s: float = 0.0
    sql_s: float = 0.0
    total_s: float = 0.0

    @property
    def ok(self) -> bool:
        return self.error is None


def connect_readonly(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open the database read-only, so no generated query can modify it."""
    path = Path(db_path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}. Run ./setup.sh first.")
    return sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)


def answer(
    question: str,
    *,
    history: tuple[Turn, ...],
    conn: sqlite3.Connection,
    briefing: str,
    cfg: Config,
    api_key: str,
) -> Turn:
    """Answer one question. Raises fireworks.FireworksError only if the API is unreachable."""
    start = time.perf_counter()
    messages = build_messages(briefing, history[-cfg.history_turns :], question)
    usage, llm_s, sql_s, sql, error = Usage(), 0.0, 0.0, "", ""

    for attempt in range(1, cfg.max_repairs + 2):
        completion = fireworks.chat(
            messages,
            model=cfg.model,
            api_key=api_key,
            response_format=REPLY_FORMAT,
            reasoning_effort=cfg.reasoning_effort,
        )
        usage, llm_s = usage + completion.usage, llm_s + completion.latency_s
        sql_start = time.perf_counter()
        try:
            sql = guard_sql(parse_reply(completion).sql)
            columns, rows, truncated = run_sql(conn, sql, timeout_s=cfg.sql_timeout_s, max_rows=cfg.max_rows)
            return Turn(
                question, sql, columns, rows, truncated,
                attempts=attempt, usage=usage, llm_s=llm_s,
                sql_s=sql_s + time.perf_counter() - sql_start,
                total_s=time.perf_counter() - start,
            )  # fmt: skip
        except (ReplyError, sqlite3.Error) as exc:
            sql_s += time.perf_counter() - sql_start
            error = str(exc)
            messages = [*messages, *repair_messages(completion.content, error)]

    return Turn(
        question, sql, error=error, attempts=cfg.max_repairs + 1,
        usage=usage, llm_s=llm_s, sql_s=sql_s, total_s=time.perf_counter() - start,
    )  # fmt: skip


def build_messages(briefing: str, history: tuple[Turn, ...], question: str) -> list[dict[str, str]]:
    """System briefing first (stable, cacheable prefix), then prior turns, then the question."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT + briefing}]
    for turn in history:
        if turn.ok:
            messages.append({"role": "user", "content": turn.question})
            messages.append({"role": "assistant", "content": json.dumps({"reasoning": "", "sql": turn.sql})})
    messages.append({"role": "user", "content": question})
    return messages


def repair_messages(bad_reply: str, error: str) -> list[dict[str, str]]:
    return [
        {"role": "assistant", "content": bad_reply},
        {"role": "user", "content": f"That failed: {error}\nFix it and reply with the corrected query in the same JSON format."},
    ]


def parse_reply(completion: fireworks.Completion) -> SQLReply:
    if completion.finish_reason == "length":
        raise ReplyError("the model's reply was cut off before it finished")
    text = _strip_code_fences(completion.content)
    try:
        return SQLReply.model_validate_json(text)
    except ValidationError as exc:
        raise ReplyError(f"reply was not JSON with a 'sql' field: {text[:200]!r}") from exc


def guard_sql(sql: str) -> str:
    """Accept a single SELECT/WITH statement. The read-only connection is the real safeguard."""
    cleaned = _strip_code_fences(sql).strip().rstrip(";").strip()
    if not cleaned:
        raise ReplyError("the query was empty")
    if ";" in cleaned:
        raise ReplyError("only a single SQL statement is allowed")
    if not READ_ONLY_START.match(cleaned) or WRITE_KEYWORDS.search(cleaned):
        raise ReplyError("only read-only SELECT queries are allowed")
    return cleaned


def run_sql(
    conn: sqlite3.Connection, sql: str, *, timeout_s: float, max_rows: int
) -> tuple[tuple[str, ...], tuple[tuple, ...], bool]:
    """Run a query with a time limit. Returns (columns, rows, truncated)."""
    deadline = time.monotonic() + timeout_s
    conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)
    try:
        cursor = conn.execute(sql)
        columns = tuple(d[0] for d in cursor.description or ())
        rows = cursor.fetchmany(max_rows + 1)
    except sqlite3.OperationalError as exc:
        if "interrupted" in str(exc):
            raise sqlite3.OperationalError(f"query took longer than {timeout_s:.0f}s and was stopped") from exc
        raise
    finally:
        conn.set_progress_handler(None, 0)
    return columns, tuple(tuple(r) for r in rows[:max_rows]), len(rows) > max_rows


def _strip_code_fences(text: str) -> str:
    match = re.search(r"```(?:json|sql)?\s*(.*?)```", text, re.DOTALL)
    return (match.group(1) if match else text).strip()
