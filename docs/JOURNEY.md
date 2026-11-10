# How we built it: from an empty folder to 88% correct answers

A log of the build, in order: what was tried, what was measured, what failed, and why each
decision was made. Built over two sessions on 10 and 11 November 2026, with Claude Code.

## Where it ended up

| | Start | End |
|---|---|---|
| Retrieval, held-out MRR | 0.690 (first measured baseline) | **0.809** |
| Retrieval, held-out Hit@5 | 79% | **94%** (97% before table rows) |
| Answers | none | **88% correct** (46/52), 100% valid citations, 98% faithful |
| Unanswerable questions | — | 4/4 answered "not in the report" |
| Golden dataset | — | 58 questions, tune and held-out splits |
| Unit tests | — | 105, offline |

---

## Session 1: build the pipeline, then learn to measure it

### 1. Choosing the stack

- **Flask** for the API, because the target job asked for it; FastAPI was considered.
- **Pinecone** for vectors, over pgvector, OpenSearch and local stores: managed, free tier, widely
  known. Trade-off noted: the free plan only runs in `us-east-1`.
- **AWS Bedrock** for embeddings and answers, through the AWS profile of an earlier project
  (`context-forge`). Reading that project showed Claude is blocked on the AWS Free Plan, so the
  plan was Qwen3 / Nova for answers and Titan for embeddings.
- **Real company PDFs, not plain text.** Companies have PDFs, often with tables or scans, so the
  test document became Berkshire Hathaway's 154-page 2025 annual report.
- No Docker: dependencies are locked with `uv`, and the goal is learning RAG, not deployment.

### 2. First pipeline

PDF → text → clean → fixed-size chunks (~1,800 characters, 300 overlap) → embed → Pinecone →
answer with numbered `[n]` citations. Ports and adapters (`ports.py`) keep Pinecone, Bedrock and
Textract behind interfaces, so tests use fakes and providers can be swapped.

The first real PDF run exposed two parser problems: **pdfplumber** merged words
(`Thetypeandvolumeof…`), doubled bold letters (`Ottther`) and took 40 seconds. Switched to
**pypdfium2**: under 1 second, clean text, and a licence fine for commercial use.

### 3. Blocked: AWS Bedrock

Every Bedrock call failed: `Error 002: Access to Bedrock models is not allowed for this account`.
IAM and model access were fine; the block was on the account. The pipeline was built and tested
offline with fakes in the meantime.

### 4. Structure-aware chunking

Fixed-size chunks cut across sections ("Risk Factors" running into "Cybersecurity"). A close
analysis of the report shaped the new chunker:

- Font sizes all read 1.0 (the PDF scales text through its matrix), so size is measured from
  glyph boxes; bold text is detected from font names.
- Sections come from the PDF outline, anchored to their heading lines because bookmarks only point
  at a page, plus bold headings (`Item 1A.`, `(12)`, risk-factor sentences).
- Not headings: headings repeated on continuation pages, running headers, the 10-K's own contents
  page, a chart's numbers misread as a contents page. Two-line bold headings are joined.
- Inside a section: paragraphs (by indent) and tables (rows ending in numbers, with their column
  headings). Tables are never split if they fit; larger ones repeat their headings.
- Each chunk carries its section path, which is embedded with it and shown in citations.

Result: 133 sections, 461 chunks, about 2 seconds for the whole report.

### 5. Embeddings without AWS

Bedrock stayed blocked, so embeddings moved to **Pinecone Inference** with
`llama-text-embed-v2` (1024 dimensions, same index). The `Embedder` interface gained separate
document and query methods, because this model encodes the two differently.

### 6. The golden dataset and the first numbers

22 questions labelled with the page and section of their answer, by page rather than chunk id so
chunking changes cannot invalidate them, including 3 unanswerable questions. `rag eval` measures
Hit@K, Recall@K, Precision@K, nDCG@K, MRR and latency.

Baseline, dense search: **Hit@1 74%, Hit@5 89%, MRR 0.795.** Paraphrases worked; exact terms and
table numbers failed ("cash and cash equivalents" not in the top 15).

### 7. Hybrid search: first worse, then better

Added a sparse (keyword) index with `pinecone-sparse-english-v0`, queried in parallel with dense
search and fused with reciprocal rank fusion (RRF), as four plug-in pipelines: `basic`, `hybrid`,
`advanced` (with a reranker) and `basic-rerank`.

Textbook hybrid search **lowered** MRR from 0.795 to 0.690. Diagnosis: the sparse model weights
"the", "of" and "and" like keywords, and RRF's standard k=60 lets a chunk that appears in both
lists outvote dense search's correct first result. Tuning RRF to k=1 with a sparse weight of 0.5
turned it into a gain: **MRR 0.811, Hit@5 95%**.

### 8. Reranker: replacing versus blending

