"""The Pinecone Inference adapter against a stub client: batching, input types, retries."""

from types import SimpleNamespace
from typing import Any

import pytest
from pinecone.errors import ApiError, RateLimitError

from rag_system.clients.pinecone_inference import (
    PineconeEmbedder,
    PineconeReranker,
    PineconeSparseEncoder,
)
from rag_system.domain import Chunk, SearchHit, SparseVector
from rag_system.ports import UpstreamServiceError


class StubInference:
    def __init__(self, failures: list[Exception] | None = None) -> None:
        self.failures = failures or []
        self.calls: list[dict[str, Any]] = []

    def embed(self, model: str, inputs: list[str], parameters: dict[str, Any]) -> Any:
        self.calls.append({"model": model, "inputs": inputs, **parameters})
        if self.failures:
            raise self.failures.pop(0)
        data = [SimpleNamespace(values=[float(len(text))] * 4) for text in inputs]
        return SimpleNamespace(data=data)


def _embedder(inference: StubInference, waits: list[float] | None = None) -> PineconeEmbedder:
    client: Any = SimpleNamespace(inference=inference)
    sleep = (waits if waits is not None else []).append
    return PineconeEmbedder(client, "llama-text-embed-v2", 4, sleep=sleep)


def test_documents_are_embedded_as_passages_in_batches_of_96() -> None:
    inference = StubInference()
    texts = [f"chunk {i}" for i in range(200)]

    vectors = _embedder(inference).embed_documents(texts)

    assert [len(call["inputs"]) for call in inference.calls] == [96, 96, 8]
    assert {call["input_type"] for call in inference.calls} == {"passage"}
    assert inference.calls[0]["dimension"] == 4
    assert len(vectors) == 200
    assert vectors[150][0] == float(len("chunk 150"))  # order is kept across batches


def test_questions_are_embedded_as_queries() -> None:
    inference = StubInference()

    vector = _embedder(inference).embed_query("What was revenue?")

    assert vector == [17.0] * 4
    assert inference.calls[0]["input_type"] == "query"


def test_rate_limits_are_retried_with_growing_waits() -> None:
    inference = StubInference(failures=[RateLimitError(), RateLimitError()])
    waits: list[float] = []

    vector = _embedder(inference, waits).embed_query("hi")

    assert vector == [2.0] * 4
    assert waits == [5.0, 10.0]


def test_persistent_rate_limits_and_other_errors_raise_upstream_errors() -> None:
    with pytest.raises(UpstreamServiceError, match="rate limited"):
        _embedder(StubInference(failures=[RateLimitError()] * 5)).embed_query("hi")

    with pytest.raises(UpstreamServiceError, match="embedding failed"):
        _embedder(StubInference(failures=[ApiError("bad", 400)])).embed_query("hi")


class StubSparseAndRerank:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def embed(self, model: str, inputs: list[str], parameters: dict[str, Any]) -> Any:
        self.calls.append({"inputs": inputs, **parameters})
        data = [SimpleNamespace(sparse_indices=[7, 9], sparse_values=[0.5, 1.5]) for _ in inputs]
        return SimpleNamespace(data=data)

    def rerank(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        # The stub prefers the second document.
        rows = [SimpleNamespace(index=1, score=0.9), SimpleNamespace(index=0, score=0.1)]
        return SimpleNamespace(data=rows[: kwargs["top_n"]])


def test_sparse_encoder_returns_indices_and_weights() -> None:
    inference = StubSparseAndRerank()
    encoder = PineconeSparseEncoder(SimpleNamespace(inference=inference), "sparse")  # type: ignore[arg-type]

    [vector] = encoder.encode_documents(["1.125% Senior Notes"])
    query = encoder.encode_query("senior notes")

    assert vector == SparseVector(indices=[7, 9], values=[0.5, 1.5])
    assert query.indices == [7, 9]
    assert [call["input_type"] for call in inference.calls] == ["passage", "query"]


def test_reranker_sends_section_and_text_and_returns_hits_in_its_order() -> None:
    inference = StubSparseAndRerank()
    reranker = PineconeReranker(SimpleNamespace(inference=inference), "bge")  # type: ignore[arg-type]
    hits = [
        SearchHit(Chunk("d#0", "d", "d.pdf", 0, "noise", 1, 1, "Other"), 0.8),
        SearchHit(Chunk("d#1", "d", "d.pdf", 1, "Notes due 2027", 25, 25, "Cover"), 0.2),
    ]

    ranked = reranker.rerank("When are the notes due?", hits, top_n=1)

    assert [(hit.chunk.id, hit.score) for hit in ranked] == [("d#1", 0.9)]
    sent = inference.calls[0]["documents"]
    assert sent[1] == {"id": "d#1", "text": "Cover\nNotes due 2027"}
    assert reranker.rerank("q", [], top_n=3) == []
