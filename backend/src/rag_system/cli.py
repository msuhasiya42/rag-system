"""Command line: `rag ingest`, `rag search`, `rag ask`, `rag eval`, `rag chat`, `rag serve`."""

import argparse
import logging
import sys
from pathlib import Path
from typing import cast

from rag_system.answer_eval import AnswerJudge, run_answer_eval, summarize_answers
from rag_system.chat import format_answer, format_hits, run_chat
from rag_system.config import Settings
from rag_system.eval_report import (
    format_answer_report,
    format_rank_matrix,
    format_report,
    format_scoreboard,
    save_answer_results,
    save_results,
)
from rag_system.evaluation import (
    CaseResult,
    GoldenCase,
    load_golden_set,
    run_retrieval_eval,
    summarize,
)
from rag_system.ports import UpstreamServiceError
from rag_system.retrieval import (
    PIPELINE_DESCRIPTIONS,
    PIPELINES,
    USES_RERANKER,
    USES_SPARSE,
    Pipeline,
)
from rag_system.wiring import (
    ConfigurationError,
    Pinecones,
    build_answer_service,
    build_chat_model,
    build_ingestion_service,
    build_retriever,
)

# Evaluation re-runs score the same questions and candidates; reuse earlier reranker results.
RERANK_CACHE = Path("evals/.cache/rerank.json")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rag", description="Question answering over PDFs.")
    commands = parser.add_subparsers(dest="command", required=True)

    ingest = commands.add_parser("ingest", help="Index PDF files (re-ingesting replaces them).")
    ingest.add_argument("paths", nargs="+", type=Path)

    ask = commands.add_parser("ask", help="Ask a question from the terminal.")
    ask.add_argument("question")

    search = commands.add_parser(
        "search", help="Show the chunks retrieved for a question (no answer model)."
    )
    search.add_argument("question")

    evaluate = commands.add_parser("eval", help="Score retrieval against a golden dataset.")
    evaluate.add_argument("--golden", type=Path, default=Path("evals/golden.json"))
    evaluate.add_argument("--k", type=int, default=10, help="Chunks retrieved per question.")
    evaluate.add_argument("--label", default=None, help="Save results under evals/results/.")

    answers = commands.add_parser(
        "eval-answers", help="Answer every golden question and grade the answers."
    )
    answers.add_argument("--golden", type=Path, default=Path("evals/golden.json"))
    answers.add_argument("--label", default=None, help="Save results under evals/results/.")
    answers.add_argument("--limit", type=int, default=None, help="Only the first N questions.")
    answers.add_argument(
        "--pause", type=float, default=4.0, help="Seconds between model calls (free-tier limits)."
    )

    chat = commands.add_parser("chat", help="Ask questions interactively in the terminal.")

    serve = commands.add_parser("serve", help="Run the Flask development server.")
    serve.add_argument("--port", type=int, default=5001)  # macOS AirPlay uses 5000

    for command in (ask, search, chat, serve, answers):
        command.add_argument("--pipeline", choices=PIPELINES, default=None)
    for command in (ask, search, chat, answers):
        command.add_argument("--top-k", type=int, default=None)
    evaluate.add_argument(
        "--pipeline", choices=(*PIPELINES, "all"), default=None, help="'all' compares every one."
    )

    args = parser.parse_args(argv)
    # Chat output should be just questions and answers; other commands log progress.
    logging.basicConfig(
        level=logging.WARNING if args.command == "chat" else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = Settings()

    try:
        if args.command == "ingest":
            return _ingest(settings, args.paths)
        if args.command == "eval":
            return _evaluate(settings, args.golden, args.k, args.label, args.pipeline)
        pipeline: Pipeline = args.pipeline or settings.retrieval_pipeline
        if args.command == "eval-answers":
            return _evaluate_answers(
                settings,
                args.golden,
                pipeline,
                args.top_k or settings.default_top_k,
                args.label,
                args.limit,
                args.pause,
            )
        if args.command == "serve":
            return _serve(settings, args.port, pipeline)
        top_k = args.top_k or settings.default_top_k
        if args.command == "ask":
            return _ask(settings, args.question, top_k, pipeline)
        if args.command == "search":
            return _search(settings, args.question, top_k, pipeline)
        return _chat(settings, top_k, pipeline)
    except (ConfigurationError, UpstreamServiceError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


def _ingest(settings: Settings, paths: list[Path]) -> int:
    missing = [path for path in paths if not path.is_file() or path.suffix.lower() != ".pdf"]
    if missing:
        print(f"Not PDF files: {', '.join(map(str, missing))}", file=sys.stderr)
        return 1
    pinecones = Pinecones(settings)
    for name in pinecones.ensure_indexes(settings):
        print(f"Created Pinecone index '{name}'.")
    service = build_ingestion_service(settings, pinecones)
    for path in paths:
        report = service.ingest_pdf(path)
        print(
            f"{report.document_name}: {report.page_count} pages "
            f"({report.ocr_pages} OCR, {report.empty_pages} empty), "
            f"{report.section_count} sections -> {report.chunk_count} chunks "
            f"+ {report.table_row_count} table rows"
        )
    return 0


def _ask(settings: Settings, question: str, top_k: int, pipeline: Pipeline) -> int:
    print(format_answer(build_answer_service(settings, pipeline).answer(question, top_k)))
    return 0


def _search(settings: Settings, question: str, top_k: int, pipeline: Pipeline) -> int:
    print(format_hits(build_retriever(settings, pipeline).retrieve(question, top_k)))
    return 0


def _evaluate(
    settings: Settings, golden: Path, k: int, label: str | None, choice: str | None
) -> int:
    cases = load_golden_set(golden).cases
    ks = [n for n in (1, 3, 5, 10) if n <= k]
    pipelines: list[Pipeline] = (
        list(PIPELINES)
        if choice == "all"
        else [cast(Pipeline, choice or settings.retrieval_pipeline)]
    )
    runs: list[tuple[Pipeline, list[CaseResult]]] = []
    for pipeline in pipelines:
        results = _evaluate_one(
            settings, cases, k, ks, pipeline, label, verbose=len(pipelines) == 1
        )
        runs.append((pipeline, results))
    if len(runs) > 1:
        print("\nFirst relevant rank per question:\n")
        print(format_rank_matrix(runs))
    for split in ("tune", "test"):
        in_split = [(name, [r for r in results if r.case.split == split]) for name, results in runs]
        print(f"\nScoreboard, {split} split ({len(in_split[0][1])} questions, k={k}):\n")
        print(
            format_scoreboard(
                [(name, PIPELINE_DESCRIPTIONS[name], summarize(rs, ks)) for name, rs in in_split]
            )
        )
    return 0


def _evaluate_one(
    settings: Settings,
    cases: list[GoldenCase],
    k: int,
    ks: list[int],
    pipeline: Pipeline,
    label: str | None,
    verbose: bool,
) -> list[CaseResult]:
    print(f"=== {pipeline}: {PIPELINE_DESCRIPTIONS[pipeline]} ===")
    retriever = build_retriever(settings, pipeline, rerank_cache=RERANK_CACHE)
    results = run_retrieval_eval(retriever.retrieve, cases, k)
    summary = summarize(results, ks)
    if verbose:
        print(format_report(results, summary, ks))
    if label:
        config = {
            "pipeline": pipeline,
            "embeddingProvider": settings.embedding_provider,
            "embeddingModel": settings.pinecone_embedding_model
            if settings.embedding_provider == "pinecone"
            else settings.embedding_model_id,
            "index": settings.pinecone_index,
            "namespace": settings.pinecone_namespace,
            "chunkSizeChars": settings.chunk_size_chars,
            "chunkOverlapChars": settings.chunk_overlap_chars,
            "indexTableRows": settings.index_table_rows,
            "candidates": settings.retrieval_candidates,
            "rerankModel": settings.rerank_model if pipeline in USES_RERANKER else None,
            "rerankBlendWeight": settings.rerank_blend_weight
            if pipeline in USES_RERANKER
            else None,
            "sparseModel": settings.pinecone_sparse_model if pipeline in USES_SPARSE else None,
            "rrfK": settings.rrf_k if pipeline in USES_SPARSE else None,
            "sparseWeight": settings.hybrid_sparse_weight if pipeline in USES_SPARSE else None,
            "k": k,
        }
        path = save_results(Path("evals/results"), f"{label}-{pipeline}", config, results, summary)
        print(f"Saved {path}")
    return results


def _evaluate_answers(
    settings: Settings,
    golden: Path,
    pipeline: Pipeline,
    top_k: int,
    label: str | None,
    limit: int | None,
    pause: float,
) -> int:
    cases = load_golden_set(golden).cases[:limit]
    service = build_answer_service(settings, pipeline, rerank_cache=RERANK_CACHE)
    # The judge is the answer model: the only working model on this account. A model grading
    # its own answers can be lenient; the key-number check does not depend on it.
    judge = AnswerJudge(build_chat_model(settings))
    print(f"Answering {len(cases)} questions with pipeline '{pipeline}', top {top_k} sources")
    results = run_answer_eval(service, judge, cases, top_k, pause, progress=print)
    summary = summarize_answers(results)
    print("\n" + format_answer_report(results, summary))
    if label:
        config = {
            "pipeline": pipeline,
            "topK": top_k,
            "answerProvider": settings.answer_provider,
            "answerModels": settings.gemini_model_ids
            if settings.answer_provider == "gemini"
            else settings.answer_model_ids,
            "namespace": settings.pinecone_namespace,
        }
        path = save_answer_results(Path("evals/results"), label, config, results, summary)
        print(f"\nSaved {path}")
    return 0


def _chat(settings: Settings, top_k: int, pipeline: Pipeline) -> int:
    import readline  # noqa: F401  (arrow keys and history in input())

    run_chat(build_answer_service(settings, pipeline), top_k)
    return 0


def _serve(settings: Settings, port: int, pipeline: Pipeline) -> int:
    from rag_system.api.app import create_app

    app = create_app(build_answer_service(settings, pipeline), settings.default_top_k)
    app.run(host="127.0.0.1", port=port, debug=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
