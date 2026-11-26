"""Interactive text-to-SQL CLI. Run with: uv run cli [--mode chat|agent] [--model ID]"""

from __future__ import annotations

import argparse
import os
import sys

from dotenv import find_dotenv, load_dotenv

from src.chat import DEFAULT_MODEL, MODES, Config, Turn, connect_readonly, make_session
from src.context import cache_path, load_tables
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
    args = parse_args()
    api_key = os.environ.get("FIREWORKS_API_KEY")
    if not api_key:
        sys.exit("FIREWORKS_API_KEY is not set. Add it to .env or export it.")

    cfg = Config(model=args.model, mode=args.mode)
    conn = connect_readonly()
    if not cache_path().exists():
        print("First run: reading the database schema (a few seconds; cached after this)...")
    tables = load_tables(conn)
    ask = make_session(cfg, conn, tables, api_key)
    print(f"Ready. Mode: {cfg.mode} · model: {short_model(cfg.model)}\n{HELP}")

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
            history, ask = (), make_session(cfg, conn, tables, api_key)
            print("Conversation cleared.")
            continue

        try:
            turn = ask(question, history)
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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="cli", description="Ask questions about the bank database in plain English.", allow_abbrev=False)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--chat", dest="mode", action="store_const", const="chat",
                      help="chat mode (v0.1): one model call per question, with the full schema briefing")
    mode.add_argument("--agent", dest="mode", action="store_const", const="agent",
                      help="agent mode (v0.2): a Strands agent that tests its SQL before answering")
    parser.set_defaults(mode=os.environ.get("FIREWORKS_MODE", "chat"))
    parser.add_argument("--model", default=os.environ.get("FIREWORKS_MODEL", DEFAULT_MODEL),
                        help="Fireworks model id. Default: $FIREWORKS_MODEL or %(default)s.")
    args = parser.parse_args(argv)
    if args.mode not in MODES:
        parser.error(f"FIREWORKS_MODE must be one of {', '.join(MODES)}, not {args.mode!r}")
    return args


def render_turn(turn: Turn, model: str) -> str:
    sql = "\n".join("  " + line for line in turn.sql.splitlines()) or "  (none)"
    body = f"Error: {turn.error}" if turn.error else format_table(turn.columns, turn.rows, turn.truncated)
    return f"\nSQL:\n{sql}\n\n{body}\n\n{format_stats(turn, model)}"


def format_stats(turn: Turn, model: str) -> str:
    u = turn.usage
    cached = f", {u.cached_tokens:,} cached" if u.cached_tokens else ""
    if turn.tool_calls:  # agent mode: attempts counts model calls
        attempts = f"{turn.attempts} model calls, {turn.tool_calls} tool call{'s' if turn.tool_calls != 1 else ''}"
    else:
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
    line = lambda cells: " | ".join(cell.ljust(w) for cell, w in zip(cells, widths))
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
