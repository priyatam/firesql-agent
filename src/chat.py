"""Chat mode (v0.1): question (+ conversation) -> one LLM call -> SQL -> rows, with one repair attempt.

Pipeline: build messages -> ask the model for {reasoning, sql} -> check it is read-only
-> run it -> on any failure, send the error back once and retry.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy.engine import Engine

from src import db, fireworks
from src.context import Table, render_briefing
from src.fireworks import Message, Usage

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
- If the message is not a question about this data (gibberish, a greeting, an unrelated request), \
reply with "sql" as an empty string and say why in "reasoning". Never repeat the previous query unless asked.

Reply as JSON: {"reasoning": "<one or two short sentences>", "sql": "<the query>"}

# Database
"""

READ_ONLY_START = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)
WRITE_KEYWORDS = re.compile(
    r"\b(insert|update|delete|drop|alter|create|attach|detach|pragma|vacuum|into|grant|revoke|truncate|merge|copy|lock|call)\b",
    re.IGNORECASE,
)
# The SQLite wording in the prompts is swapped for the connected database's (see for_dialect).
DIALECTS = {
    "sqlite": ("SQLite", "", ""),
    "postgresql": (
        "PostgreSQL",
        'PostgreSQL dialect. Double-quote identifiers that are SQL keywords or mixed case, e.g. the table "order".',
        "For dates use EXTRACT(YEAR FROM col) or ranges such as col >= '1997-01-01'; CAST text before comparing it as a number or date.",
    ),
    "mysql": (
        "MySQL",
        'MySQL dialect with ANSI_QUOTES: double-quote identifiers that are SQL keywords, e.g. the table "order"; single-quote strings.',
        "For dates use YEAR(col) or ranges such as col >= '1997-01-01'.",
    ),
    "mariadb": (
        "MariaDB",
        'MariaDB dialect with ANSI_QUOTES: double-quote identifiers that are SQL keywords, e.g. the table "order"; single-quote strings.',
        "For dates use YEAR(col) or ranges such as col >= '1997-01-01'.",
    ),
}
STRING_LITERAL = re.compile(r"'(?:[^']|'')*'")


class SQLReply(BaseModel):
    """The model's structured reply."""

    model_config = ConfigDict(frozen=True)
    reasoning: str = ""
    sql: str


REPLY_FORMAT = fireworks.json_schema_format("sql_reply", SQLReply.model_json_schema())


NOT_A_QUESTION = "That doesn't look like a question about this database"


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
    dialect: str = "sqlite"  # the connected database; set by make_session


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
    declined: bool = False  # the model judged the message not a question about the data
    llm_s: float = 0.0
    sql_s: float = 0.0
    total_s: float = 0.0

    @property
    def ok(self) -> bool:
        return self.error is None


def answer(
    question: str,
    *,
    history: tuple[Turn, ...],
    engine: Engine,
    briefing: str,
    cfg: Config,
    api_key: str,
) -> Turn:
    """Answer one question in chat mode (v0.1): one model call, plus one repair on error.

    Raises fireworks.FireworksError only if the API is unreachable.
    """
    start = time.perf_counter()
    messages = build_messages(briefing, history[-cfg.history_turns :], question, cfg.dialect)
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
            reply = parse_reply(completion)
            if not reply.sql.strip():
                return declined_turn(question, reply, attempts=attempt, usage=usage, llm_s=llm_s, start=start)
            sql = guard_sql(reply.sql)
            columns, rows, truncated = db.query(engine, sql, timeout_s=cfg.sql_timeout_s, max_rows=cfg.max_rows)
            return Turn(
                question=question, sql=sql, columns=columns, rows=rows, truncated=truncated,
                attempts=attempt, usage=usage, llm_s=llm_s,
                sql_s=sql_s + time.perf_counter() - sql_start,
                total_s=time.perf_counter() - start,
            )  # fmt: skip
        except (ReplyError, db.QueryError) as exc:
            sql_s += time.perf_counter() - sql_start
            error = str(exc)
            messages = [*messages, *repair_messages(completion.content, error)]

    return Turn(
        question=question, sql=sql, error=error, attempts=cfg.max_repairs + 1,
        usage=usage, llm_s=llm_s, sql_s=sql_s, total_s=time.perf_counter() - start,
    )  # fmt: skip


