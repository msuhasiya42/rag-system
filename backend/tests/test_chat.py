from collections.abc import Callable, Iterator

from rag_system.answering import AnswerService
from rag_system.chat import run_chat
from rag_system.domain import Chunk
from rag_system.retrieval import DenseRetriever
from tests.fakes import FakeChatModel, FakeEmbedder, InMemoryVectorStore, as_units


def _service(model: FakeChatModel) -> AnswerService:
    store = InMemoryVectorStore()
    chunk = Chunk("report#00000", "report", "report.pdf", 0, "Revenue was 12 million", 3, 3)
    store.upsert(as_units([chunk]), FakeEmbedder().embed_documents([chunk.text]))
    return AnswerService(DenseRetriever(FakeEmbedder(), store), model)


def _inputs(*lines: str | type[BaseException]) -> Callable[[str], str]:
    queue: Iterator[str | type[BaseException]] = iter(lines)

    def read(_prompt: str) -> str:
        item = next(queue)
        if isinstance(item, type):
            raise item()
        return item

    return read


def test_chat_answers_each_question_with_sources_until_exit() -> None:
    output: list[str] = []
    model = FakeChatModel(reply="Revenue was $12 million [1].")

    run_chat(_service(model), 3, _inputs("What was revenue?", "", "exit"), output.append)

    transcript = "\n".join(output)
    assert "Revenue was $12 million [1]." in transcript
    assert "[1] report.pdf, page 3" in transcript
    assert len(model.prompts) == 1  # the blank line was skipped


def test_chat_survives_a_failed_question_and_ends_on_ctrl_d() -> None:
    output: list[str] = []

    run_chat(_service(FakeChatModel(fail=True)), 3, _inputs("Revenue?", EOFError), output.append)

    assert any(line.startswith("Error:") for line in output)
