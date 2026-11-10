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

ASK     question ─► llama-text-embed-v2 ─► Pinecone top-k ─► answer model on Bedrock ─► answer with [n] citations
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
