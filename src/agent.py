"""Agent mode (v0.2): a Strands agent that explores the schema with tools before answering.

One fixed path: prompt -> write SQL -> test it once with run_sql -> final {reasoning, sql}.
This module then runs the final SQL itself, so agent mode returns the same Turn as chat mode.
While the full briefing fits (MAX_BRIEFING_CHARS), it goes in the cached system prompt, so the agent
needs no schema lookups. Larger schemas get the compact outline, and the agent fetches column notes
and coded values with tools on demand.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict
from sqlalchemy.engine import Engine
from strands import Agent, tool
from strands.models.openai import OpenAIModel
from strands.types.agent import Limits

from src import db
from src.chat import (
    Config,
    ReplyError,
    SQLReply,
    Turn,
    declined_turn,
    for_dialect,
    guard_sql,
)
from src.context import Table, qualified, render_briefing, render_outline, render_table
from src.fireworks import Usage

BASE_URL = "https://api.fireworks.ai/inference/v1"
# Strands warns on every call that it drops the model's reasoning text between turns; that is expected here.
logging.getLogger("strands.models.openai").setLevel(logging.ERROR)
TOOL_ROWS = 20  # rows a tool shows the model; the final answer is not capped by this
MAX_BRIEFING_CHARS = 40_000  # above this, the prompt gets the outline and the agent uses describe_table

SYSTEM_PROMPT = """\
You are a careful data analyst answering questions about a SQLite database by writing ONE \
read-only query. You have tools; use them, then give the final query.

How to work:
1. Write the query from the schema below. Only if a table's column notes or coded values are not \
shown, call describe_table; only if a value you need is not listed, call sample_values.
2. Test the query once with run_sql. If it errors or the result looks wrong, fix it and test again.
3. Finish with the final query as {"reasoning": "<one or two short sentences>", "sql": "<query>"}.
   If the message is not a question about this data (gibberish, a greeting, an unrelated request), don't call \
tools: finish with "sql" as an empty string and say why in "reasoning". Never repeat the previous query unless asked.

Rules:
- Use only tables and columns that exist. Never invent names.
- Apply only the conditions the question states. Don't narrow it with a status, type or category \
it doesn't mention: "loans" means all loans, "standing orders" means all orders.
- Return only the columns the question asks for; no extra counts or ids.
- SQLite dialect. Double-quote identifiers that are SQL keywords, e.g. the table "order".
- Filter on exact stored values (codes such as 'POPLATEK TYDNE'), not English words.
- Dates are text 'YYYY-MM-DD'; use strftime('%Y', col) or string comparisons.
- When counting entities reached through joins, use COUNT(DISTINCT ...).
- Select only the columns needed. ORDER BY for top/most/least, LIMIT for top N.
- For follow-up questions, build on the previous query. The previous result is shown above the question; \
when the question refers to it ("those", "the third one", "that year"), use the values it shows.

# Database
"""


class AgentUsage(BaseModel):
    """Strands' running totals, read before and after a question."""

    model_config = ConfigDict(frozen=True)
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    tool_calls: int = 0
    cycles: int = 0


def build_model(cfg: Config, api_key: str) -> OpenAIModel:
    params: dict[str, Any] = {"temperature": 0.0, "max_tokens": 2048}
    if cfg.reasoning_effort:
        params["reasoning_effort"] = cfg.reasoning_effort
    client_args: dict[str, Any] = {"api_key": api_key, "base_url": BASE_URL}
    if cfg.cache_key:
        client_args["default_headers"] = {"x-session-affinity": cfg.cache_key}
    return OpenAIModel(client_args=client_args, model_id=cfg.model, params=params)


def build_tools(tables: tuple[Table, ...], engine: Engine, cfg: Config) -> list:
    """Read-only tools. Strands runs tools on worker threads; the engine hands each call its own connection."""
    by_name = {**{t.name: t for t in tables}, **{qualified(t): t for t in tables}}
    find = lambda name: by_name.get(name.strip().replace('"', "").replace("`", ""))

    def query(sql: str, max_rows: int):
        return db.query(engine, sql, timeout_s=cfg.sql_timeout_s, max_rows=max_rows)

    @tool
    def list_tables() -> str:
        """List every table with its row count, columns and joins."""
        return render_outline(tables)

    @tool
    def describe_table(table: str) -> str:
        """Column meanings, notes, stored formats and the full list of values for coded columns of one table."""
        t = find(table)
        return render_table(t) if t else f"No table named {table!r}. Tables: {', '.join(qualified(t) for t in tables)}"

    @tool
    def sample_values(table: str, column: str, limit: int = 20) -> str:
        """The most common distinct stored values of one column, with counts."""
        t = find(table)
        if t is None or column not in {c.name for c in t.columns}:
            return f"No column {column!r} in table {table!r}."
        col = sa.column(column)
        n = sa.func.count().label("n")
        sql = sa.select(col, n).select_from(sa.table(t.name, col, schema=t.schema_name)).group_by(col).order_by(n.desc()).limit(max(1, min(limit, 100)))
        _, rows, _ = query(sql, 100)
        return "\n".join(f"{v!r}: {n:,}" for v, n in rows)

    @tool(name="run_sql")
    def run_sql_tool(sql: str) -> str:
        """Run one read-only SELECT and see the first rows, or the error. Use it to test your query."""
        try:
            columns, rows, truncated = query(guard_sql(sql), TOOL_ROWS)
        except (ReplyError, db.QueryError) as exc:
            return f"Error: {exc}"
        more = " (more rows not shown)" if truncated else ""
        return "\n".join([" | ".join(columns), *(" | ".join(map(str, r)) for r in rows)]) + f"\n({len(rows)} rows{more})"

    return [list_tables, describe_table, sample_values, run_sql_tool]


