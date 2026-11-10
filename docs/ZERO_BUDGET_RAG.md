# AI Engineering #05: A RAG system on zero budget

One laptop. An internet connection. No credit card.

I built a RAG system over Berkshire Hathaway's 154-page annual report. Halfway through, my cloud
plan fell apart: Bedrock got blocked on my AWS account, and the free tiers I moved to had limits I
only found by hitting them.

So here's what I'd do with no money at all:

1️⃣ **Parse locally**
pypdfium2 reads all 154 pages, with font sizes and bold, in under a second. For scanned pages,
use Tesseract / OCRmyPDF instead of a paid OCR API.

2️⃣ **Embed locally**
bge-m3 or nomic-embed-text run fine on a laptop. You embed the documents once; each query is a
single embedding.

3️⃣ **Skip the vector database**
My whole report is 1,345 vectors. A brute-force cosine search over that in NumPy takes under a
millisecond. HNSW (see #03) matters at millions of vectors, not thousands. When you outgrow it:
LanceDB, Chroma or sqlite-vec. Still just files on disk.

4️⃣ **Keyword search is free too**
SQLite FTS5 or rank_bm25 gives you BM25. Fuse it with dense search using RRF, and tune k: the
textbook k=60 made my results worse (MRR 0.795 → 0.690).

5️⃣ **Run the reranker locally**
This one hurt. The free hosted reranker allows 500 calls a month, and one day of evaluation runs
used them all. bge-reranker-v2-m3 is the same model with open weights, and it runs on your
laptop with no quota.

6️⃣ **LLM: free API first, local fallback**
Gemini Flash's free tier answered 88% of my questions correctly, but rate limits (429, then 503)
dropped questions mid-run. For a fallback, Ollama runs a 7–8B model in 16 GB of RAM and a ~27B
quantized one in 24 GB. One OpenAI-compatible adapter covers both, so switching is a config
change.

7️⃣ **Evaluation is free, it just takes time**
58 hand-written questions, each labelled with the page that holds its answer. Cache every
reranker and LLM call so re-runs cost nothing. Use a different model for the judge than the one
writing the answers.

What zero budget costs: time, RAM and patience.
What it doesn't cost: quality. The changes that moved my numbers most were all free:
structure-aware chunking, indexing table rows on their own (MRR 0.690 → 0.798), and holding
questions out of tuning.

The expensive part of RAG isn't the API bill.
It's not knowing whether your changes help.

#RAG #AIEngineering #LLM #OpenSource

---

**What is measured and what is not.** Points 1, 4 and 7, and the numbers in 5 and 6, come from
this project ([EVALUATION.md](EVALUATION.md), [JOURNEY.md](JOURNEY.md)). The local options in
2, 3, 5 and 6 (local embeddings, brute-force search, a local reranker, Ollama) have not been run
here yet: this repository still embeds and reranks on Pinecone and answers with Gemini.
