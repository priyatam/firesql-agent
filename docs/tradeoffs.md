# Tradeoffs

Answers to the main design questions behind a text-to-SQL agent. Each heading is one question. Each answer gives what the code does today, what else was considered, and why. The **Update when** line at the end of each section says which code change will add measured numbers or change the answer. Measured numbers come from [reports.md](reports.md); anything without a **Measured** note is an estimate.

## 1. What happens when the generated SQL doesn't execute?

**Today:** one repair attempt (`Config.max_repairs = 1`). If the reply is unusable (bad JSON, cut off, empty, more than one statement, not read-only) or SQLite raises an error (bad column, syntax, 15s timeout), the model's reply and the error message go back as a follow-up, and it tries once more. If that also fails, the CLI shows the error and the last SQL. It never shows made-up results.

| Option | Pro | Con |
|---|---|---|
| No retry | Fastest, cheapest | Every typo becomes a failure |
| **One retry with the error (chosen)** | Fixes most mechanical errors (wrong name, missing quotes, type casts) | A failure costs about 2× the latency |
| Several retries / self-consistency (sample N, vote on results) | Higher accuracy | Breaks the under-3s P50 target and multiplies cost |

A query that runs and returns wrong or empty rows is not retried. Catching that needs a judge or result-sanity check, which is a next step.

> **Measured (v0.1):** on the 10 dev questions the repair step never fired. The first attempt always produced runnable SQL. See [reports.md](reports.md).
>
> **Measured (v0.2):** on 200 golden questions it still never fired. Every miss was SQL that ran and returned the wrong rows. So the next gain is checking results, not retrying errors. In agent mode the model tests its SQL with `run_sql` before answering, which is a built-in repair loop, capped at 8 model calls.
>
> **Update when:** the eval runs (repair rate, and how many failures one retry fixes). Also if a check for empty or implausible results is added.

## 2. How does the agent know about the database it's querying?

**Today:** a static briefing built once at startup (`src/context.py`). It has every table and column with its type, the CSV column notes, and every distinct value of low-cardinality text columns (≤100 values), so the model filters on `'POPLATEK TYDNE'` and doesn't invent `'weekly'`. It also flags numbers stored as text.

| Option | Pro | Con |
|---|---|---|
| Nothing (naive baseline) | Smallest prompt | Hallucinated tables and columns, the main failure of naive text-to-SQL |
| **Full schema, docs and values in the prompt (chosen)** | Simple; nothing to miss; a stable prefix that caches well | Prompt grows with the schema; fine for 8 tables, not for 500 |
| Retrieval over columns (embeddings) | Scales to wide schemas | Can miss a needed column; another call and more latency |
| Schema-exploration tools (`list_tables`, `sample_values`) | The model looks up only what it needs | Several round trips per question |

BIRD's own finding backs this choice: missing value and domain knowledge is the largest error source, and adding it lifts every model by 5–20 points (see the [BIRD appendix](solution.md#appendix-b-the-bird-benchmark)). For "plug in any database", the briefing builder runs unchanged on any SQLite file. Retrieval is the upgrade path once schemas outgrow the prompt.

> **Measured (v0.2):** agent mode adds the exploration-tools option. Its prompt holds a compact outline of 1,281 characters, against 6,847 for the full briefing, and it fetches notes and values with `describe_table` and `sample_values` (3.1 tool calls per question). On this 8-table database it scored 82% against 86% for the full briefing, at twice the cost. Seeing sample values made it add filters the question didn't ask for. While the schema fits in one prompt, the full briefing wins. Tools are the path for schemas that don't fit.
>
> **Update when:** retrieval is added, or the CLI accepts a connection string for another database.

## 3. What are the trade-offs between different agent architectures for this problem?

**Today:** two modes behind one interface (`make_session`). `chat` (v0.1) is a fixed pipeline with one model call on the happy path: briefing, generate, guard, run, repair once. `agent` (v0.2) is a Strands tool-calling loop: outline, then explore, test with `run_sql`, and answer, capped at 8 model calls.

