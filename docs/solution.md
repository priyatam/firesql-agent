# Solution: Text-to-SQL Agent

## Objective

An accurate, low-latency, low-cost text-to-SQL CLI built on open models served by Fireworks AI.

## How it works

![firetext2sql architecture: CLI, chat mode (chat.py) and agent mode (harness.py), SQL guard, Fireworks, read-only SQLite](architecture.png)

Each version is a mode of the same CLI: `uv run cli --chat` or `uv run cli --agent`. Both modes share the CLI, the briefing from `context.py`, the SQL guard, the read-only database and the Fireworks model. The diagram source is [architecture.dot](architecture.dot); re-render with `dot -Tpng -Gdpi=200 docs/architecture.dot -o docs/architecture.png`.

### Chat mode (v0.1)

At startup, `context.py` builds a full briefing of every table and column, the column notes, and the real values of coded columns, so the model writes `'POPLATEK TYDNE'` rather than inventing `'weekly'`. For each question, `chat.py` sends that cached briefing, the last 3 turns and the question in one Fireworks call. It gets back `{reasoning, sql}`, checks that the SQL is a single read-only `SELECT`, runs it with a 15s limit, and retries once with the error if it fails. **Results:** 86% on 200 golden questions and 10/10 on dev, against 8% for a naive baseline prompt, at a 1.07s P50 and about $4 a day at 30,000 queries. This is the default mode. Details are in [reports.md](reports.md#scorecard-version-02).

### Agent mode (v0.2)

`harness.py` runs a [Strands Agents](https://strandsagents.com) ([GitHub](https://github.com/strands-agents/harness-sdk)) agent on the same Fireworks model. Its cached prompt holds the full briefing while the schema fits (under 40k characters), and a compact outline otherwise. For each question it writes the query, tests it once with `run_sql`, and calls `describe_table` or `sample_values` only when something it needs isn't shown. A rule stops it adding conditions or columns the question didn't ask for. It then answers with `{reasoning, sql}`, which the harness guards and runs itself. Each question gets at most 8 model calls, and the agent keeps the conversation until `:reset`. A follow-up question also carries the previous result's rows (up to 10), so "the third one" or "that year" resolves to the value the user saw. **Results:** 90% on the golden set (chat: 86%) and 46/46 follow-up turns (chat: 44/46), at a 1.80s P50 and about $9 a day, with about 2 tool calls per question. It is the most accurate mode, and the base for databases too large for one prompt. Details are in [reports.md](reports.md#scorecard-version-02).

**Design decisions:**

1. **Grounding beats model size.** Accuracy comes from context: the schema, what each column means, and the real stored values. While the schema fits, all of it sits in the agent's cached prompt. Beyond that, the agent fetches it table by table with tools, so the same design scales to large databases.
2. **Verify by execution, within a budget.** The agent tests its query on the real database before answering and fixes it if it fails or looks wrong. A hard cap of 8 model calls keeps latency and cost bounded.
3. **The model proposes; deterministic code executes.** The agent only returns `{reasoning, sql}`. The harness checks it is a single read-only `SELECT` and runs it on a read-only connection with a 15s limit, so the model never has write access and every mode returns the same result shape.
4. **Conversation is state, including results.** The agent keeps the conversation until `:reset`, and each follow-up carries the previous result's rows, so "the third one" resolves to what the user saw.
5. **Fast and cheap by construction.** An open model on Fireworks serverless, a stable prompt prefix that is 84–94% cached under session affinity, and a schema scanned once and cached at startup.
6. **One interface over every mode.** Chat and agent sit behind `make_session`, so the CLI, the eval and a future router that picks a mode per question share it.

## Fireworks platform

Everything runs on [Fireworks AI](https://fireworks.ai) serverless inference, through its OpenAI-compatible endpoint (`https://api.fireworks.ai/inference/v1`).

1. **CLI and evals:** `glm-5p3-flash` ($0.15 input, $0.03 cached, $0.50 output per 1M tokens). Chat mode calls it with `src/fireworks.py`, a standard-library client. Agent mode calls it through Strands' `OpenAIModel`, pointed at the same endpoint, using Fireworks tool calling.
2. **Structured output:** both modes request JSON-schema output, so every reply is `{reasoning, sql}`; `reasoning_effort="low"` keeps output short.
3. **Prompt caching:** the briefing is a fixed prefix, and an `x-session-affinity` header per database and mode keeps parallel sessions on one warm replica. 84–94% of input tokens are served from cache at a fifth of the input price.
4. **Golden set:** `src/golden.py` used `kimi-k3` ($3 / $15 per 1M) as the teacher to write 289 questions and two or three SQL samples each; it cost about $11. The default teacher is now `glm-5p3-flash`.
5. **Evals:** a full run of every mode on all three sets is about 1,000 calls and costs about $0.15. At 8 parallel workers the agent's extra requests hit serverless throttling bursts, so the agent runs with 4.
6. **Next on Fireworks:** a dedicated deployment if the P95 needs to hold under load, and LoRA fine-tuning (see the roadmap below).

## Evaluation

**Question sets.**

1. **Dev (10, from BIRD).** `data/dev_questions_with_answers.json`, three tiers, each with gold SQL and the expected result. We run every gold query first to confirm the answer key.
2. **Golden (200, synthetic).** `eval/golden_questions.json`, generated by `golden.py` with `kimi-k3`. The teacher first wrote questions from the briefing, with no SQL. It then wrote SQL for each question two or three times, and a question was kept only if the queries ran and agreed. Ambiguous questions disagree, so they drop out. Questions close to the dev set were removed, so the two sets don't overlap. Tiers come from the gold SQL by a fixed rule.
3. **Follow-ups (15 conversations, 46 turns, hand-written).** `eval/followup_questions.json`. Each conversation has 3–4 turns at medium level (refine a filter, add a split) or complex level (refer to an earlier result: "those five districts", "the third one", "that year"). Every gold query was run and checked for ties. Each conversation runs in one session with the model's own earlier answers as history, as in the CLI. We score every turn, and whole conversations.

**Metric: execution accuracy (as in BIRD).** We compare results, not query text. A prediction is correct when its rows match the gold query's rows. Column names are ignored. Row order is ignored unless the gold query sorts its final result (an `ORDER BY` outside any subquery), and even then tied rows may come in any order. Numbers are rounded before comparing. A query that errors counts as wrong.

**Modes compared, on the same questions:**

1. **baseline:** the naive prompt, `Convert this question to SQL: {question}`, with no schema;
2. **chat** (v0.1);
3. **agent** (v0.2);
4. **agent-no-rows:** agent mode without the previous result's rows, on the follow-up set only.

For each mode we report accuracy (overall and per tier), P50/P95 latency, tool calls, and cost per query projected to 30k queries a day. Run it with `uv run python -m src.eval --set dev|golden|followup`; add `--rescore` to re-grade a saved report locally without model calls. Per-question records go to `eval/report_<set>.json`, and the dev answers to `dev_answers.json`.

## Results

Measured accuracy, latency and cost per mode are in [reports.md](reports.md).

## Latency and cost

```
latency = model calls × (time to first token + output tokens ÷ speed) + tool and SQL time
cost    = (uncached input + discounted cached input + output), summed over model calls
```

The levers:

1. **Fewest model calls.** Chat uses one on the happy path. The agent uses more, so it pays only when the extra calls buy accuracy.
2. **A cached prefix.** The briefing or outline comes first and is identical every time, and session affinity keeps concurrent requests on one warm cache.
3. **Short output.** Low reasoning effort and a one-line `reasoning` field.
4. **A bounded worst case.** One repair in chat mode; 8 model calls in agent mode; a 15s SQL limit.
5. **Model choice.** Driven by the eval: the cheapest model that meets the accuracy target.

Every answer shows its own timings and token counts. The measured numbers are in [reports.md](reports.md).

## Fine-tuning roadmap

1. **Data:** the golden-set pipeline already produces execution-verified question/SQL pairs; add real CLI questions with user corrections.
2. **SFT with LoRA** on a medium open model, for example Qwen3-30B-A3B.
3. **RL** with a "results match gold" reward, starting from the SFT model.
4. **When it's worth it:** when the eval shows recurring domain errors that better briefing or tools don't fix, or when a smaller tuned model matches accuracy at lower latency and cost.

Fine-tuning numbers are estimates. Measured latency, cost and accuracy are in [reports.md](reports.md).

## Appendix A: Adding the Decider model

Implement mode `routed`. Decider scores the question; the score picks the Fireworks model. Decider does not write SQL. Serve it beside the CLI (`strands-decider serve StrandsAgents/strands-decider-E2B-gemma4-v2-2611 --port 8099`). Fireworks cannot host it.

1. Add `"routed"` to `MODES` in `src/chat.py`. The CLI and eval take it from there.
2. Add `src/router.py`. One function, `route(question, history, outline) -> Route`. `Route` is a frozen model: `model`, `mode` (`chat` or `agent`), `max_turns`. The HTTP call is the only I/O in this module. Tests inject it.
3. `POST {STRANDS_DECIDER_URL}/v1/systemone`, default `http://127.0.0.1:8099`. State: the question, the last 3 question/SQL pairs, and the schema outline. One question, `complexity`, type `score`, criteria in order: `easy` (one table, a filter or a count), `medium` (one join, a date filter, or a grouped aggregate), `hard` (several joins, `COUNT(DISTINCT …)` through `disp`, or district columns `A2`–`A16`).
4. Read `answers.complexity.score` (0, 1, or 2) and `answers.complexity.confidence`. Round the score to the nearest level. Level 2 and confidence ≥ 0.9 → `accounts/fireworks/models/kimi-k3`, mode `agent`, `max_turns` 4. Every other case, including an unreachable server or a body without that score → `accounts/fireworks/models/glm-5p3-flash`, mode `chat`.
5. In `make_session`, when `cfg.mode == "routed"`, call `route` on each question, `model_copy` the config, then the existing chat or agent path. Build the Strands agent per question, because the model can change.
6. Tests must not call the server. Run `uv run pytest` and `uvx ruff check src`.

## Appendix B: the BIRD benchmark

**Paper:** *Can LLM Already Serve as a Database Interface? A BIg Bench for Large-Scale Database Grounded Text-to-SQLs*, Li et al., NeurIPS 2023 ([bird-paper.pdf](bird-paper.pdf)).

BIRD is a text-to-SQL benchmark: 12,751 question/SQL pairs over 95 real databases from 37 domains, with cryptic column names, coded values, dates stored as text and millions of rows. `financial.sqlite` is one of its databases.

**Main idea.** Correct SQL needs the question, the database (tables, columns, real values), and external knowledge linking words to values, for example "weekly" = `'POPLATEK TYDNE'`. 70% of BIRD's questions need that knowledge of what stored values mean.

**Key findings** (2023 models; use the relative lessons, not the scores):

1. Real databases are much harder than clean ones: models scored 58–83% on Spider but 12–51% on BIRD.
2. External knowledge is the biggest lever. It raised every model by 5–20 points; GPT-4 went from 35% to 55%.
3. Prompting technique beats raw model power. The best score was DIN-SQL with GPT-4, which used sample values, worked examples and self-correction.
4. The best model was far behind humans: 55% against 93%.
5. Of 500 ChatGPT errors, 42% chose the wrong table or column, 41% misread the database (including made-up names), and 18% misused the knowledge hints.
6. Query speed matters too: two correct queries in one example took 22.4s and 4.0s.

**Metrics.** Execution accuracy checks whether the predicted query returns the same rows as the gold query, which is the metric we use. The Valid Efficiency Score adds credit for correct queries that also run fast.

## Limitations

A fresh run on 2026-11-25 (Pacific) matched the published story and moved the exact scores by a point or two. Dev was again 0/10, 10/10, and 9/10, with the agent's miss still `q_008` (owners counted by the client's district, while the answer key uses the account's). Golden was 7% / 86% / 89% for baseline, chat, and agent, against 8% / 86% / 90% in [reports.md](reports.md). Chat P50 was 1.05s and about $4 a day; the agent was 1.90s and about $9. Every target still held. The agent P95 was 4.58s, against 3.25s published. Follow-ups were 43/46 for chat and 46/46 for the agent. The run without previous result rows was also 46/46, so the claim that those rows are what makes follow-ups perfect did not reproduce.

What a team shipping this to its own developers would hit:

1. **One database.** The brief is a CLI pointed at any database with a connection string. This proof is one SQLite file, the Czech bank schema, and the column CSVs shipped with it. Chat's accuracy depends on putting that whole briefing in the prompt. The agent is the path for a larger schema, and on this small one it is only about three points ahead, at twice the cost.
2. **Wrong SQL that runs.** Chat produced runnable SQL for every golden question and was still wrong on 27 of 200. The CLI prints that SQL and those rows as the answer. At 30,000 queries a day that is thousands of confident wrong answers.
3. **The answer key is a model's.** The golden questions and their SQL were written and filtered by a model. The 86–89% is agreement with that key, not with an analyst.
4. **The tail moves.** The same agent was 1.80s P50 in the write-up and 1.90s here. Eight parallel agent calls already blew the tail out to tens of seconds. A dedicated deployment is still an assumption, not a measurement.
