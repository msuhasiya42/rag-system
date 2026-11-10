"""The Pinecone Inference adapter against a stub client: batching, input types, retries."""

from types import SimpleNamespace
from typing import Any

import pytest
from pinecone.errors import ApiError, RateLimitError

from rag_system.clients.pinecone_embedder import PineconeEmbedder
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
