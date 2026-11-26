# Solution: Text-to-SQL Agent

## Objective

An accurate, low-latency, low-cost text-to-SQL CLI built on open models served by Fireworks AI.

## How it works

### Version 0.1

![firetext2sql architecture: user, CLI, agent, Fireworks serverless, read-only SQLite](architecture.png)

The diagram source is [architecture.dot](architecture.dot). To re-render it: `dot -Tpng -Gdpi=200 docs/architecture.dot -o docs/architecture.png`.

1. **Briefing (once).** `context.py` lists every table and column, adds what each column means (from `data/database_description/`), and adds the real values of coded columns. So the model writes `'POPLATEK TYDNE'` and doesn't invent `'weekly'`. The briefing is the same on every call, so Fireworks can cache it.
2. **Ask.** `agent.py` sends the briefing, the last few questions and their SQL, and the new question. The model must reply as JSON with a `sql` field.
3. **Check and run.** Only a single read-only `SELECT` is accepted, on a read-only connection, with a time limit.
4. **Fix once.** If the reply is unusable or SQLite errors, the error goes back to the model for one corrected attempt.
5. **Show.** `cli.py` prints the SQL, a results table, and the time and tokens used.

The reasoning behind each choice is in [design-considerations.md](design-considerations.md).

#### Architecture principles

1. The pipeline is functional and serverless. Keep it that shape, and add steps as functions, not layers.
2. The briefing (schema, column notes, coded values) is built once at startup and stays a stable prefix, so the prompt cache keeps working.
3. The database is always opened read-only. The SQL guard is a second layer, not the only one.

#### Evaluation

**Golden set.** `data/dev_questions_with_answers.json` has 10 questions in three tiers (simple, one join, several joins plus grouping), each with gold SQL and the expected result. We first run every gold query to confirm the answer key. These 10 are public, so the plan adds about 50 holdout questions of the same kinds, kept separate to catch overfitting the briefing. The holdout isn't built yet.

**Metric: execution accuracy (as in BIRD).** We compare results, not query text. A prediction is correct when its rows match the gold query's rows. Column names are ignored. Row order is ignored unless the gold query sorts (`ORDER BY`), and even then tied rows may come in any order. Numbers are rounded before comparing. A query that errors counts as wrong.

**Three versions, same questions:**

1. the naive prompt, `Convert this question to SQL: {question}`, with no briefing;
2. our agent with the briefing;
3. our agent with the briefing plus the fix-once step.

**For each version** we report accuracy (overall and per tier), P50/P95 latency, and cost per query projected to 30k queries a day. Each miss gets one tag: wrong table or column, wrong join, wrong code value, wrong counting, or ambiguous question. That list becomes "what still fails, and what's next" in the project summary. The agent's answers to the 10 questions go into `dev_answers.json`. Run it with `uv run python -m src.eval`, which also writes per-question records to `eval/report.json`.

#### Results

Measured accuracy, latency and cost, plus a verdict against each target, are in [reports.md](reports.md).

#### Latency and cost

```
latency = time to first token (shrinks when the prompt is cached) + output tokens ÷ speed + SQL time (+ one repair)
cost    = (uncached input + discounted cached input + output) × (1 + repair rate)
```

The levers:

- **One model call** on the happy path. The answer is printed from the rows, not written by a second call.
- **A cached prefix.** The ~3k-token briefing comes first and is identical every time.
- **Short output.** Low reasoning effort and a one-line `reasoning` field.
- **A bounded worst case.** One repair at most, plus a SQL time limit.
- **Model choice.** Driven by the eval: a fast model by default, a strong one as the accuracy reference.

Every answer shows its own timings and token counts. The measured effect of these levers is in [reports.md](reports.md).

#### Fine-tuning roadmap

1. **Data:** execution-verified synthetic question/SQL pairs from a strong teacher model, then real CLI questions with user corrections.
2. **SFT with LoRA** on a medium open model, for example Qwen3-30B-A3B.
3. **RL** with a "results match gold" reward, starting from the SFT model.
4. **When it's worth it:** when the eval shows recurring domain errors that better briefing doesn't fix, or when a smaller tuned model matches accuracy at lower latency and cost.

Fine-tuning numbers are estimates. Measured latency, cost and accuracy are in [reports.md](reports.md).
