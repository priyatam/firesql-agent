# Version 0.1

## Summary

Version 0.1 meets its three headline targets on the development set. With a briefing of the schema, column meanings and coded values, an open model on Fireworks answered 10 of 10 questions. A naive one-line prompt answered none, because it invents table names. Median latency is 1.0s against a 3s target, and cost is about $3 a day at 30,000 queries, against $105–424 for GPT-5.5. The evidence is narrow (10 public questions, one user, one known database), and two areas need to grow before this is a production-ready product. Follow-up questions currently cover simple refinements only. Longer conversations and references to earlier results need multi-turn context and scoring. The fixed single-call pipeline should become selectively agentic, with tools to explore the schema, test a query and ask for clarification, so it can handle any database while keeping its latency and cost headroom.

## Eval report

Accuracy, latency and cost of version 0.1, judged against the project's targets. The method is in [solution.md](solution.md#evaluation). To reproduce it, run `uv run python -m src.eval`. Raw per-question records are written to `eval/report.json`, and the agent's answers to `dev_answers.json`.

Run on 2026-11-25 with `glm-5p3-flash` on Fireworks serverless, all 10 dev questions, one run per version. Times are end to end, from a laptop over the internet. The one-time startup briefing is not included.

### Results

| Version | Correct | Tier 1 / 2 / 3 | P50 | P95 | Avg tokens in (cached) / out | Cost per query | Cost per day at 30k |
|---|---|---|---|---|---|---|---|
| Baseline: naive prompt | **0/10** | 0/4 · 0/3 · 0/3 | 2.22s | 2.46s | 37 (11) / 211 | $0.00011 | $3.30 |
| Grounded, no repair | **10/10** | 4/4 · 3/3 · 3/3 | 1.08s | 1.59s | 2,377 (2,128) / 75 | $0.00014 | $4.16 |
| **Grounded + repair (the CLI)** | **10/10** | 4/4 · 3/3 · 3/3 | **1.00s** | **1.54s** | 2,377 (2,363) / 75 | **$0.00011** | **$3.31** |

Prices are Fireworks' listed `glm-5p3-flash` serverless rates: $0.15 per 1M input tokens, $0.03 cached, $0.50 output. For comparison, GPT-5.5 lists at $5 input, $0.50 cached and $30 output (third-party sources). Our prompt on GPT-5.5 would cost about $0.0035–0.014 per query, or **$105–424 a day**, depending on cache hits.

**Why the baseline scores zero.** Without the schema, the model invents tables on 9 of 10 questions (`bank_accounts`, `loans`, `clients`, a made-up `district_name` column). On the tenth it returns two statements instead of one query. These are the classic failures of naive text-to-SQL.

**Where the time goes.** Almost all of it is the model call (P50 0.98s). SQL time is at most 0.26s, even on the 1M-row `trans` table. After the first call, nearly the whole briefing is served from the prompt cache. That's why the grounded prompt is 64× larger than the baseline's yet costs about the same.

### Verdict

| Requirement | Target | v0.1 result | Verdict |
|---|---|---|---|
| Quality | Better than the baseline prompt | 10/10 vs. 0/10 | **Met** on the dev set |
| Latency | P50 under 3s end to end | P50 1.00s, P95 1.54s | **Met**, about 3× headroom |
| Cost | A fraction of GPT-5.5 at 30k queries a day | $3.31 a day vs. $105–424 for GPT-5.5 | **Met**, about 30–130× cheaper |
| Interactive CLI with SQL, results and follow-ups | Works | Built and working; follow-ups not yet scored | **Partly shown** |
| Point at any database | A connection string | Fixed to one SQLite file | **Not met** |

### How much to trust this

10 public questions is a small sample, and the briefing was written with this database in view. The next steps, in order:

1. Build the 50-question holdout.
2. Repeat runs to measure variance.
3. Add follow-up test cases.
4. Test failure cases that trigger the repair step, which never fired on the dev set.

An earlier run scored 9/10. The grader failed q_008 because two districts tied on owner count and came back in the other order, though the rows were correct. I fixed the grader so tied rows may come in any order, not the agent, and reran. That earlier run's P50 was 1.11s, a first hint of run-to-run variance.

The baseline ran on the same open model, not on GPT-5.5. Its zero comes from missing schema, which no model can guess.
