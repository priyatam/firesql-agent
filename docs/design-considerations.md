# Design considerations & trade-offs

Answers to the main design questions behind a text-to-SQL agent. Each heading is one question. Each answer gives what the code does today, what else was considered, and why. The **Update when** line at the end of each section says which code change will add measured numbers or change the answer. Until the eval is run, accuracy, latency and cost figures are estimates.

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
> **Update when:** the eval runs (repair rate, and how many failures one retry fixes). Also if a check for empty or implausible results is added.

## 2. How does the agent know about the database it's querying?

**Today:** a static briefing built once at startup (`src/context.py`). It has every table and column with its type, the CSV column notes, and every distinct value of low-cardinality text columns (≤100 values), so the model filters on `'POPLATEK TYDNE'` and doesn't invent `'weekly'`. It also flags numbers stored as text.

| Option | Pro | Con |
|---|---|---|
| Nothing (naive baseline) | Smallest prompt | Hallucinated tables and columns, the main failure of naive text-to-SQL |
| **Full schema, docs and values in the prompt (chosen)** | Simple; nothing to miss; a stable prefix that caches well | Prompt grows with the schema; fine for 8 tables, not for 500 |
| Retrieval over columns (embeddings) | Scales to wide schemas | Can miss a needed column; another call and more latency |
| Schema-exploration tools (`list_tables`, `sample_values`) | The model looks up only what it needs | Several round trips per question |

BIRD's own finding backs this choice: missing value and domain knowledge is the largest error source, and adding it lifts every model by 5–20 points (see [bird-primer.md](bird-primer.md)). For "plug in any database", the briefing builder runs unchanged on any SQLite file. Retrieval is the upgrade path once schemas outgrow the prompt.

> **Update when:** retrieval or schema-exploration tools are added, or the CLI accepts a connection string for another database. Add the measured briefing size in tokens.

## 3. What are the trade-offs between different agent architectures for this problem?

**Today:** a fixed pipeline with one model call on the happy path: briefing → generate → guard → run → repair once.

| Architecture | Latency | Reliability | Fit |
|---|---|---|---|
| **Single call plus repair (chosen)** | 1 call, 2 at most | Predictable and easy to test | Meets the under-3s target |
| Tool-calling agent loop (the model calls `run_sql` and explores) | 2–5+ calls | Can self-check, but has more ways to fail | Better for ambiguous questions and wide schemas |
| Multi-stage (schema linking, then generation, then verification) | 3+ calls | Strong on benchmarks (DIN-SQL style) | Too slow for interactive use as the default |
| Router (a small model triages and escalates hard questions) | 1–2 calls | Puts the cost where it's needed | A good next step once the eval shows which questions fail |

The pipeline is plain functions with frozen dataclasses, so each stage can be tested and swapped. Adding a tool-calling mode means replacing one function, not the whole design.

> **Update when:** a tool-calling mode or router is added (the live session). Compare them on the same eval.

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

**Today:** the last 3 **successful** turns go in as question/SQL pairs. Result rows are left out, and failed turns are dropped. `:reset` clears the history.

| Option | Pro | Con |
|---|---|---|
| No history | Cheapest | "Now only for 1997" can't work |
| **Last N questions plus SQL (chosen)** | Enough to refine a query; small; keeps the cached prefix stable | Can't refer to specific result values |
| Include result rows | Allows "which of those…" questions | Large prompts, higher latency, sensitive data in the prompt |
| Summarized memory | Long sessions | An extra call, and lossy |

> **Update when:** there are follow-up test cases in the eval, or the history policy changes (number of turns, including rows).

## 6. How do you balance latency, cost, and accuracy for an interactive CLI?

- **One call on the happy path.** The answer summary is printed from the rows, not written by a second model call.
- **A stable, cacheable prefix.** The system prompt and briefing come first and are identical on every call, so Fireworks prompt caching cuts time to first token and input cost.
- **Short output.** `reasoning_effort="low"` and a one- or two-sentence `reasoning` field keep output tokens down.
- **Bounded worst case.** One repair at most, a 15s SQL timeout, and at most 1,000 rows fetched.
- **Visible costs.** Every answer prints total, model and SQL time, tokens (including cached) and the number of attempts, so you can check each trade-off against real numbers.

The next lever is model choice per question (a router), measured on the eval.

> **Measured (v0.1):** P50 1.00s, P95 1.54s, $0.00011 per query ($3.31 a day at 30k). The model call is about 98% of the time, and the cache serves about 99% of input tokens. See [reports.md](reports.md).
>
> **Update when:** per-stage timings and token use from the eval are available, and when prompt-cache keys or session affinity are added.

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

> **Update when:** a synthetic dataset or SFT run exists. Add its size, cost and accuracy change.