def declined_turn(question: str, reply: SQLReply, *, attempts: int, usage: Usage, llm_s: float, start: float, tool_calls: int = 0) -> Turn:
    """No query: the model judged the message not a question about the data. Nothing runs, nothing joins history."""
    reason = reply.reasoning.strip()
    return Turn(
        question=question, sql="", error=f"{NOT_A_QUESTION}: {reason}" if reason else NOT_A_QUESTION, declined=True,
        attempts=attempts, usage=usage, tool_calls=tool_calls, llm_s=llm_s, total_s=time.perf_counter() - start,
    )  # fmt: skip


def build_messages(briefing: str, history: tuple[Turn, ...], question: str, dialect: str = "sqlite") -> list[Message]:
    """System briefing first (stable, cacheable prefix), then prior turns, then the question."""
    messages = [Message(role="system", content=for_dialect(SYSTEM_PROMPT, dialect) + briefing)]
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
    """Accept a single SELECT/WITH statement. The read-only session (db.py) is the real safeguard."""
    cleaned = _strip_code_fences(sql).strip().rstrip(";").strip()
    if not cleaned:
        raise ReplyError("the query was empty")
    code = STRING_LITERAL.sub("''", cleaned)  # words and semicolons inside quoted values don't count
    if ";" in code:
        raise ReplyError("only a single SQL statement is allowed")
    if not READ_ONLY_START.match(code) or WRITE_KEYWORDS.search(code):
        raise ReplyError("only read-only SELECT queries are allowed")
    return cleaned


def for_dialect(prompt: str, dialect: str) -> str:
    """The prompt with its SQLite-specific lines replaced for another database. SQLite prompts are unchanged."""
    if dialect == "sqlite":
        return prompt
    name, quoting, dates = DIALECTS.get(dialect) or (
        dialect,
        f'{dialect} dialect. Double-quote identifiers that are SQL keywords, e.g. the table "order".',
        f"For dates use {dialect}'s date functions or ranges such as col >= '1997-01-01'.",
    )
    prompt = re.sub(r"(?m)^- SQLite dialect\..*$", f"- {quoting}", prompt)
    prompt = re.sub(r"(?m)^- Dates are .*$", f"- {dates}", prompt)
    return prompt.replace("SQLite", name)


def _strip_code_fences(text: str) -> str:
    match = re.search(r"```(?:json|sql)?\s*(.*?)```", text, re.DOTALL)
    return (match.group(1) if match else text).strip()

# --- sessions: one interface over every version ------------------------------------------------
#
#   chat  v0.1: one model call with the full briefing, plus one repair on error
#   agent        v0.2: a Strands agent that explores the schema with tools, then answers
#
# agent.py imports this module, so it is imported inside the functions (and Strands loads only in agent mode).

MODES = ("chat", "agent")

Ask = Callable[[str, tuple[Turn, ...]], Turn]


def make_session(cfg: Config, engine: Engine, tables: tuple[Table, ...], api_key: str) -> Ask:
    """A fresh conversation in the configured mode: ask(question, history) -> Turn. Call again to reset."""
    briefing = render_briefing(tables)
    cfg = cfg.model_copy(update={"dialect": db.dialect(engine), "cache_key": cfg.cache_key or cache_key(cfg.mode, briefing)})
    if cfg.mode == "agent":
        from src.agent import (  # Strands loads only in agent mode
            answer_with_agent,
            build_agent,
        )

        agent = build_agent(tables, engine, cfg, api_key)
        return lambda question, history: answer_with_agent(agent, question, history=history, engine=engine, cfg=cfg)
    return lambda question, history: answer(
        question, history=history, engine=engine, briefing=briefing, cfg=cfg, api_key=api_key
    )


def cache_key(mode: str, briefing: str) -> str:
    """Same database and mode, same key: every session shares the cached prompt prefix on one replica."""
    prefix = ""
    if mode == "agent":
        from src.agent import SYSTEM_PROMPT

        prefix = SYSTEM_PROMPT
    return hashlib.sha256(f"{mode}\n{prefix}\n{briefing}".encode()).hexdigest()[:16]
