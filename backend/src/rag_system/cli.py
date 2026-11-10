"""Command line: `rag ingest <pdf>...`, `rag search|ask "<question>"`, `rag chat`, `rag serve`."""

import argparse
import logging
import sys
from pathlib import Path

from rag_system.chat import format_answer, format_hits, run_chat
from rag_system.config import Settings
from rag_system.ports import UpstreamServiceError
from rag_system.wiring import (
    ConfigurationError,
    build_answer_service,
    build_ingestion_service,
    build_vector_store,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rag", description="Question answering over PDFs.")
    commands = parser.add_subparsers(dest="command", required=True)

    ingest = commands.add_parser("ingest", help="Index PDF files (re-ingesting replaces them).")
    ingest.add_argument("paths", nargs="+", type=Path)

    ask = commands.add_parser("ask", help="Ask a question from the terminal.")
    ask.add_argument("question")
    ask.add_argument("--top-k", type=int, default=None)

    search = commands.add_parser(
        "search", help="Show the chunks retrieved for a question (no answer model)."
    )
    search.add_argument("question")
    search.add_argument("--top-k", type=int, default=None)

    chat = commands.add_parser("chat", help="Ask questions interactively in the terminal.")
    chat.add_argument("--top-k", type=int, default=None)

    serve = commands.add_parser("serve", help="Run the Flask development server.")
    serve.add_argument("--port", type=int, default=5001)  # macOS AirPlay uses 5000

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
        if args.command == "ask":
            return _ask(settings, args.question, args.top_k or settings.default_top_k)
        if args.command == "search":
            return _search(settings, args.question, args.top_k or settings.default_top_k)
        if args.command == "chat":
            return _chat(settings, args.top_k or settings.default_top_k)
        return _serve(settings, args.port)
    except (ConfigurationError, UpstreamServiceError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


def _ingest(settings: Settings, paths: list[Path]) -> int:
    missing = [path for path in paths if not path.is_file() or path.suffix.lower() != ".pdf"]
    if missing:
        print(f"Not PDF files: {', '.join(map(str, missing))}", file=sys.stderr)
        return 1
    store = build_vector_store(settings)
    if store.ensure_index(
        settings.embedding_dimensions, settings.pinecone_cloud, settings.pinecone_region
    ):
        print(f"Created Pinecone index '{settings.pinecone_index}'.")
    service = build_ingestion_service(settings, store)
    for path in paths:
        report = service.ingest_pdf(path)
        print(
            f"{report.document_name}: {report.page_count} pages "
            f"({report.ocr_pages} OCR, {report.empty_pages} empty), "
            f"{report.section_count} sections -> {report.chunk_count} chunks"
        )
    return 0


def _ask(settings: Settings, question: str, top_k: int) -> int:
    print(format_answer(build_answer_service(settings).answer(question, top_k)))
    return 0


def _search(settings: Settings, question: str, top_k: int) -> int:
    print(format_hits(build_answer_service(settings).retrieve(question, top_k)))
    return 0


def _chat(settings: Settings, top_k: int) -> int:
    import readline  # noqa: F401  (arrow keys and history in input())

    run_chat(build_answer_service(settings), top_k)
    return 0


def _serve(settings: Settings, port: int) -> int:
    from rag_system.api.app import create_app

    app = create_app(build_answer_service(settings), settings.default_top_k)
    app.run(host="127.0.0.1", port=port, debug=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