def build_agent(tables: tuple[Table, ...], engine: Engine, cfg: Config, api_key: str) -> Agent:
    """One agent per conversation; it keeps its own message history for follow-ups."""
    return Agent(
        model=build_model(cfg, api_key),
        system_prompt=for_dialect(SYSTEM_PROMPT, cfg.dialect) + schema_for_prompt(tables),
        tools=build_tools(tables, engine, cfg),
        callback_handler=None,
    )


def schema_for_prompt(tables: tuple[Table, ...]) -> str:
    """The full briefing while it fits (it is cached, so it is cheap); otherwise the compact outline."""
    briefing = render_briefing(tables)
    return briefing if len(briefing) <= MAX_BRIEFING_CHARS else render_outline(tables)


def usage_snapshot(agent: Agent) -> AgentUsage:
    metrics = agent.event_loop_metrics
    usage = metrics.accumulated_usage or {}
    return AgentUsage(
        input_tokens=usage.get("inputTokens", 0),
        output_tokens=usage.get("outputTokens", 0),
        cached_tokens=usage.get("cacheReadInputTokens", 0),
        tool_calls=sum(m.call_count for m in (metrics.tool_metrics or {}).values()),
        cycles=metrics.cycle_count,
    )


def followup_prompt(question: str, history: tuple[Turn, ...], max_rows: int) -> str:
    """The question, preceded by the previous answer's rows, so a follow-up can refer to results ("the third one").

    The agent already remembers earlier questions and its SQL; only the rows this module ran are new to it.
    """
    last = history[-1] if history else None
    if max_rows <= 0 or last is None or not last.ok:
        return question
    shown = last.rows[:max_rows]
    more = f" (first {len(shown)} of {len(last.rows)} rows)" if len(last.rows) > len(shown) else ""
    table = "\n".join([" | ".join(last.columns), *(" | ".join(map(str, r)) for r in shown)])
    return f"Result of the previous query{more}:\n{table}\n\nQuestion: {question}"


def answer_with_agent(
    agent: Agent,
    question: str,
    *,
    history: tuple[Turn, ...] = (),
    engine: Engine,
    cfg: Config,
    call: Callable[[Agent, str], Any] | None = None,
) -> Turn:
    """Answer one question in agent mode. `call` is injectable so tests never reach the model."""
    start, before = time.perf_counter(), usage_snapshot(agent)
    limits = Limits(turns=cfg.max_turns, structured_output_attempts=2)
    invoke = call or (lambda a, q: a(q, structured_output_model=SQLReply, limits=limits))
    sql, error, declined = "", None, None
    try:
        result = invoke(agent, followup_prompt(question, history, cfg.history_rows))
        reply = result.structured_output
        if not isinstance(reply, SQLReply):
            raise ReplyError("the agent finished without a final query")
        if not reply.sql.strip():
            declined = reply
        else:
            sql = guard_sql(reply.sql)
    except Exception as exc:  # noqa: BLE001  limit or structured-output failure: report it as the turn's error
        error = f"{type(exc).__name__}: {exc}" if not isinstance(exc, ReplyError) else str(exc)
    llm_s = time.perf_counter() - start
    after = usage_snapshot(agent)
    usage = Usage(
        prompt_tokens=after.input_tokens - before.input_tokens,
        completion_tokens=after.output_tokens - before.output_tokens,
        cached_tokens=after.cached_tokens - before.cached_tokens,
    )
    tool_calls, cycles = after.tool_calls - before.tool_calls, after.cycles - before.cycles
    if declined is not None:
        return declined_turn(question, declined, attempts=cycles, usage=usage, llm_s=llm_s, start=start, tool_calls=tool_calls)
    if error:
        return Turn(question=question, sql=sql, error=error, attempts=cycles, usage=usage, tool_calls=tool_calls,
                    llm_s=llm_s, total_s=time.perf_counter() - start)
    sql_start = time.perf_counter()
    try:
        columns, rows, truncated = db.query(engine, sql, timeout_s=cfg.sql_timeout_s, max_rows=cfg.max_rows)
    except db.QueryError as exc:
        return Turn(question=question, sql=sql, error=str(exc), attempts=cycles, usage=usage, tool_calls=tool_calls,
                    llm_s=llm_s, sql_s=time.perf_counter() - sql_start, total_s=time.perf_counter() - start)
    return Turn(question=question, sql=sql, columns=columns, rows=rows, truncated=truncated, attempts=cycles,
                usage=usage, tool_calls=tool_calls, llm_s=llm_s, sql_s=time.perf_counter() - sql_start,
                total_s=time.perf_counter() - start)