Added the `bge-reranker-v2-m3` cross-encoder. Replacing the hybrid order with the reranker's
order looked useless (MRR 0.789). It ruined a paraphrase ("hand out profits as payouts" lost the
Dividends section) because cross-encoders favour literal word overlap.

### 9. 58 questions and a held-out split

The question set grew to 58 (6 unanswerable, written to be tricky), split into 22 tuning and 36
held-out questions. **The held-out split reversed the reranker verdict**: on unseen questions it
was the biggest gain (MRR 0.697 → 0.778).

Two ways to protect paraphrases:

- A **confidence gate** (skip the reranker when its top score is low) scored best on the tuning
  questions and **never fired** on held-out ones: it had been fitted to the one question it was
  designed from. Rejected.
- **Blending** the reranker's order with the hybrid order (weighted RRF) improved both splits.
  Adopted: held-out **Hit@5 97%, MRR 0.790**.

### 10. Published

Pushed to GitHub (`msuhasiya42/rag-system`), first private, then public.

---

## Session 2: table rows, answers, and answer accuracy

### 11. Table-row indexing (small-to-big retrieval)

Financial tables were the weakest category: a table chunk's vector blends twenty line items. Each
labelled table row is now also indexed on its own, written as a sentence that pairs values with
their years:

```
CONSOLIDATED BALANCE SHEETS: Total assets: 2025: 1,222,176; 2024: 1,153,881
```

A matching row returns its whole table chunk. Compared in two Pinecone namespaces (chunks only and
chunks plus rows): dense search alone went from **MRR 0.690 to 0.798**, matching the previous best
pipeline at a third of the latency, and the best pipeline reached **MRR 0.809**. The two
balance-sheet questions it was aimed at improved least.

### 12. Evaluation has a cost

The full scoreboard runs used up Pinecone's free **500 rerank requests a month** (about 540
calls in a day). Fixes: a monthly quota now fails fast instead of retrying, retrieval evaluation
skips unanswerable questions, and reranker scores are cached so an unchanged re-run costs nothing.
A new Pinecone account restored the quota.

### 13. Bedrock, investigated properly

The same `Error 002` from our code, the AWS CLI with the earlier project's own settings, the AWS
console's playground, and a Bedrock API key. The whole account is blocked, so no change of model,
profile or credential can help. Only the account owner can resolve it, through the plan, credits
or AWS Support.

### 14. Answers from Gemini

Researched free LLM tiers (Gemini, Groq, Mistral, Cerebras, GitHub Models, OpenRouter, Meta's
Llama API). With a Gemini key: Pro models are not in the free tier, some Flash models were
overloaded, and `gemini-3.5-flash` works. One **OpenAI-compatible adapter** now serves Gemini, and
would serve Groq, GitHub Models, Cerebras or a local Ollama model, with configuration only. First
real answers: 3 out of 3 right, including "not in the report" for the CEO's pay.

Running a local model was considered: an M4 Pro with 24 GB fits Gemma 4 26B or Qwen 27B. Gemini
Flash stays for now.

### 15. Answer evaluation

`rag eval-answers` answers every golden question with the full pipeline and grades it:
correctness and faithfulness with an LLM judge, valid citations and key figures with code, and
whether a wrong answer was a retrieval or a generation failure. The first run:

- **46 of 52 answerable questions correct (88%)**, held-out 31 of 33 (94%)
- 4 of 4 graded unanswerable questions answered "not in the report"
- 98% faithful, 100% valid citations
- Wrong answers: 2 retrieval failures (balance-sheet rows), 1 generation failure (a figure that
  was in the sources, reported as missing), 3 partial answers
- 2 questions not answered because of the free tier's rate limit

The judge is the answer model itself and was inconsistent about prior-year details, so the
strict score lies between about 82% and 94%.

### 16. Documented

[EVALUATION.md](EVALUATION.md) explains what is tested and the metrics; the
[glossary](RAG_GLOSSARY.md) explains the terms; the [README](../README.md) has every scoreboard.

---

## Lessons

1. **Measure before adding complexity.** Textbook hybrid search made results worse; a better
   index (table rows) beat a more expensive model (the reranker).
2. **Hold questions out.** Two conclusions reversed on unseen questions: the reranker's value, and
   the confidence gate, which only fitted the question it was designed from.
3. **Label evidence by page and section,** not chunk id, so one dataset can compare chunking
   strategies.
4. **Separate retrieval failures from generation failures** before fixing either.
5. **Evaluation costs money and quota.** Cache what does not change and skip what is not scored.
6. **Keep providers behind interfaces.** Bedrock, Titan, Pinecone, Gemini: every provider
   change touched one adapter and a setting.
7. **Report limits with the numbers.** One document, 58 developer-written questions, and a judge
   grading its own model.

## What is next

- Retrieval for single balance-sheet rows, the main remaining cause of wrong answers.
- A consistent judge (or a different judge model) and a refined key-number check.
- Re-run the two rate-limited questions; more questions, ideally from real users.
- A local reranker, to remove the monthly rerank quota.
