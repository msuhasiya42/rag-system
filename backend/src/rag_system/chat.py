"""Interactive terminal chat: ask questions, get answers with page citations."""

from collections.abc import Callable

from rag_system.answering import AnswerService, describe_source
from rag_system.domain import Answer, SearchHit
from rag_system.ports import UpstreamServiceError

EXIT_COMMANDS = frozenset({"exit", "quit", ":q"})


def run_chat(
    service: AnswerService,
    top_k: int,
    read: Callable[[str], str] = input,
    write: Callable[[str], None] = print,
) -> None:
    write("Ask a question about your documents. Type 'exit' or press Ctrl-D to quit.\n")
    while True:
        try:
            question = read("> ").strip()
        except (EOFError, KeyboardInterrupt):
            write("")
            return
        if not question:
            continue
        if question.lower() in EXIT_COMMANDS:
            return
        try:
            write(format_answer(service.answer(question, top_k)))
        except UpstreamServiceError as error:
            # Keep the session alive; the next question may succeed.
            write(f"Error: {error}\n")


def format_answer(answer: Answer) -> str:
    lines = [answer.text, ""]
    if answer.sources:
        lines.append("Sources:")
        lines.extend(
            f"  [{number}] {describe_source(hit)}  (score {hit.score:.2f})"
            for number, hit in enumerate(answer.sources, start=1)
        )
    if answer.model_id:
        lines.append(f"  model: {answer.model_id}")
    lines.append("")
    return "\n".join(lines)


def format_hits(hits: list[SearchHit], preview_chars: int = 300) -> str:
    """Retrieved chunks with their source, score and the start of their text."""
    blocks = []
    for number, hit in enumerate(hits, start=1):
        preview = " ".join(hit.chunk.text.split())[:preview_chars]
        blocks.append(f"[{number}] {describe_source(hit)}  (score {hit.score:.3f})\n    {preview}")
    return "\n\n".join(blocks) or "No results."
