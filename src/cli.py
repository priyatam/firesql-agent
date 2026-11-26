"""Interactive text-to-SQL CLI. Run with: uv run cli (or python -m src.cli)"""

from __future__ import annotations

import os
import sys

from dotenv import find_dotenv, load_dotenv

from src.agent import DEFAULT_MODEL, Config, Turn, answer, connect_readonly
from src.context import build_briefing
from src.fireworks import FireworksError

MAX_SHOWN_ROWS = 20
MAX_CELL_WIDTH = 40

HELP = """\
Ask a question about the bank database in plain English, e.g.
  How many accounts are in the Prague region?
Follow-up questions build on the previous answer.
Commands: :reset (forget the conversation), :help, exit / quit"""


def main() -> None:
    load_dotenv(find_dotenv(usecwd=True))
    api_key = os.environ.get("FIREWORKS_API_KEY")
    if not api_key:
        sys.exit("FIREWORKS_API_KEY is not set. Add it to .env or export it.")

    cfg = Config(model=os.environ.get("FIREWORKS_MODEL", DEFAULT_MODEL))
    conn = connect_readonly()
    print("Reading the database schema...")
    briefing = build_briefing(conn)
    print(f"Ready. Model: {short_model(cfg.model)}\n{HELP}")

    history: tuple[Turn, ...] = ()
    while True:
        try:
            question = input("\nask> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not question:
            continue
        if question.lower() in {"exit", "quit"}:
            break
        if question == ":help":
            print(HELP)
            continue
        if question == ":reset":
            history = ()
            print("Conversation cleared.")
            continue

        try:
            turn = answer(question, history=history, conn=conn, briefing=briefing, cfg=cfg, api_key=api_key)
        except FireworksError as exc:
            print(f"Error talking to Fireworks: {exc}")
            continue
        except KeyboardInterrupt:
            print("\nCancelled.")
            continue

        print(render_turn(turn, cfg.model))
        if turn.ok:
            history = (*history, turn)

    conn.close()


def render_turn(turn: Turn, model: str) -> str:
    sql = "\n".join("  " + line for line in turn.sql.splitlines()) or "  (none)"
    body = f"Error: {turn.error}" if turn.error else format_table(turn.columns, turn.rows, turn.truncated)
    return f"\nSQL:\n{sql}\n\n{body}\n\n{format_stats(turn, model)}"


def format_stats(turn: Turn, model: str) -> str:
    u = turn.usage
    cached = f", {u.cached_tokens:,} cached" if u.cached_tokens else ""
    attempts = "1 attempt" if turn.attempts == 1 else f"{turn.attempts} attempts"
    return (
        f"[{turn.total_s:.2f}s total: model {turn.llm_s:.2f}s, sql {turn.sql_s:.2f}s | "
        f"{u.prompt_tokens:,} in{cached} / {u.completion_tokens:,} out tokens | {attempts} | {short_model(model)}]"
    )


def format_table(columns: tuple[str, ...], rows: tuple[tuple, ...], truncated: bool = False) -> str:
    if not columns:
        return "(no columns returned)"
    if not rows:
        return "(no rows)"
    shown = [[format_cell(v) for v in row] for row in rows[:MAX_SHOWN_ROWS]]
    widths = [max(len(str(c)), *(len(r[i]) for r in shown)) for i, c in enumerate(columns)]
    line = lambda cells: " | ".join(cell.ljust(w) for cell, w in zip(cells, widths))  # noqa: E731
    out = [line(list(columns)), "-+-".join("-" * w for w in widths), *(line(r) for r in shown)]
    hidden = len(rows) - len(shown)
    if hidden > 0 or truncated:
        out.append(f"... {hidden:,}{'+' if truncated else ''} more rows")
    out.append(f"({len(rows):,}{'+' if truncated else ''} row{'s' if len(rows) != 1 else ''})")
    return "\n".join(out)


def format_cell(value: object) -> str:
    if value is None:
        return "NULL"
    text = f"{value:.2f}" if isinstance(value, float) else str(value)
    return text if len(text) <= MAX_CELL_WIDTH else text[: MAX_CELL_WIDTH - 1] + "…"


def short_model(model: str) -> str:
    return model.rsplit("/", 1)[-1]


if __name__ == "__main__":
    main()
