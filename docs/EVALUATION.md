# How this RAG system is tested

Three layers of testing, from "does each function work" to "is the final answer right".

## The 30-second version

> I built a **58-question golden dataset** from Berkshire Hathaway's 154-page 2025 annual report,
> with the answer's page and section labelled for each question, including **6 deliberately
> unanswerable** ones. I evaluate in two stages. **Retrieval:** does the right evidence come back?
> Measured with Hit@K, MRR and nDCG on a **held-out split**. **Answers:** is the final answer
> correct, faithful to its sources and properly cited, and does it say "not in the report"
> instead of guessing? Every wrong answer is classified as a **retrieval failure** or a
> **generation failure**. On top of that, 105 unit tests cover the pipeline logic without any
> network calls.

## Layer 1: retrieval evaluation (`rag eval`)

**Question it answers:** does the system find the right part of the document?

| What | How |
|---|---|
| Test data | [`backend/evals/golden.json`](../backend/evals/golden.json): 58 questions; each answerable one is labelled with the page(s) and section that contain the answer |
| Process | For each question, retrieve the top 10 chunks and check whether any comes from a labelled page and section |
| Metrics | **Hit@K**, **MRR**, **Recall@K**, **nDCG@K** (explained below) and **latency** (p50 / p95) |
| Labels | By page and section, not chunk id, so the same set can compare chunking strategies. A fact stated in two places (CEO letter and 10-K) lists both as alternatives |
| Methodology | **Tune / test split:** 22 questions are used to choose settings, 36 are held out to confirm them |

The held-out split caught two false conclusions. The reranker looked useless on the tuning
questions but was the biggest gain on held-out ones. A "confidence gate" scored best on the
tuning questions and never fired on held-out ones: it had been fitted to the one question it was
designed from.

**Result (held-out split, 33 answerable questions):**

| Pipeline | Hit@1 | Hit@5 | nDCG@5 | MRR | p50 latency |
|---|---|---|---|---|---|
| Dense search only | 61% | 79% | 70% | 0.690 | ~600 ms |
| Dense + table rows | 73% | 94% | — | 0.798 | ~600 ms |
| Hybrid + blended reranker | 70% | 97% | 83% | 0.790 | ~1,300 ms |
| **Hybrid + blended reranker + table rows** | **70%** | **94%** | **83%** | **0.809** | ~1,700 ms |

