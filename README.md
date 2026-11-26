# FireSql, a text2sql agent on Fireworks AI

## Objective

An accurate, low-latency, low-cost text-to-SQL CLI built on open models served by Fireworks AI.

## Goals

1. **Accurate:** SQL grounded in the real schema and stored values, with no invented tables or columns, measured by execution accuracy.
2. **Fast:** under 3s P50 end to end, so the CLI feels interactive.
3. **Low cost:** viable at about 30,000 queries a day on open models served by Fireworks AI.
4. **Conversational:** ask in plain English, see the SQL and the results, ask follow-ups, and leave with `exit` or `quit`.
5. **Safe:** read-only on any SQL database, with a single guarded `SELECT` per answer.
6. **Measured:** every mode is scored against a naive one-line prompt on accuracy, latency and cost, and the remaining failures are documented.

## How it works

![firetext2sql architecture: CLI, chat mode (chat.py) and agent mode (agent.py), SQL guard, Fireworks, read-only SQL database](docs/architecture.png)

You type a question in plain English, and the CLI turns it into SQL, runs it read-only on the database, and shows the query and the results. Follow-up questions build on the conversation. It's a proof of concept that open models on Fireworks beat closed-source models, under 3s P50 end to end. It works on SQLite, PostgreSQL, MySQL and MariaDB; the sample database is `financial.sqlite`, a Czech retail bank with 8 tables from the BIRD benchmark.

Both modes share a briefing built once at startup (schema, column notes from `data/database_description/`, and the real stored values of coded columns such as `'POPLATEK TYDNE'` = weekly), a read-only database with a SQL guard, and a 15s query limit. Each version is a mode:

