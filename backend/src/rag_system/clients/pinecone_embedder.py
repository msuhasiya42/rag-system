"""Pinecone Inference embeddings (hosted models such as llama-text-embed-v2)."""

import logging
import time
from collections.abc import Callable, Sequence
from typing import cast

from pinecone import Pinecone
from pinecone.errors import PineconeError, RateLimitError

from rag_system.ports import Embedder, UpstreamServiceError

logger = logging.getLogger(__name__)

MAX_BATCH_SIZE = 96  # Pinecone Inference limit per request
# Embedding is idempotent, so a rate-limited batch is safe to retry after a pause.
RATE_LIMIT_RETRIES = 5
RATE_LIMIT_FIRST_WAIT_SECONDS = 5.0


class PineconeEmbedder(Embedder):
    def __init__(
        self,
        client: Pinecone,
        model: str,
        dimensions: int,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client
        self._model = model
        self._dimensions = dimensions
        self._sleep = sleep

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), MAX_BATCH_SIZE):
            batch = texts[start : start + MAX_BATCH_SIZE]
            vectors.extend(self._embed(batch, "passage"))
            logger.info("Embedded %d/%d chunks", min(start + len(batch), len(texts)), len(texts))
        return vectors

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text], "query")[0]

    def _embed(self, texts: Sequence[str], input_type: str) -> list[list[float]]:
        parameters = {"input_type": input_type, "truncate": "END", "dimension": self._dimensions}
        wait = RATE_LIMIT_FIRST_WAIT_SECONDS
        for attempt in range(1, RATE_LIMIT_RETRIES + 1):
            try:
                response = self._client.inference.embed(
                    model=self._model, inputs=list(texts), parameters=parameters
                )
                # Dense embeddings carry a float list; the SDK types the attribute loosely.
                return [list(cast(list[float], item.values)) for item in response.data]
            except RateLimitError as error:
                if attempt == RATE_LIMIT_RETRIES:
                    raise UpstreamServiceError(
                        f"Pinecone embedding rate limited: {error}"
                    ) from error
                logger.warning("Rate limited; retrying in %.0f s (attempt %d)", wait, attempt)
                self._sleep(wait)
                wait *= 2
            except PineconeError as error:
                raise UpstreamServiceError(f"Pinecone embedding failed: {error}") from error
        raise AssertionError("unreachable")