| Architecture | Latency | Reliability | Fit |
|---|---|---|---|
| **Single call plus repair (chat mode, default)** | 1 call, 2 at most | Predictable and easy to test | Meets the under-3s target |
| **Tool-calling agent loop (agent mode)** | 2–5+ calls | Can self-check, but has more ways to fail | Better for ambiguous questions and wide schemas |
| Multi-stage (schema linking, then generation, then verification) | 3+ calls | Strong on benchmarks (DIN-SQL style) | Too slow for interactive use as the default |
| Router (a small model triages and escalates hard questions) | 1–2 calls | Puts the cost where it's needed | A good next step once the eval shows which questions fail |

The pipeline is plain functions with frozen Pydantic models, so each stage can be tested and swapped. Agent mode was added as one more session type, not a rewrite. Strands handles the loop, the tool schemas and structured output. We added loop limits, because by default Strands retries structured output without end, and a database connection per tool call, because Strands runs tools on worker threads.

> **Measured (v0.2), 200 golden questions:** chat 86%, P50 1.07s, $0.00014 a query. Agent 82%, P50 2.31s, $0.00027 a query. The agent won 6 questions and lost 14. On a schema this small, the loop costs more than it earns. A router that tries chat first and escalates to the agent is the next design to measure.
>
> **Update when:** a router is added, or the agent's prompt changes (for example a "no unstated filters" rule).

## 4. What are the trade-offs between open-source and proprietary models for this use case?

| | Open models on Fireworks | Proprietary (GPT-5.5) |
|---|---|---|
| Cost at 30k queries/day | Much lower per token, plus prompt caching | Unit economics don't work at platform scale |
| Latency | Fast serverless inference; a dedicated deployment if the SLO needs it | Often several seconds end to end |
| Control | Pin versions, fine-tune, self-host or dedicate | Model changes outside your control; no weight-level tuning |
| Raw quality | Has to be measured; the gap narrows with grounding | Strong general reasoning |

Most text-to-SQL failures (hallucinated names, wrong joins) come from missing context, not from model capability, so a grounded open model should close most of the gap. That claim still has to be measured. Default model: `glm-5p3-flash`, which you can change with `FIREWORKS_MODEL`.

> **Update when:** the eval compares models (EX accuracy, P50/P95 latency, cost per query against the GPT-5.5 baseline).

## 5. How should the CLI handle follow-up questions and conversation context?

**Today:** chat mode sends the last 3 **successful** turns as question/SQL pairs, without result rows; failed turns are dropped. Agent mode keeps its whole conversation, and each follow-up also carries the previous result's rows (up to 10, `Config.history_rows`). `:reset` clears the history in both modes.

| Option | Pro | Con |
|---|---|---|
| No history | Cheapest | "Now only for 1997" can't work |
| **Last N questions plus SQL (chat)** | Enough to refine a query; small; keeps the cached prefix stable | Can't refer to specific result values |
| **Include result rows (agent)** | Allows "which of those…" questions | Large prompts, higher latency, sensitive data in the prompt |
| Summarized memory | Long sessions | An extra call, and lossy |

Only the previous result's rows are added, capped at 10. Earlier results are already in the agent's conversation. That keeps prompts small; the cost is that a user can't point at row 15 of a long result.

> **Measured (v0.2), 15 follow-up conversations (46 turns):** chat 96% (44/46), agent with result rows 93% (43/46), agent without rows 89% (41/46). Rows fixed the "the third one" conversation, where the agent without rows picked the wrong district. In conversations the agent averages about 2 tool calls per turn, against 3.1 on one-off questions, because it reuses its earlier schema lookups. Its cost per turn is $0.00021 against $0.00017 for chat, a 1.3× gap instead of 2×. Chat does well because its SQL history already says which rows the user saw, as long as the query can be rerun.
>
> **Update when:** follow-ups get longer (5+ turns), or a user needs to refer to rows past the first 10.

