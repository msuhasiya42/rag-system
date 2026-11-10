"""Pinecone Inference: hosted dense embeddings, sparse embeddings and reranking."""

import logging
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any, TypeVar

from pinecone import Pinecone
from pinecone.errors import PineconeError, RateLimitError

from rag_system.domain import SearchHit, SparseVector
from rag_system.ports import Embedder, Reranker, SparseEncoder, UpstreamServiceError

logger = logging.getLogger(__name__)

MAX_BATCH_SIZE = 96  # Pinecone Inference embedding limit per request
# Every call here is idempotent, so a rate-limited request is safe to retry after a pause.
RATE_LIMIT_RETRIES = 5
RATE_LIMIT_FIRST_WAIT_SECONDS = 5.0

Sleep = Callable[[float], None]
T = TypeVar("T")


class _InferenceClient:
    def __init__(self, client: Pinecone, model: str, sleep: Sleep = time.sleep) -> None:
        self._client = client
        self._model = model
        self._sleep = sleep

    def _call(self, what: str, request: Callable[[], T]) -> T:
        wait = RATE_LIMIT_FIRST_WAIT_SECONDS
        for attempt in range(1, RATE_LIMIT_RETRIES + 1):
            try:
                return request()
            except RateLimitError as error:
                if attempt == RATE_LIMIT_RETRIES:
                    raise UpstreamServiceError(f"Pinecone {what} rate limited: {error}") from error
                logger.warning("Rate limited; retrying in %.0f s (attempt %d)", wait, attempt)
                self._sleep(wait)
                wait *= 2
            except PineconeError as error:
                raise UpstreamServiceError(f"Pinecone {what} failed: {error}") from error
        raise AssertionError("unreachable")

    def _embed(self, texts: Sequence[str], parameters: Mapping[str, object]) -> list[Any]:
        response = self._call(
            "embedding",
            lambda: self._client.inference.embed(
                model=self._model, inputs=list(texts), parameters=parameters
            ),
        )
        return list(response.data)

    def _embed_in_batches(
        self, texts: Sequence[str], parameters: Mapping[str, object]
    ) -> list[Any]:
        items: list[Any] = []
        for start in range(0, len(texts), MAX_BATCH_SIZE):
            batch = texts[start : start + MAX_BATCH_SIZE]
            items.extend(self._embed(batch, parameters))
            logger.info("%s: %d/%d texts", self._model, start + len(batch), len(texts))
        return items


class PineconeEmbedder(_InferenceClient, Embedder):
    """Dense embeddings, e.g. llama-text-embed-v2."""

    def __init__(self, client: Pinecone, model: str, dimensions: int, sleep: Sleep = time.sleep):
        super().__init__(client, model, sleep)
        self._dimensions = dimensions

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [_dense(item) for item in self._embed_in_batches(texts, self._parameters("passage"))]

    def embed_query(self, text: str) -> list[float]:
        return _dense(self._embed([text], self._parameters("query"))[0])

    def _parameters(self, input_type: str) -> dict[str, object]:
        return {"input_type": input_type, "truncate": "END", "dimension": self._dimensions}


class PineconeSparseEncoder(_InferenceClient, SparseEncoder):
    """Learned sparse embeddings, e.g. pinecone-sparse-english-v0."""

    def encode_documents(self, texts: Sequence[str]) -> list[SparseVector]:
        parameters = {"input_type": "passage", "truncate": "END"}
        return [_sparse(item) for item in self._embed_in_batches(texts, parameters)]

    def encode_query(self, text: str) -> SparseVector:
        return _sparse(self._embed([text], {"input_type": "query", "truncate": "END"})[0])


class PineconeReranker(_InferenceClient, Reranker):
    """Cross-encoder reranking, e.g. bge-reranker-v2-m3."""

    def rerank(self, question: str, hits: Sequence[SearchHit], top_n: int) -> list[SearchHit]:
        if not hits:
            return []
        # The reranker sees the section path too: it tells it what the text is about.
        documents = [{"id": hit.chunk.id, "text": hit.chunk.contextual_text()} for hit in hits]
        response = self._call(
            "rerank",
            lambda: self._client.inference.rerank(
                model=self._model,
                query=question,
                documents=documents,
                rank_fields=["text"],
                return_documents=False,
                top_n=min(top_n, len(hits)),
                parameters={"truncate": "END"},
            ),
        )
        return [SearchHit(hits[row.index].chunk, row.score) for row in response.data]


# The SDK types embedding items loosely (dense and sparse share one union), hence Any.
def _dense(item: Any) -> list[float]:
    return [float(value) for value in item.values]


def _sparse(item: Any) -> SparseVector:
    return SparseVector(
        indices=[int(index) for index in item.sparse_indices],
        values=[float(value) for value in item.sparse_values],
    )
