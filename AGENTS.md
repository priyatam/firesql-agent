# AGENTS.md

How to work in this repo. Keep it simple.

## Style

1. Write pure functions. Pass data in, return data out.
2. Use frozen Pydantic models for all data, including JSON and API payloads; no bare dicts in the core. Don't mutate; return a new value with `model_copy(update=...)`.
3. Keep I/O at the edges. Only `fireworks.py` and the Strands model in `agent.py` touch the network, only `db.py` opens database connections (read-only; SQLite, PostgreSQL, MySQL, MariaDB), and only `cli.py` touches the terminal.
4. Prefer the standard library. Add a dependency only when it removes real code.
5. Small modules, short functions, plain names. The only classes are frozen Pydantic models and exception types: no classes with behavior or mutable state, no inheritance. Share results by returning them (e.g. return `Usage` with the result), never through shared mutable objects. The only framework is Strands, and only for agent mode (`src/agent.py`).
6. Put constants at the top of the module. Pass settings through `Config`, not globals or env lookups deep in the code.
7. Fail loudly at the edges with a clear message. Don't swallow errors in the core.

## Architecture

Follow the agent-mode design decisions in `docs/solution.md`.

## Workflow

1. Run `uv run cli --chat` or `uv run cli --agent` and `uv run python -m src.eval --set dev|golden`. Each version is a mode.
2. Before claiming a change helps, run the eval and compare accuracy, P50/P95 latency and cost with the previous version.
3. After a change, update the matching docs: results in `docs/reports.md` under the version heading, reasoning in `docs/tradeoffs.md`, and design in `docs/solution.md`.
4. Run `uv run pytest` before calling work done. Tests never reach the model; inject it.
5. Lint with `uvx ruff check src`.
6. Never modify or add files in `data/`; it is the given dataset. Generated sets go in `eval/`.
7. Never commit, branch or push unless asked. Never commit `.env` or `data/financial.sqlite`.
