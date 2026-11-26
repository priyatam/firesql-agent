# firetext2sql

## Objective

An interactive CLI that turns plain-English questions into SQL, runs them on a database and shows the query and the results. It's a proof of concept that open-source models on Fireworks AI can beat a naive baseline prompt (`Convert this question to SQL: {question}`) on quality and reliability, with a target of under 3s P50 end to end. The sample database is `financial.sqlite`, a Czech retail bank with 8 tables from the BIRD benchmark.

## How it works

1. **Briefing (once, at startup).** Reads the schema from the database, adds the column notes from `data/database_description/`, and lists the real stored values of coded columns (for example `'POPLATEK TYDNE'` = weekly). The result is one static system prompt, which Fireworks can cache.
2. **Generate.** One Fireworks chat call with the briefing, the last 3 turns of the conversation and the question. Structured output forces the reply into `{reasoning, sql}`.
3. **Guard.** Accepts only `SELECT`/`WITH` queries. The database is opened read-only, and queries time out after 15s.
4. **Run and repair.** Runs the SQL. If the reply is unusable or SQLite raises an error, the error goes back to the model for one corrected attempt.
5. **Show.** Prints the SQL, a results table, and timings and token counts. Follow-ups build on earlier turns.

## Docs

2. [docs/solution.md](docs/solution.md) — How it works (data-flow diagram), evaluation method, latency and cost levers, fine-tuning roadmap
3. [docs/design-considerations.md](docs/design-considerations.md) — Answers to the main design questions: error repair, schema grounding, architectures, open vs. proprietary models, follow-ups, latency/cost, fine-tuning
4. [docs/reports.md](docs/reports.md) — Eval results: accuracy, latency and cost per version, and a verdict against each target
5. [docs/data.md](docs/data.md) — The `financial` database: table sizes, how the tables join, coded values and gotchas
6. [docs/bird-primer.md](docs/bird-primer.md) — A short summary of the BIRD paper and what its findings mean for this prototype

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

Set them in a `.env` file in the project root, which the CLI loads automatically and git ignores:

```bash
echo 'FIREWORKS_API_KEY=<your-key>' > .env
echo 'FIREWORKS_MODEL=accounts/fireworks/models/glm-5p3-flash' >> .env   # optional; this is the default
```

or export them in your shell.

## Run

From the project root:

```bash
uv run cli
```

or, with the environment activated:

```bash
source .venv/bin/activate
python -m src.cli
```

To run the eval (about 30 model calls, well under a cent):

```bash
uv run python -m src.eval
```

Inside the CLI:

1. a question — Generates and runs SQL, then shows the query, results and stats
2. a follow-up — Builds on the last 3 successful turns
3. `:reset` — Clears the conversation
4. `:help` — Shows help
5. `exit`, `quit`, Ctrl-D — Ends the session

## Code

1. `src/cli.py` — The REPL loop and table rendering
2. `src/agent.py` — The pipeline: prompt, generate, guard, run, repair once
3. `src/context.py` — Builds the briefing from the schema, column docs and sampled values
4. `src/fireworks.py` — A minimal Fireworks chat-completions client (standard library only)
5. `src/eval.py` — Runs the baseline and the agent on the dev questions; writes `eval/report.json` and `dev_answers.json`
6. `src/utils.py` — Database and column-doc helpers
7. `data/` — The database, column-description CSVs and the 10 dev questions with gold answers

## License

The code is licensed under the [Apache License 2.0](LICENSE). The `financial` dataset comes from BIRD and has its own terms. See [BIRD_LICENSE.md](BIRD_LICENSE.md).
