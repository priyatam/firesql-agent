# BIRD Primer

**Paper:** *Can LLM Already Serve as a Database Interface? A BIg Bench for Large-Scale Database Grounded Text-to-SQLs*. Li et al., NeurIPS 2023 (`docs/bird-paper.pdf`).

## What it is

BIRD is a **benchmark, not a method**: a test for whether AI can turn plain-English questions into correct SQL on realistic databases. It has 12,751 question/SQL pairs over 95 real databases (33.4 GB, 37 domains). Earlier benchmarks used small, clean, made-up databases. BIRD uses messy real ones, with cryptic column names, coded values, dates stored as text and millions of rows.

**The sample database (`financial`) is one of BIRD's own databases.** The paper's examples come straight from it: `'POPLATEK TYDNE'` means weekly statements, and only `OWNER` accounts are eligible for loans.

## The main idea, without math

The model needs three things to write correct SQL:
1. the **question**;
2. the **database**: tables, columns and real values;
3. **"external knowledge"**: the business facts that link the words in a question to the values in the data. For example, "weekly" = `POPLATEK TYDNE`, or "eligible for loans" = `type = 'OWNER'`.

Most real failures come from missing item 2 or item 3, not from the model being unable to write SQL. 70% of BIRD's questions need an understanding of what the stored values mean.

## Key findings

| Finding | Evidence in the paper | What it means for a prototype |
|---|---|---|
| **Real databases are much harder than toy ones.** | The same models score about 58–83% on the older Spider benchmark but only about 12–51% on BIRD. | Demo accuracy on clean data says little about production. |
| **Giving the model knowledge is the biggest lever.** | Adding the knowledge hints raised every model by about 5–20 points. GPT-4 went from 35% to 55% on the test set. | Feed column descriptions and value meanings into the prompt. |
| **Good prompting beats raw model power.** | DIN-SQL with GPT-4 set the best score. It used three techniques: showing sample values, worked examples, and self-correction. | These are exactly the techniques to build first. |
| **Even the best AI is far behind people.** | 55% for the best model vs 93% for humans. | Show the SQL to the user. Never present output as a black box. |
| **Where the errors come from** | Of 500 ChatGPT errors: 42% picked the wrong table or column, 41% misread the database (including made-up names), 18% misused the hints. | Grounding and execution checks address most of these failure types. |
| **Speed of the SQL matters too.** | One example: two correct queries took 22.4s vs 4.0s. | Measure query run time, not only model time. |
| **Small fine-tuned models lagged.** | Fine-tuned T5 models (up to 3B) peaked around 24%. | That was 2023 with small models. It doesn't rule out fine-tuning modern ones. |

**How the paper measures success:**
- **Execution Accuracy:** does the generated query return the same rows as the correct query? It compares results, not query text.
- **Valid Efficiency Score:** gives extra credit when a correct query also runs fast.

## Why we're using it

It gives us a **proven, standard way to judge the prototype**. We compare result rows against known-correct answers, the same way the research community does. It also gives a **research-backed blueprint**: ground the model in schema, values and business meaning, then check by executing. And it **sets honest expectations**: even strong models make mistakes on this exact database, so validation is part of the product.

## What it means for this project

> Your prototype failed in exactly the way this research predicts. The model was never shown your database, so it invented table names and guessed at coded values. Published results on this same database show that giving the model the schema, real values and business definitions is the single biggest accuracy gain, larger than switching to a more powerful model. That's what we built, and we measured it the same way the benchmark does: by checking that the results match known-correct answers. It also means a smaller, cheaper and faster open model can do the job, because the accuracy comes mainly from the context we provide.

**Caveats:**
- The numbers are from 2023, with older models. Use them for the *relative* lessons, not as current scores.
- Our 10 dev questions are a small sample. We report exact counts and failure reasons, not broad accuracy claims.