1. **`chat` (v0.1).** One Fireworks call with the full briefing, the last 3 turns and the question, answered as `{reasoning, sql}`. If the SQL fails, the error goes back to the model once.
2. **`agent` (v0.2).** A [Strands Agents](https://strandsagents.com) ([GitHub](https://github.com/strands-agents/harness-sdk)) agent with the full briefing (a compact outline when the schema is too large) and read-only tools: list tables, describe a table, sample values, and test a query. It tests its SQL, then answers as `{reasoning, sql}`, and the CLI runs that query. Follow-up questions also see the previous result's rows.

## Capabilities

1. **Two modes.** `--chat` makes one model call per question and is the fastest and cheapest. `--agent` tests its query on the database before answering and is the most accurate (90% against 86% on the golden set). See [reports.md](docs/reports.md).
2. **Conversations.** Follow-up questions build on earlier ones ("only the ones from 1997", "the third one"). `:reset` starts over.
3. **Any Fireworks model.** `--model` or `FIREWORKS_MODEL` takes any Fireworks chat model ID. The default is `glm-5p3-flash`, the cheapest that met the accuracy target. Agent mode needs a model with tool calling.
4. **Any SQL database, through [SQLAlchemy](https://docs.sqlalchemy.org/en/20/dialects/).** `--db-url` takes a SQLAlchemy URL. SQLite, PostgreSQL, MySQL and MariaDB are built in, with read-only sessions and a 15s query limit. Other databases (Oracle, SQL Server, Snowflake, ...) work once their dialect package is installed, through a `SELECT`-only user. `--schema` adds schemas, and views are included.
5. **Grounded in your data.** The model sees tables, keys, row counts and the real stored values of coded columns. Column notes come from CSVs you supply with `--docs-dir`, or from comments stored in the database. The schema is read once and cached.
6. **Safe by default.** Only a single read-only `SELECT` runs, with a 15s limit and at most 1,000 rows. Every query, including the agent's test queries, goes through the same guard.
7. **Transparent answers.** Every answer shows the SQL, the rows, and a stats line with time, tokens, cache hits and tool calls.
8. **Built-in evals.** Three question sets (10 dev, 200 golden, 46 follow-up turns) score every mode against a naive one-line prompt on accuracy, P50/P95 latency and cost. `--rescore` re-grades saved runs for free, and `src/golden.py` generates new verified question sets.

## Docs

1. [docs/solution.md](docs/solution.md) — How it works (one architecture diagram for both modes), design decisions, how the Fireworks platform is used, the evaluation method, results, limitations, and appendices on a Decider router, the BIRD benchmark and fine-tuning
2. [docs/tradeoffs.md](docs/tradeoffs.md) — Answers to the main design questions: error repair, schema grounding, architectures, open vs. proprietary models, follow-ups, latency/cost, fine-tuning
3. [docs/reports.md](docs/reports.md) — Eval runs and key metrics (accuracy, latency, cost) per version
4. [docs/findings.md](docs/findings.md) — Key findings from the evals, one numbered point each
5. [docs/data.md](docs/data.md) — The `financial` database: table sizes, how the tables join, coded values and gotchas

## Prerequisites

1. [uv](https://github.com/astral-sh/uv) (it installs Python 3.11+ if needed)
2. `curl`, `unzip`, `sqlite3`, and `shasum` or `sha256sum` (used by `setup.sh`)
3. A Fireworks AI API key

## Setup

```bash
./setup.sh
```

This downloads the BIRD dev bundle, verifies its checksum, installs `data/financial.sqlite` and `data/database_description/`, and runs `uv sync` to create `.venv`.

## Environment variables

1. `FIREWORKS_API_KEY` (required) — Fireworks API key
2. `FIREWORKS_MODEL` (optional, default `accounts/fireworks/models/glm-5p3-flash`) — Any Fireworks chat model ID
3. `FIREWORKS_MODE` (optional, default `chat`) — `chat` (v0.1) or `agent` (v0.2)
4. `DATABASE_URL` (optional, default the bundled `data/financial.sqlite`) — the database to query, as a SQLAlchemy URL (same as `--db-url`)

Set them in a `.env` file in the project root, which the CLI loads automatically and git ignores:

```bash
echo 'FIREWORKS_API_KEY=<your-key>' > .env
echo 'FIREWORKS_MODEL=accounts/fireworks/models/glm-5p3-flash' >> .env   # optional; this is the default
echo 'FIREWORKS_MODE=agent' >> .env                                       # optional; default chat
```

or export them in your shell.

## Run

Each version is a mode of the same CLI:

```bash
uv run cli --chat    # v0.1: one model call with the full schema briefing
uv run cli --agent   # v0.2: a Strands agent that tests its SQL before answering
```

or, with the environment activated, `python -m src.cli --agent`. `--model` overrides the model.

**CLI options** (`uv run cli --help`):

| Option | What it does | Default | Example |
|---|---|---|---|
| `--chat` | Chat mode: one model call per question | yes (or `$FIREWORKS_MODE`) | `uv run cli --chat` |
| `--agent` | Agent mode: tests its SQL before answering | | `uv run cli --agent` |
| `--db-url URL` | Database to query, as a SQLAlchemy URL | `$DATABASE_URL`, else bundled `data/financial.sqlite` | `uv run cli --db-url "postgresql+psycopg://reader:<pw>@host/bank"` |
| `--schema NAME` | Schema to read; repeat for several | the database's default (e.g. `public`) | `uv run cli --db-url "$URL" --schema public --schema sales` |
| `--docs-dir DIR` | Column notes: one CSV per table, same columns as `data/database_description/` | `data/database_description/` | `uv run cli --db-url "$URL" --docs-dir notes/` |
| `--refresh-schema` | Re-read the schema instead of using the `.cache/` copy | off | `uv run cli --refresh-schema` |
| `--model ID` | Fireworks model ID (agent mode needs tool calling) | `$FIREWORKS_MODEL`, else `glm-5p3-flash` | `uv run cli --model accounts/fireworks/models/kimi-k3` |

Inside a session: `:reset` forgets the conversation, `:help` shows help, and `exit` or `quit` leaves.

To query another database, pass its [SQLAlchemy URL](https://docs.sqlalchemy.org/en/20/core/engines.html#database-urls). SQLite, PostgreSQL, MySQL and MariaDB work out of the box; any other database with an installed SQLAlchemy dialect (Oracle, SQL Server, Snowflake, ...) also connects, with a warning:

```bash
uv run cli --agent --db-url "postgresql+psycopg://reader:<password>@host:5432/bank"
uv run cli --db-url "mysql+pymysql://reader:<password>@host:3306/bank"     # MariaDB: mariadb+pymysql://...
uv run cli --db-url "sqlite:///path/to/other.sqlite"
uv run cli --db-url "$URL" --schema public --schema sales --docs-dir notes/   # more schemas, your column notes
```

1. **Safety:** on the four built-in databases every session is read-only and each query stops after 15s. Other databases get neither, so connect as a user with only `SELECT` rights (in production, do that everywhere).
2. **What the model sees:** tables and views in the default schema, or in each `--schema`, with keys, row counts (estimates on PostgreSQL and MySQL) and every value of low-cardinality text columns.
3. **Column notes:** `--docs-dir DIR` reads one CSV per table (`orders.csv`, or `sales.orders.csv` outside the default schema) with the columns of `data/database_description/` (`original_column_name, column_name, column_description, data_format, value_description`). Without a CSV, comments stored in the database (`COMMENT ON`) are used. This is how you add what codes mean, such as `'POPLATEK TYDNE'` = weekly.
4. **Startup:** the first connection reads the schema in parallel, about 1s on the bundled file, and each inspection query stops after 10s, so a huge table loses its value list rather than blocking startup. The result is cached in `.cache/`; pass `--refresh-schema` after the database changes.

The eval always runs on the bundled SQLite file, because its gold SQL is written for SQLite.

To run the eval, which compares the baseline prompt and both modes:

```bash
uv run python -m src.eval --set dev      # 10 given dev questions, about $0.01
uv run python -m src.eval --set golden   # 200 synthetic questions, about $0.12
uv run python -m src.eval --set followup # 15 multi-turn conversations (46 turns), about $0.03
uv run python -m src.eval --set golden --modes agent   # one mode only; other modes' saved results are kept
uv run python -m src.eval --set golden --rescore   # re-grade a saved report locally, no model calls
```

Other eval options: `--set all` runs every set, `--limit N` only the first N questions, `--workers N` parallel requests, `--model ID` the model under test, and `--answers-from MODE` picks whose dev answers go to `eval/dev_answers.json`.

Reading the results:

1. **Terminal:** one line per mode with accuracy, P50/P95 latency, cost per query and tool calls (plus per-turn and whole-conversation scores on the follow-up set).
2. **`eval/report_<set>.json`:** the record per question and mode: SQL, answer, correct or not, error, tokens, cost and timings.
3. **`eval/dev_answers.json`:** chat mode's answers to the 10 dev questions, each with its SQL and a readable answer.
4. **[docs/reports.md](docs/reports.md):** the written summary: success metrics, a scorecard per mode, the dev answers and the version history.

To run the unit tests (offline; they never call the model):

```bash
uv run pytest
```

Inside the CLI:

1. a question — Generates and runs SQL, then shows the query, the results and a stats line: total, model and SQL time, tokens (cached), and model and tool calls in agent mode
2. a follow-up — Builds on the conversation; in agent mode it also sees the previous result's rows
3. `:reset` — Clears the conversation
4. `:help` — Shows help
5. `exit`, `quit`, Ctrl-D — Ends the session

## Code

1. `src/cli.py` — The REPL loop, drawn with [rich](https://github.com/Textualize/rich): highlighted SQL, result tables, a progress spinner and a stats line
2. `src/chat.py` — Chat mode (v0.1): prompt, one LLM call, guard, run, repair once. Also `Config`, `Turn`, the SQL guard and `make_session` (one interface over both modes)
3. `src/agent.py` — Agent mode (v0.2): the Strands agent, its read-only schema tools, and loop limits
4. `src/context.py` — Builds the briefing from the schema, column docs and sampled values
5. `src/db.py` — The only module that opens database connections: SQLAlchemy, read-only sessions and a per-query time limit for SQLite, PostgreSQL, MySQL and MariaDB
6. `src/fireworks.py` — A minimal Fireworks chat-completions client (standard library only)
7. `src/eval.py` — Runs the baseline and each mode on the dev, golden or follow-up questions; writes `eval/report_<set>.json` and `eval/dev_answers.json`
8. `src/golden.py` — Generates the synthetic golden set (questions first, then SQL verified by agreement) into `eval/golden_questions.json`
9. `src/utils.py` — Project paths and the provided SQLite and column-doc helpers
10. `tests/` — Offline unit tests
11. `data/` — The given dataset: the database, column-description CSVs and the 10 dev questions (never modified)
12. `eval/` — The golden question set and eval reports

## License

The code is licensed under the [Apache License 2.0](LICENSE). The `financial` dataset comes from BIRD and has its own terms. See [BIRD_LICENSE.md](BIRD_LICENSE.md).