How each step was found and measured is in the [README](../README.md#retrieval-pipelines-and-evaluation).

## Layer 2: answer evaluation (`rag eval-answers`)

**Question it answers:** is the final answer right and trustworthy?

| Check | What it asks | How |
|---|---|---|
| **Correctness** | Does the answer contain the expected facts? | LLM judge against `expected_facts`: correct, partial or incorrect |
| **Abstention** | For the 6 unanswerable questions: does it say "not in the report" instead of inventing an answer? | LLM judge |
| **Faithfulness** | Is every claim supported by the retrieved sources (no hallucination)? | LLM judge |
| **Citations** | Do the `[1]`, `[2]` markers point at real sources, and does every answerable question cite one? | Code, no LLM |
| **Key numbers** | Do the expected figures (e.g. 6,824) appear in the answer? | Code, no LLM: an independent check on the judge |
| **Failure cause** | When the answer is wrong, was the evidence retrieved? | Code: **retrieval failure** if the evidence never reached the model, **generation failure** if it did and the answer was still wrong |

Each run writes JSON and a readable Markdown page with every question, the expected answer, the
system's answer, the verdict and the sources, to `backend/evals/results/`.

**Result** (gemini-3.5-flash, `advanced` retrieval with table rows, top 6 sources;
[full report](../backend/evals/results/20261110-193634-gemini-flash-answers.md)):

| Measure | Result |
|---|---|
| Correct, answerable questions | **46 / 52 = 88%** (tune 15/19, held-out 31/33 = 94%) |
| Partial / incorrect | 3 / 3 |
| Unanswerable questions answered "not in the report" | 4 / 4 graded (100%) |
| Faithful to sources | 98% (one answer made an unsupported claim) |
| Valid citations | 100% |
| Not answered | 2 (free-tier rate limit: 429 then 503) |

The 3 incorrect answers: two **retrieval** failures (cash and total assets, single rows of the
balance sheet that are not retrieved) and one **generation** failure (insurance investment income:
the figure was in the sources, but the answer said it was not). The 3 partial answers gave the
core fact but missed a detail from the expected facts (a prior-year comparison, the meeting's
city, one clause of a risk factor).

Reading the 88%: the judge was inconsistent about prior-year comparisons, marking one answer
partial for omitting one and several others correct for the same omission, so the strict score
lies between about 82% and 94%. The no-LLM key-number check reports 70% because it also counts
prior-year figures that the questions do not ask for, and it does not read short figures like
"23.7%"; it needs refining before it can stand on its own.

**Known limitation:** the judge is the same model that writes the answers (Gemini Flash, the only
free model available), so verdicts may be lenient. The key-number check does not use an LLM. A
different judge model or human spot checks would be stronger.

## Layer 3: unit tests (`pytest`, 105 tests)

**Question they answer:** does each piece of logic work? They run in under a second with no
network: in-memory fakes stand in for Pinecone, Gemini and Bedrock.

Examples: header and footer removal, heading detection, table-row parsing and linearization,
chunks never crossing a section, the RRF arithmetic, blended reranking, model fallback when one
is overloaded, the reranker cache, every metric's calculation, and API error responses.

## What the questions cover

| Type | Count | Example | Tests whether… |
|---|---|---|---|
| Table numbers | 15 | "What were Berkshire's total assets at the end of 2025?" | numbers inside financial tables can be found |
| Prose facts | 18 | "How many people did Berkshire's subsidiaries employ?" | plain facts in text can be found |
| Paraphrases | 8 | "Does the company hand out its profits to owners as regular payouts?" (dividends) | meaning is matched without the document's words |
| Exact terms | 7 | "When are the 1.125% Senior Notes due?" | codes, ids and precise terms are matched |
| Multi-section | 4 | "What are Berkshire's five largest equity holdings?" (CEO letter + Note 4) | several pieces of evidence are combined |
| Unanswerable | 6 | "What is Berkshire's ESG rating?" | the system admits it does not know |

The unanswerable questions are adversarial: the report mentions related things (a 2015 market
value row, a different "successor", bond credit ratings) but not the answer.

## The metrics, with examples

**Hit@K:** is at least one correct chunk in the top K results? Rank 1 and rank 5 count the same.

**MRR (Mean Reciprocal Rank):** for each question, 1 ÷ the rank of the first correct chunk, then
averaged across questions.

| First correct chunk at | Score |
|---|---|
| Rank 1 | 1.0 |
| Rank 2 | 0.5 |
| Rank 3 | 0.33 |
| Rank 5 | 0.2 |
| Not in the top 10 | 0 |

Example: three questions found at rank 1, rank 2 and not at all give MRR = (1.0 + 0.5 + 0) / 3 =
0.50. MRR 0.809 means the right chunk is usually first.

**nDCG@K (normalized Discounted Cumulative Gain):** are *all* the needed pieces of evidence in the
top K, and how high? Each chunk that supplies a not-yet-found piece of evidence earns
1 ÷ log₂(rank + 1): rank 1 earns 1, rank 2 earns 0.63, rank 3 earns 0.5, rank 5 earns 0.39. The
total is divided by the best possible score, so every question lands between 0 and 1.

Example, "net earnings in 2025", which needs the MD&A page and the earnings statement:

| Ranking returned | Points | nDCG |
|---|---|---|
| MD&A at 1, statement at 2 | 1 + 0.63 = 1.63 | 1.63 / 1.63 = **1.0** |
| MD&A at 1, statement at 3 | 1 + 0.5 = 1.5 | **0.92** |
| MD&A at 3, statement at 5 | 0.5 + 0.39 = 0.89 | **0.55** |
| Only the MD&A, at 1 | 1 | **0.61** |

| Metric | Cares about | Blind spot |
|---|---|---|
| Hit@5 | Is any correct chunk in the top 5? | Rank 1 and rank 5 count the same |
| MRR | How high is the first correct chunk? | Ignores the second and third pieces of evidence |
| nDCG@5 | Are all needed pieces found, and how high? | Harder to explain in one sentence |

In RAG, Hit@K matters most for answers (the model reads the top 6 chunks, so rank 4 still
reaches it), while MRR and nDCG show how well the ranking puts the best evidence first.

## Honest limits

- **One document, 58 questions.** One held-out question is worth about 3 points of Hit@K, so
  small differences are noise.
- **The questions were written by the developer** from reading the report. Real user questions
  would be the next step.
- **The answer judge grades its own model's output.** A different model or human spot checks
  would be stronger.
- **Reranker quota:** Pinecone's free plan allows 500 rerank requests a month. Evaluation runs
  skip unanswerable questions for retrieval metrics and cache reranker scores, so repeated runs
  cost nothing.

## Running it

```bash
cd backend
uv run pytest                                   # unit tests, offline
uv run rag eval --pipeline all --label <name>   # retrieval scoreboard, both splits
uv run rag eval-answers --label <name>          # answers + grading, JSON and Markdown report
```
