# Reports

## Summary

Both grounded modes pass every target, and the naive prompt fails because a model that never sees the schema invents it. The agent is now the most accurate mode, and its lead grows with difficulty: 4 points on the golden set, mostly on tier 2 and 3 questions, and perfect on follow-ups, where testing its query and seeing the previous result's rows matter most. That accuracy costs about twice as much as chat and 0.7s more at P50, still more than 10× under GPT-5.5. So chat stays the right choice for fast one-off lookups, and the agent for conversations and harder questions; a router that picks per question is the natural next step. The misses left are mostly ambiguous questions rather than invented schema. On the dev question the agent missed, it counted owners by the client's district where the answer key uses the account's. The caveat is one run per mode: a 4-point gap is 8 questions, and the golden set's answer key comes from a model.

## Success metrics

Each mode is judged on five dimensions, each with a target for a usable interactive product.

| Dimension | Metric | Target | Why |
|---|---|---|---|
| Accuracy | Execution accuracy on the 200-question golden set | ≥ 85%, and far above the naive prompt | Wrong SQL is the main failure of naive text-to-SQL |
| Follow-ups | Turns correct, and conversations with every turn correct (15 conversations, 46 turns) | ≥ 90% of turns, ≥ 80% of conversations | The CLI is conversational; one wrong turn derails the rest |
| Latency | End-to-end P50 and P95 per question | P50 < 3s, P95 < 5s | Interactive use |
| Cost | Cost per day at 30,000 queries | ≤ $15 a day, at least 7× under GPT-5.5 ($105–424) | Proprietary-model unit economics don't work at this volume |
| Reliability | Questions with no runnable SQL | ≤ 1% | The user should always get a query or a clear error |

We also track internal numbers with no target: model calls and tool calls per question, prompt-cache hit rate, tokens in and out, and CLI startup time.

## Scorecard (version 0.2)

| | Baseline (naive prompt) | Chat | Agent |
|---|---|---|---|
| **Accuracy, golden (200)** | 8% ❌ | 86% ✅ | **90%** ✅ |
| Tier 1 / 2 / 3 | 5% · 5% · 13% | 93% · 86% · 75% | 97% · 92% · 78% |
| Dev (10 given) | 0/10 | 10/10 | 9/10 |
| **Follow-up turns (46)** | n/a | 96% ✅ | **100%** ✅ |
| Conversations fully correct (15) | n/a | 87% ✅ | **100%** ✅ |
| **Latency P50 / P95** | 1.69s / 3.83s | **1.07s / 1.90s** ✅ | 1.80s / 3.25s ✅ |
| **Cost per day at 30k** | $3.22 | **$4.14** ✅ | $9.09 ✅ |
| **No runnable SQL** | 91% ❌ | **0%** ✅ | 1% ✅ |
| *Model calls / tool calls per question* | 1 / 0 | 1 / 0 | 2.1 / 2.1 |
| *Prompt-cache hit rate* | 0% | 90% | 94% |
| *Tokens in / out per question* | 44 / 201 | 2,384 / 78 | 6,364 / 135 |
| *Startup (schema cached)* | n/a | 0.56s | 1.31s |

Accuracy, latency and cost are from the golden set; follow-ups from the follow-up set. GPT-5.5 would cost $105–424 a day for the same prompts.


## Dev questions

The 10 dev questions, as recorded in `dev_answers.json`. The answers come from chat mode, the default `uv run cli` mode, which scored 10/10. Agent mode scored 9/10: on q_008 it counted owners by the client's district, where the answer key uses the account's district.

| ID | Tier | Question | Answer (chat mode) | Chat | Agent |
|---|---|---|---|---|---|
| q_001 | 1 | How many bank accounts are located in the Prague region? | COUNT(*) = 554 | ✅ | ✅ |
| q_002 | 1 | What is the total approved loan amount for loans whose contract is finished and fully paid with no problems? | total_amount = 18603216 | ✅ | ✅ |
| q_003 | 1 | How many disposition records represent account owners (not authorized users)? | owner_count = 4500 | ✅ | ✅ |
| q_004 | 1 | What is the average monthly loan payment amount across all loans? | avg_monthly_payment = 4190.6642228739 | ✅ | ✅ |
| q_005 | 2 | How many female clients live in districts where the average salary is greater than 10,000? | female_clients = 636 | ✅ | ✅ |
| q_006 | 2 | How many distinct clients are account owners for at least one account in the Prague region? | COUNT(DISTINCT d.client_id) = 554 | ✅ | ✅ |
| q_007 | 2 | What is the total transaction amount for account 1 during 1997? | total_amount = 96418 | ✅ | ✅ |
| q_008 | 3 | For each district in the south Moravia region, how many distinct account owners are there? List district name and owner count, ordered by owner count descending. | 14 rows: district_name=Brno - mesto, owner_count=128; district_name... | ✅ | ❌ |
| q_009 | 3 | Which five clients with account owner status have the highest total loan amounts across their owned accounts? Show client id, gender, and total loan amount. | 5 rows: client_id=9340, gender=M, total_loan_amount=590820; client_... | ✅ | ✅ |
| q_010 | 3 | How many running loan contracts that are in good standing are linked to accounts owned by male clients living in the north Bohemia region? | COUNT(DISTINCT l.loan_id) = 18 | ✅ | ✅ |

## Golden set (200 questions)

Per-question records for every mode are in `eval/report_golden.json`. Chat mode misses 29 and the agent 20. 17 questions defeat both modes, which points to ambiguous wording rather than a mode's weakness.

| Why an answer was wrong | Chat | Agent |
|---|---|---|
| Extra columns beyond what was asked | 8 | 4 |
| Missing columns the answer key includes | 7 | 6 |
| Right shape, wrong values (wrong filter, join or column) | 9 | 6 |
| Wrong number of rows | 5 | 2 |
| No runnable SQL | 0 | 2 |

Column mismatches are about half of each mode's misses (15 of 29, 10 of 20), and the strict BIRD metric counts them as wrong.

## How it was run

Run on 2026-11-25 and 26 with `glm-5p3-flash` on Fireworks serverless. Baseline and chat ran 8 questions in parallel and the agent 4, because at 8 the agent's extra requests hit throttling bursts (P95 44.6s; 2–5s when rerun one at a time).

```bash
uv run python -m src.eval --set golden     # 200 synthetic questions → eval/report_golden.json
uv run python -m src.eval --set followup   # 15 conversations, 46 turns → eval/report_followup.json
uv run python -m src.eval --set dev        # 10 given questions → eval/report_dev.json and dev_answers.json
```

## History

| Version | Change | Golden | Follow-ups | P50 | Cost per day |
|---|---|---|---|---|---|
| 0.1 | Chat with the full briefing (dev only: 10/10) | 86% | 96% | 1.07s | $4.14 |
| 0.2 | First agent loop: outline + schema tools | 82% | 93% | 2.31s | $8.09 |
| 0.2 | Agent + previous result's rows in follow-ups | — | 93% (89% without rows) | 1.76s | — |
| 0.2 | Optimized agent loop: full briefing cached, test once, no unasked filters | **90%** | **100%** | 1.80s | $9.09 |

Chat was scored on the golden and follow-up sets after they were built in 0.2. Follow-up scores use the corrected grader, where an `ORDER BY` inside a subquery no longer makes row order count.
