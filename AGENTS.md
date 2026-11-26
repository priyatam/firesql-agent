# AGENTS.md

How to work in this repo. Keep it simple.

## Style

1. Write pure functions. Pass data in, return data out.
2. Use frozen dataclasses (or frozen Pydantic models) for data. Don't mutate; return a new value with `dataclasses.replace`.
3. Keep I/O at the edges. Only `fireworks.py` touches the network, only `agent.py` and `context.py` touch SQLite, and only `cli.py` touches the terminal.
4. Prefer the standard library. Add a dependency only when it removes real code.
5. Small modules, short functions, plain names. No classes for behavior, no frameworks, no inheritance.
6. Put constants at the top of the module. Pass settings through `Config`, not globals or env lookups deep in the code.
7. Fail loudly at the edges with a clear message. Don't swallow errors in the core.

## Architecture

Follow the architecture principles for the current version in `docs/solution.md`.

## Workflow

1. Run from the project root: `uv run cli` and `uv run python -m src.eval`.
2. Before claiming a change helps, run the eval and compare accuracy, P50/P95 latency and cost with the previous version.
3. After a change, update the matching docs: results in `docs/reports.md` under the version heading, reasoning in `docs/design-considerations.md`, and design in `docs/solution.md`.
4. Lint with `uvx ruff check src`.
5. Never commit, branch or push unless asked. Never commit `.env` or `data/financial.sqlite`.
