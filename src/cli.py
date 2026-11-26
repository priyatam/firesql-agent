"""Interactive text-to-SQL CLI. Run with: uv run cli [--chat | --agent] [--db-url URL] [--model ID]

The only module that touches the terminal. It draws with rich: highlighted SQL, a results table and a stats line.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from dotenv import find_dotenv, load_dotenv
from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from src import db
from src.chat import DEFAULT_MODEL, MODES, Config, Turn, make_session
from src.context import cache_path, load_tables
from src.fireworks import FireworksError
from src.utils import DOCS_DIR

MAX_SHOWN_ROWS = 20
MAX_CELL_WIDTH = 40
SQL_THEME = "ansi_dark"  # uses the terminal's own colors

HELP = """\
Ask a question about the database in plain English, e.g. [italic]How many accounts are in the Prague region?[/]
Follow-up questions build on the previous answer.
Commands: [bold]:reset[/] forget the conversation · [bold]:help[/] · [bold]exit[/] / [bold]quit[/]"""


def main() -> None:
    try:
        import readline  # noqa: F401  arrow keys and history at the prompt, where available
    except ImportError:
        pass
    console = Console()
    load_dotenv(find_dotenv(usecwd=True))
    args = parse_args()
    api_key = os.environ.get("FIREWORKS_API_KEY")
    if not api_key:
        console.print("[red]FIREWORKS_API_KEY is not set.[/] Add it to .env or export it.")
        raise SystemExit(1)

    cfg = Config(model=args.model, mode=args.mode)
    engine = db.connect(args.db_url)
    db.ping(engine)
    if not db.is_guarded(engine):
        console.print(f"[yellow]Warning:[/] {db.dialect(engine)} gets no read-only session or query time limit here. "
                      "Connect as a user with only SELECT rights.")
    schemas, docs_dir = tuple(args.schema), Path(args.docs_dir)
    status = "Reading the database schema (cached after this)..."
    if args.refresh_schema or not cache_path(engine, docs_dir=docs_dir, schemas=schemas).exists():
        with console.status(status):
            tables = load_tables(engine, refresh=True, docs_dir=docs_dir, schemas=schemas)
    else:
        tables = load_tables(engine, docs_dir=docs_dir, schemas=schemas)
    ask = make_session(cfg, engine, tables, api_key)
    console.print(render_banner(cfg, db.dialect(engine), len(tables)))

    history: tuple[Turn, ...] = ()
    while True:
        try:
            question = console.input("\n[bold cyan]ask>[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            break
        if not question:
            continue
        if question.lower() in {"exit", "quit"}:
            break
        if question == ":help":
            console.print(HELP)
            continue
        if question == ":reset":
            history, ask = (), make_session(cfg, engine, tables, api_key)
            console.print("[green]Conversation cleared.[/]")
            continue

        try:
            with console.status("Thinking..." if cfg.mode == "chat" else "Working: writing and testing the query..."):
                turn = ask(question, history)
        except FireworksError as exc:
            console.print(f"[red]Error talking to Fireworks:[/] {exc}")
            continue
        except KeyboardInterrupt:
            console.print("\n[yellow]Cancelled.[/]")
            continue

        console.print(render_turn(turn, cfg.model))
        if turn.ok:
            history = (*history, turn)

    engine.dispose()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="cli", description="Ask questions about the bank database in plain English.", allow_abbrev=False)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--chat", dest="mode", action="store_const", const="chat",
                      help="chat mode (v0.1): one model call per question, with the full schema briefing")
    mode.add_argument("--agent", dest="mode", action="store_const", const="agent",
                      help="agent mode (v0.2): a Strands agent that tests its SQL before answering")
    parser.set_defaults(mode=os.environ.get("FIREWORKS_MODE", "chat"))
    parser.add_argument("--db-url", default=os.environ.get("DATABASE_URL", db.DEFAULT_URL),
                        help="SQLAlchemy URL of the database, e.g. "
                        "postgresql+psycopg://user:pass@host/db. Default: $DATABASE_URL or the bundled financial.sqlite.")
    parser.add_argument("--schema", action="append", default=[], metavar="NAME",
                        help="Schema to read; repeat for several. Default: the database's default schema (e.g. public).")
    parser.add_argument("--docs-dir", default=str(DOCS_DIR), metavar="DIR",
                        help="Column notes: one CSV per table (loan.csv or sales.orders.csv) with the columns of "
                        "data/database_description/. Default: %(default)s.")
    parser.add_argument("--refresh-schema", action="store_true", help="Re-read the database schema instead of using the cache.")
    parser.add_argument("--model", default=os.environ.get("FIREWORKS_MODEL", DEFAULT_MODEL),
                        help="Fireworks model id. Default: $FIREWORKS_MODEL or %(default)s.")
    args = parser.parse_args(argv)
    if not Path(args.docs_dir).is_dir():
        parser.error(f"--docs-dir {args.docs_dir} is not a directory")
    if args.mode not in MODES:
        parser.error(f"FIREWORKS_MODE must be one of {', '.join(MODES)}, not {args.mode!r}")
    return args


def render_banner(cfg: Config, dialect: str, n_tables: int) -> RenderableType:
    title = Text.assemble(("FireSQL", "bold"), "  ·  ", (cfg.mode, "bold cyan"), " mode  ·  ",
                          (dialect, "bold"), f" ({n_tables} tables)  ·  ", (short_model(cfg.model), "bold"))
    return Panel(Text.from_markup(HELP), title=title, title_align="left", border_style="cyan", expand=False)


def render_turn(turn: Turn, model: str) -> RenderableType:
    if turn.declined:  # not a question about the data: no SQL ran
        return Group(Text(), Text(f"{turn.error}. Try asking about the data.", style="yellow"),
                     Text(format_stats(turn, model), style="dim"))
    sql = Syntax(turn.sql or "(none)", "sql", theme=SQL_THEME, word_wrap=True, background_color="default")
    body = Text(f"Error: {turn.error}", style="red") if turn.error else format_table(turn.columns, turn.rows, turn.truncated)
    return Group(Text(), Panel(sql, title="SQL", title_align="left", border_style="dim", expand=False), body,
                 Text(format_stats(turn, model), style="dim"))


def format_stats(turn: Turn, model: str) -> str:
    u = turn.usage
    cached = f", {u.cached_tokens:,} cached" if u.cached_tokens else ""
    if turn.tool_calls:  # agent mode: attempts counts model calls
        attempts = f"{turn.attempts} model calls, {turn.tool_calls} tool call{'s' if turn.tool_calls != 1 else ''}"
    else:
        attempts = "1 attempt" if turn.attempts == 1 else f"{turn.attempts} attempts"
    return (
        f"{turn.total_s:.2f}s total: model {turn.llm_s:.2f}s, sql {turn.sql_s:.2f}s · "
        f"{u.prompt_tokens:,} in{cached} / {u.completion_tokens:,} out tokens · {attempts} · {short_model(model)}"
    )


def format_table(columns: tuple[str, ...], rows: tuple[tuple, ...], truncated: bool = False) -> RenderableType:
    if not columns:
        return Text("(no columns returned)", style="dim")
    if not rows:
        return Text("(no rows)", style="dim")
    shown = rows[:MAX_SHOWN_ROWS]
    numeric = [all(isinstance(r[i], (int, float)) or r[i] is None for r in shown) for i in range(len(columns))]
    hidden = len(rows) - len(shown)
    more = f" · {hidden:,}{'+' if truncated else ''} more not shown" if hidden > 0 or truncated else ""
    table = Table(header_style="bold")
    for name, is_number in zip(columns, numeric):
        table.add_column(str(name), justify="right" if is_number else "left", overflow="ellipsis", max_width=MAX_CELL_WIDTH)
    for row in shown:
        table.add_row(*(Text("NULL", style="dim") if v is None else Text(format_cell(v)) for v in row))
    count = f"{len(rows):,}{'+' if truncated else ''} row{'s' if len(rows) != 1 else ''}{more}"
    return Group(table, Text(count, style="dim"))


def format_cell(value: object) -> str:
    return f"{value:,.2f}" if isinstance(value, float) else str(value)


def short_model(model: str) -> str:
    return model.rsplit("/", 1)[-1]


if __name__ == "__main__":
    main()
