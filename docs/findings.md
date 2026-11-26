# Findings

1. **Grounding is the whole game.** A naive one-line prompt scored 8% on 200 golden questions and 0/10 on dev, because the model invents table and column names. Giving the same open model the schema, column notes and real coded values raised it to 86% in a single call.

2. **The agent loop got faster and more accurate.** The first agent saw only a schema outline and spent model calls fetching column notes, scoring 82% at a 2.31s P50. Putting the full briefing in its cached prompt, testing the query once, and forbidding conditions or columns the question didn't ask for raised it to 90% on the golden set and 46/46 on follow-ups. P50 fell to 1.80s and tool calls to 2.1 per question. Cost rose only 10% ($0.00030 a query), because the larger prompt is mostly served from cache.

3. **Seeing more data made the agent over-filter.** Before the rule, after sampling values the agent read "approved loans" as a status filter and "standing orders" as one payment type. With the optimized loop, all six such misses we found (g_019, g_057, g_059, g_072, g_088, g_135) are now correct.

4. **Follow-ups need the previous result, not just the previous SQL.** With the previous result's rows in the prompt, the agent answered "the third one" correctly; without them it picked the wrong district. Rows lifted the first agent loop from 89% to 93% on follow-up turns.

5. **Session affinity makes prompt caching work under load.** Under 8 parallel sessions, cache hits were 0% until requests carried an `x-session-affinity` key per database and mode. With the key, 84–90% of input tokens were served from cache.

6. **Startup time was a database scan, not the agent.** About 3.5s of the 4.5s startup went to counting values on every column, including the 1M-row `trans` table. Caching the scanned schema in `.cache/` cut startup to 0.56s in chat mode and 1.31s in agent mode.

7. **Parallel load, not the agent, caused the long tail.** At 8 parallel workers, 20 agent runs stalled for over 40s in bursts; one at a time, the same questions took 2–5s. The agent makes about twice as many requests as chat, so it hits serverless throttling first.

8. **Check the grader before trusting a score.** The grader treated an `ORDER BY` inside a subquery as "row order matters" and marked correct follow-up answers wrong. Fixing it moved chat from 89% to 96% on follow-ups; a free `--rescore` re-grades saved reports without model calls.

9. **A strong teacher model is expensive.** Generating the 200-question golden set with `kimi-k3` cost about $11. The same eval runs with `glm-5p3-flash` cost cents, so it is now the default teacher, with a $2 cap.

10. **No eval library fits text-to-SQL scoring with latency and cost.** [Strands Evals](https://strandsagents.com/docs/user-guide/evals-sdk/quickstart/), [Inspect AI](https://inspect.aisi.org.uk/) and [promptfoo](https://www.promptfoo.dev/) mainly add LLM judges, runners and viewers, and [BIRD's script](https://github.com/AlibabaResearch/DAMO-ConvAI/tree/main/bird) only compares result rows. None tracks latency and cost for our own model calls, so the custom `src/eval.py` exists.

11. **The pipeline stays synchronous; async and [aiostream](https://github.com/vxgmichel/aiostream) add nothing here.** Each question is a chain of dependent model calls, and model calls are about 98% of the time, so async can't overlap anything. At 30,000 queries a day (about 0.35 per second), threads already cover concurrency. Async would also spread through about 7 files, wrap every blocking SQLite and HTTP call, and add stream operators to what are a few plain function calls, making the code harder to read with no change to accuracy, latency or cost.