## 6. How do you balance latency, cost, and accuracy for an interactive CLI?

- **One call on the happy path.** The answer summary is printed from the rows, not written by a second model call.
- **A stable, cacheable prefix.** The system prompt and briefing come first and are identical on every call, so Fireworks prompt caching cuts time to first token and input cost.
- **Short output.** `reasoning_effort="low"` and a one- or two-sentence `reasoning` field keep output tokens down.
- **Bounded worst case.** One repair at most, a 15s SQL timeout, and at most 1,000 rows fetched.
- **Visible costs.** Every answer prints total, model and SQL time, tokens (including cached) and the number of attempts, so you can check each trade-off against real numbers.

The next lever is model choice per question (a router), measured on the eval.

> **Measured (v0.1):** P50 1.00s, P95 1.54s, $0.00011 per query ($3.31 a day at 30k). The model call is about 98% of the time, and the cache serves about 99% of input tokens. See [reports.md](reports.md).
>
> **Measured (v0.2):** requests now carry an `x-session-affinity` key per database and mode, so parallel sessions land on a replica that already has the prefix cached. With 8 parallel sessions, cache hits went from 0% to 90% of input tokens in chat mode, and 84% in agent mode. The agent's extra model calls double the cost and latency ($8.09 against $4.14 a day; P50 2.31s against 1.07s). Under parallel load they also hit throttling first: 20 of 200 agent runs stalled for more than 40s, but ran in 2–5s when retried one at a time.
>
> **Load time (v0.2):** startup took about 4.5s, almost all of it the database scan (row and value counts on every column, including the 1M-row `trans` table). The scanned schema is now cached in `.cache/` until the database, the docs or the inspection code change. Startup is now 0.56s in chat mode and 1.31s in agent mode; Strands is imported only in agent mode.
>
> **Agent loop (v0.2, optimized):** the first loop gave the agent only the outline, so it spent model calls on `describe_table` to fetch notes the chat prompt already had in cache. Now the full briefing sits in the agent's cached prompt while it fits, the agent tests once with `run_sql`, and a rule stops it adding unasked conditions or columns. Golden: 82% to 90%, P50 2.31s to 1.80s, P95 3.25s, 3.1 to 2.1 tool calls. Follow-ups: 93% to 100%. Cost per query rose from $0.00027 to $0.00030, because the larger prompt is mostly cached. A router (chat first, the agent on an error or empty result) is the next lever.
>
> **Update when:** a router is added, or a dedicated deployment replaces serverless.

## 7. How would you approach post-training (fine-tuning) to improve the model for this database over time? What data would you need, and when does it beat prompt or retrieval changes?

**When it beats prompt or retrieval changes:** once the eval shows recurring domain errors that better grounding doesn't fix (coded values, join conventions), or once a smaller tuned model matches accuracy at lower latency and cost per query.

**Data needed:**
1. Execution-verified synthetic question/SQL pairs from a strong teacher model, keeping only pairs whose SQL runs and agrees across samples.
2. Real CLI usage: questions, accepted SQL, and user corrections.

**Path on Fireworks:**
1. LoRA SFT on a medium open model (for example Qwen3-30B-A3B).
2. RL with an execution-match reward through the Training API.
3. Serve as a LoRA adapter, one adapter per database or tenant on a shared base model.

The roadmap is also summarized in [solution.md](solution.md).

> **Measured (v0.2):** the synthetic dataset exists: 200 questions in `eval/golden_questions.json`, from `src/golden.py`. `kimi-k3` wrote 289 questions without SQL. It then wrote SQL for each two or three times, and 275 questions agreed. 200 were kept, balanced across tiers. It cost about $11.27 at list prices. That was too much: `glm-5p3-flash` is now the default teacher, with a $2 cap. The same pipeline can produce SFT training pairs.
>
> **Update when:** an SFT run exists. Add its size, cost and accuracy change.
