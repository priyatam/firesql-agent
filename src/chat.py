"""Chat mode (v0.1): question (+ conversation) -> one LLM call -> SQL -> rows, with one repair attempt.

Pipeline: build messages -> ask the model for {reasoning, sql} -> check it is read-only
-> run it -> on any failure, send the error back once and retry.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from src import fireworks
from src.context import Table, render_briefing
from src.fireworks import Message, Usage
from src.utils import DB_PATH

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
WRITE_KEYWORDS = re.compile(r"\b(insert|update|delete|drop|alter|create|attach|detach|pragma|vacuum)\b", re.IGNORECASE)
STRING_LITERAL = re.compile(r"'(?:[^']|'')*'")


class SQLReply(BaseModel):
    """The model's structured reply."""

    model_config = ConfigDict(frozen=True)
    reasoning: str = ""
    sql: str


REPLY_FORMAT = fireworks.json_schema_format("sql_reply", SQLReply.model_json_schema())


class ReplyError(ValueError):
    """The model's reply could not be used (bad JSON, cut off, or not a safe query)."""


class Config(BaseModel):
    model_config = ConfigDict(frozen=True)
    model: str = DEFAULT_MODEL
    mode: Literal["chat", "agent"] = "chat"  # v0.1 or v0.2, see make_session below
    reasoning_effort: str | None = "low"
    max_repairs: int = 1
    history_turns: int = 3
    history_rows: int = 10  # agent mode: rows of the previous result shown with a follow-up (0 = none)
    max_rows: int = 1000
    sql_timeout_s: float = 15.0
    max_turns: int = 8  # agent mode: model calls per question
    cache_key: str | None = None  # session affinity for the prompt cache; set per database by make_session


class Turn(BaseModel):
    model_config = ConfigDict(frozen=True)
    question: str
    sql: str
    columns: tuple[str, ...] = ()
    rows: tuple[tuple[Any, ...], ...] = ()
    truncated: bool = False
    error: str | None = None
    attempts: int = 1
    usage: Usage = Usage()
    tool_calls: int = 0  # agent mode only
    llm_s: float = 0.0
    sql_s: float = 0.0
    total_s: float = 0.0

    @property
    def ok(self) -> bool:
        return self.error is None


def connect_readonly(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
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
    """Answer one question in chat mode (v0.1): one model call, plus one repair on error.

    Raises fireworks.FireworksError only if the API is unreachable.
    """
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
            session_id=cfg.cache_key,
        )
        usage, llm_s = usage + completion.usage, llm_s + completion.latency_s
        sql_start = time.perf_counter()
        try:
            sql = guard_sql(parse_reply(completion).sql)
            columns, rows, truncated = run_sql(conn, sql, timeout_s=cfg.sql_timeout_s, max_rows=cfg.max_rows)
            return Turn(
                question=question, sql=sql, columns=columns, rows=rows, truncated=truncated,
                attempts=attempt, usage=usage, llm_s=llm_s,
                sql_s=sql_s + time.perf_counter() - sql_start,
                total_s=time.perf_counter() - start,
            )  # fmt: skip
        except (ReplyError, sqlite3.Error) as exc:
            sql_s += time.perf_counter() - sql_start
            error = str(exc)
            messages = [*messages, *repair_messages(completion.content, error)]

    return Turn(
        question=question, sql=sql, error=error, attempts=cfg.max_repairs + 1,
        usage=usage, llm_s=llm_s, sql_s=sql_s, total_s=time.perf_counter() - start,
    )  # fmt: skip


def build_messages(briefing: str, history: tuple[Turn, ...], question: str) -> list[Message]:
    """System briefing first (stable, cacheable prefix), then prior turns, then the question."""
    messages = [Message(role="system", content=SYSTEM_PROMPT + briefing)]
    for turn in history:
        if turn.ok:
            reply = SQLReply(reasoning="", sql=turn.sql).model_dump_json()
            messages += [Message(role="user", content=turn.question), Message(role="assistant", content=reply)]
    return [*messages, Message(role="user", content=question)]


def repair_messages(bad_reply: str, error: str) -> list[Message]:
    return [
        Message(role="assistant", content=bad_reply),
        Message(role="user", content=f"That failed: {error}\nFix it and reply with the corrected query in the same JSON format."),
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
    code = STRING_LITERAL.sub("''", cleaned)  # words and semicolons inside quoted values don't count
    if ";" in code:
        raise ReplyError("only a single SQL statement is allowed")
    if not READ_ONLY_START.match(code) or WRITE_KEYWORDS.search(code):
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

# --- sessions: one interface over every version ------------------------------------------------
#
#   chat  v0.1: one model call with the full briefing, plus one repair on error
#   agent        v0.2: a Strands agent that explores the schema with tools, then answers
#
# harness.py imports this module, so it is imported inside the functions (and Strands loads only in agent mode).

MODES = ("chat", "agent")

Ask = Callable[[str, tuple[Turn, ...]], Turn]


def make_session(cfg: Config, conn: sqlite3.Connection, tables: tuple[Table, ...], api_key: str) -> Ask:
    """A fresh conversation in the configured mode: ask(question, history) -> Turn. Call again to reset."""
    briefing = render_briefing(tables)
    cfg = cfg.model_copy(update={"cache_key": cfg.cache_key or cache_key(cfg.mode, briefing)})
    if cfg.mode == "agent":
        from src.harness import (  # Strands loads only in agent mode
            answer_with_agent,
            build_agent,
        )

        agent = build_agent(tables, cfg, api_key)
        return lambda question, history: answer_with_agent(agent, question, history=history, conn=conn, cfg=cfg)
    return lambda question, history: answer(
        question, history=history, conn=conn, briefing=briefing, cfg=cfg, api_key=api_key
    )


def cache_key(mode: str, briefing: str) -> str:
    """Same database and mode, same key: every session shares the cached prompt prefix on one replica."""
    prefix = ""
    if mode == "agent":
        from src.harness import SYSTEM_PROMPT

        prefix = SYSTEM_PROMPT
    return hashlib.sha256(f"{mode}\n{prefix}\n{briefing}".encode()).hexdigest()[:16]
