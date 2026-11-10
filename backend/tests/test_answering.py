from rag_system.answering import NO_RESULTS_ANSWER, AnswerService
from rag_system.domain import Chunk
from rag_system.retrieval import DenseRetriever
from tests.fakes import FakeChatModel, FakeEmbedder, InMemoryVectorStore, as_units


def _chunk(index: int, text: str, page: int) -> Chunk:
    return Chunk(f"report#{index:05d}", "report", "report.pdf", index, text, page, page)


def _store_with(*chunks: Chunk) -> InMemoryVectorStore:
    store = InMemoryVectorStore()
    embedder = FakeEmbedder()
    store.upsert(as_units(chunks), embedder.embed_documents([chunk.text for chunk in chunks]))
    return store


def test_answer_cites_the_retrieved_sources() -> None:
    store = _store_with(
        _chunk(0, "Insurance float reached 176 billion dollars", 7),
        _chunk(1, "The railroad carried more freight", 40),
    )
    model = FakeChatModel(reply="Float was $176 billion [1].")

    answer = AnswerService(DenseRetriever(FakeEmbedder(), store), model).answer(
        "How large was float?", top_k=1
    )

    assert answer.text == "Float was $176 billion [1]."
    assert answer.model_id == "fake-model"
    assert [hit.chunk.page_start for hit in answer.sources] == [7]
    _, user_prompt = model.prompts[0]
    assert "[1] report.pdf, page 7" in user_prompt
    assert "Insurance float reached 176 billion dollars" in user_prompt
    assert user_prompt.endswith("Question: How large was float?")


def test_retrieval_returns_at_most_top_k_sources() -> None:
    store = _store_with(*[_chunk(i, f"fact number {i}", i) for i in range(10)])

    answer = AnswerService(DenseRetriever(FakeEmbedder(), store), FakeChatModel()).answer(
        "fact", top_k=3
    )

    assert len(answer.sources) == 3


def test_empty_index_answers_without_calling_the_model() -> None:
    model = FakeChatModel()

    answer = AnswerService(DenseRetriever(FakeEmbedder(), InMemoryVectorStore()), model).answer(
        "Anything?", 5
    )

    assert answer.text == NO_RESULTS_ANSWER
    assert answer.model_id is None
    assert model.prompts == []
