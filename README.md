# RAG System

Ask questions about company PDFs and get answers that cite the document and page. Built with Flask,
Pinecone (vector index, and `llama-text-embed-v2` embeddings through Pinecone Inference), Amazon
Bedrock (answer model; Titan embeddings as an alternative) and AWS Textract for scanned pages.

```
INGEST  PDF ─► lines + font style, outline (pypdfium2) ─┬─► clean ─► sections ─► chunks ─► llama-text-embed-v2 ─► Pinecone
        scanned pages ─► OCR (Textract) ────────────────┘

        clean:    drop headers, footers, page numbers, leader dots; join hyphenated words
        sections: outline + bold headings; inside each, paragraphs and tables
        chunks:   ≤1800 chars, never across sections, tables kept whole; section path embedded

        every chunk is written to two indexes: dense (llama-text-embed-v2) and sparse
        (pinecone-sparse-english-v0, exact terms)

ASK     question ─► retrieval pipeline (below) ─► answer model on Bedrock ─► answer with [n] citations
                                                  (Qwen3-235B, falls back to Nova 2 Lite)
```

## Setup

Requires Python 3.12, [uv](https://docs.astral.sh/uv/), an AWS CLI profile with Bedrock access, and
a Pinecone API key.

```bash
cd backend
uv sync
cp .env.example .env          # then set PINECONE_API_KEY and RAG_AWS_PROFILE
```

OCR needs `textract:DetectDocumentText`. Attach [the policy](backend/config/iam/textract-ocr.json) to
the IAM user (in CloudShell, or from an admin profile):

```bash
aws iam put-user-policy --user-name contextforge-dev --policy-name TextractOcr \
  --policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":"textract:DetectDocumentText","Resource":"*"}]}'
```

## Use

```bash
cd backend
mkdir -p ../data/documents
curl -fL -o ../data/documents/berkshire-2025-annual-report.pdf \
  https://www.berkshirehathaway.com/2025ar/2025ar.pdf

uv run rag ingest ../data/documents/*.pdf      # creates the Pinecone index on first run
uv run rag search "Does Berkshire pay a dividend?"   # retrieved chunks only, no answer model
uv run rag chat                                # interactive: ask, read answer, repeat; 'exit' quits
uv run rag ask "How did insurance float change in 2025?"   # one question
uv run rag serve                               # Flask API on http://127.0.0.1:5001
```

Output shape (illustrative values):

```text
> How did insurance float change in 2025?
Float grew to ... [1][2].

Sources:
  [1] berkshire-2025-annual-report.pdf, pages 7-8  (score 0.62)
  [2] berkshire-2025-annual-report.pdf, page 31  (score 0.58)
  model: qwen.qwen3-235b-a22b-2507-v1:0
```

```bash
curl -s localhost:5001/api/ask -H 'content-type: application/json' \
  -d '{"question": "Who are the directors of the company?", "topK": 5}'
```

`POST /api/ask` takes `{"question": str, "topK"?: 1-20}` and returns
`{"answer", "modelId", "sources": [{"number", "documentName", "section", "pageStart", "pageEnd", "pagesLabel", "score", "text"}]}`.
Errors use `{"error": {"code", "message", "details"?}}`: 400 for invalid input, 502 when Bedrock or
Pinecone fails (details are logged, not returned).

## Retrieval pipelines and evaluation

Pipelines are stacks of small retrievers (`retrieval.py`), chosen with `--pipeline` or
`RAG_RETRIEVAL_PIPELINE`:

| Pipeline | Stack |
|---|---|
| `basic` | `DenseRetriever` |
| `hybrid` | `HybridRetriever(Dense, Sparse)`: both in parallel, weighted reciprocal rank fusion |
| `advanced` (default) | `RerankingRetriever(Hybrid)`: `bge-reranker-v2-m3` reranks the top 20, and its order is blended with the hybrid order (weighted RRF) |
| `basic-rerank` | `RerankingRetriever(Dense)`: isolates the reranker's effect |

`evals/golden.json` holds 58 questions about the Berkshire report (52 answerable, 6 deliberately
unanswerable), with evidence labelled by page and section so it survives chunking changes. A fact
stated in two places lists both as alternatives. Questions are split:

- **tune** (q01-q22): used to choose settings (RRF k, sparse weight).
- **test** (q23-q58): held out, only used to check whether those choices generalise.

`uv run rag eval --pipeline all --label <name>` prints a per-question rank matrix and a
scoreboard per split, and saves JSON to `evals/results/`.

Held-out **test** split (33 answerable questions, k=10), each row adding one step:

| Pipeline | Hit@1 | Hit@5 | Recall@5 | nDCG@5 | MRR | p50 latency |
|---|---|---|---|---|---|---|
| basic: dense | 61% | 79% | 79% | 70% | 0.690 | ~600 ms |
| hybrid: + sparse, RRF k=1, sparse weight 0.5 | 58% | 91% | 91% | 75% | 0.697 | ~590 ms |
| advanced, pure rerank (reranker order replaces hybrid order) | 70% | 91% | 91% | 80% | 0.778 | ~1,500 ms |
| **advanced: rerank blended with hybrid order (weight 2)** | **70%** | **97%** | **97%** | **83%** | **0.790** | ~1,300 ms |
| basic-rerank, blended | 67% | 91% | 91% | 79% | 0.765 | ~1,470 ms |

Tune split (19 answerable questions), where the settings were chosen:

| Pipeline | Hit@1 | Hit@5 | Recall@5 | nDCG@5 | MRR |
|---|---|---|---|---|---|
| basic | 74% | 89% | 87% | 81% | 0.795 |
| hybrid, textbook RRF (k=60, equal weights) | 58% | 84% | 79% | 69% | 0.690 |
| hybrid, tuned (k=1, sparse weight 0.5) | 74% | 95% | 89% | 82% | 0.811 |
| advanced, pure rerank | 68% | 89% | 84% | 78% | 0.789 |
| **advanced, blended** | **74%** | **95%** | **89%** | **82%** | **0.816** |

MRR by question type (all 52 answerable questions):

| Type | basic | hybrid | advanced, pure rerank | advanced, blended |
|---|---|---|---|---|
| exact term (7) | 0.620 | 0.683 | 0.833 | 0.857 |
| multi-section (4) | 0.812 | 0.812 | 1.000 | 1.000 |
| paraphrase (8) | 0.838 | 0.775 | 0.585 | 0.671 |
| prose fact (18) | 0.881 | 0.898 | 0.935 | 0.921 |
| table number (15) | 0.514 | 0.536 | 0.622 | 0.641 |

What the experiments showed:
- **Textbook hybrid search lowered quality.** The sparse model weights every query word equally
  (`the`, `of`, `and` included), so its list is noisy on a 10-K. With RRF's usual k=60, ranks 1
  and 3 score almost the same, so a chunk appearing in both lists outvotes dense search's correct
  first result. A small k and a lower sparse weight fixed it.
- **Sparse search fixes exact terms:** "When are the 1.125% Senior Notes due?" went from rank 10
  (dense) to 4 (hybrid) to 1 (reranked).
- **The held-out split reversed the reranker verdict.** On the tune split it looked useless
  (MRR -0.005), because one of its failures, the paraphrase "hand out its profits as regular
  payouts", sat there: every candidate scored below 0.007, so it was ordering noise. On the
  36 held-out questions it is the largest gain (MRR +0.088), lifting table numbers and exact
  terms (total liabilities: not found to rank 1; BNSF tax rate: 7 to 1). It still loses some
  (U.S. federal tax paid: rank 2 to not found) and doubles latency. Cohere rerankers are not
  available on Pinecone's free plan.
- **Blending the reranker with the hybrid order beat replacing it.** A cross-encoder favours
  literal word overlap, so pure reranking dropped paraphrase MRR from 0.775 to 0.585. Fusing
  its order with the hybrid order (weighted RRF, reranker weight 2) brought every paraphrase
  answer back into the top 5 and raised held-out Hit@5 from 91% to 97%. A "confidence gate"
  (skip the reranker when its top score is low) scored best on the tune split but never fired
  on held-out questions: it had been fitted to the one tune question it was designed from.
- **Tuned hybrid generalised for recall, not ordering:** on held-out questions Hit@5 rose from
  79% to 91%, but Hit@1 fell from 61% to 58%. It finds more evidence; the reranker orders it.
- **Weakest category: table numbers.** "Cash and cash equivalents" and "total assets" are single
  rows inside balance-sheet chunks; prose that discusses them outranks the table.
- **Caveat:** 33 held-out questions is still small: one question is about 3 points of Hit@K.

## Checks

```bash
cd backend
uv run pytest            # offline: fakes replace Bedrock, Textract and Pinecone
uv run ruff check . && uv run ruff format --check .
uv run mypy src tests    # strict
```

## Design decisions

- **pypdfium2, not pdfplumber, for the text layer.** On the 154-page Berkshire report, pdfplumber
  took about 40 s and produced merged words (`Thetypeandvolumeof...`) and doubled bold letters
  (`Ottther`). pypdfium2 took under 1 s with clean text, and its license (Apache/BSD) is fine for
  commercial use (PyMuPDF's AGPL is not).
- **OCR only when a page has little text and contains an image.** Blank separator pages are skipped,
  so Textract is not paid for them. The synchronous `DetectDocumentText` API takes one rendered page
  at a time, so no S3 bucket or async job is needed. It returns lines only; table structure
  (`AnalyzeDocument`) costs about ten times more.
- **Header, footer and page-number removal.** Lines repeated in the top or bottom two lines on 30% or
  more of pages are removed, as are lines that look like page numbers (`12`, `Page 3 of 10`,
  `K-55`). Otherwise every chunk would contain the same noise.
- **Structure-aware chunking** (`ingestion/structure.py` finds sections, `ingestion/chunking.py`
  packs them). Designed from an analysis of the Berkshire report:
  - Sections come from the PDF outline plus heading lines. The PDF scales text through its
    transformation matrix, so font size is measured from glyph boxes (the reported size is 1.0).
    Headings are bold body-size text or larger text; `Item 1A.` / `Chapter 3` are level 2,
    `(12)` / `3.1` level 3, other headings level 4. Bookmarks point only at a page, so each is
    anchored to its heading line (`Risk Factors` starts mid-page 50 at `Item 1A. Risk Factors`).
  - Not headings: a heading repeated at the top of a continuation page, running headers, bold
    table labels (`Assets:`), numbers, and the lines of a contents page. Wrapped bold headings
    are joined (`…impair our ability to` + `access capital markets…`).
  - Inside a section, blocks are paragraphs (first-line indent or vertical gap) and tables (lines
    ending in two or more numeric cells, plus the column headings and units above them).
  - Chunks never cross a section. A small section is one chunk, so `Dividends: Berkshire has not
    declared a cash dividend since 1967.` stays a precise answer. Large sections split between
    blocks. Tables are kept whole (every table in the report but three fits in a chunk); larger
    ones split between rows and repeat their column headings, so numbers keep year and units.
  - The section path (`FORM 10-K > Item 7. Management's Discussion… > Insurance—Underwriting`) is
    embedded with the text and shown in citations.
  - Without fonts (OCR) or an outline, the document is one section and chunking is size-based.
  - Result on the report: 133 sections, 461 chunks (median 1,447 chars), in about 2 s.
- **Re-ingesting replaces a document.** Chunk ids are `<document-id>#<n>` from the file name.
  Ingest embeds first, then deletes the old ids by prefix and upserts, so a failed run leaves
  the previous version searchable. Serverless Pinecone cannot delete by metadata filter.
- **Ports and adapters.** `ports.py` defines `Embedder`, `VectorStore`, `ChatModel` and `OcrEngine`;
  only `wiring.py` knows the concrete clients. Swapping Pinecone for pgvector or S3 Vectors, or
  Bedrock for another provider, is one adapter.
- **Answer prompt.** Sources are numbered, the model must cite `[n]`, say when the sources lack the
  answer, and treat source text as data rather than instructions (prompt-injection guard).

## Known limitations

- Tables are text rows (`Goodwill 55,945 56,860`) without cell structure; multi-line column
  headings (as in the S&P 500 comparison) read less clearly than single-line ones.
- Heading rules are tuned on one report; other layouts (e.g. headings marked only by size or
  colour) may need adjustments. Scanned pages get no headings.
- Pinecone's free plan runs in `us-east-1`, so document text leaves the Sydney region.
- No upload endpoint or web UI: documents are ingested from the CLI, and questions are asked in the
  terminal (`rag chat`) or through the API.
- No retrieval evaluation set yet (recall@k on known question/page pairs).
