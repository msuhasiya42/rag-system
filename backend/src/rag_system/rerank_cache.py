"""A disk cache in front of a reranker, for evaluation runs.

Reranking is billed per request (Pinecone's free plan allows 500 a month) and evaluation repeats
the same requests: an unchanged question with unchanged candidates gets the same scores. The
cache key covers the model, the question and every candidate's id and text, so any change to
retrieval or chunking is a cache miss and is scored afresh.
"""

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path

from rag_system.domain import SearchHit
from rag_system.ports import Reranker


class CachingReranker(Reranker):
    def __init__(self, inner: Reranker, model: str, path: Path) -> None:
        self._inner = inner
        self._model = model
        self._path = path
        self._entries: dict[str, list[list[float]]] = (
            json.loads(path.read_text()) if path.exists() else {}
        )
        self.hits = 0
        self.misses = 0

    def rerank(self, question: str, hits: Sequence[SearchHit], top_n: int) -> list[SearchHit]:
        if not hits:
            return []
        key = self._key(question, hits)
        if key not in self._entries:
            self.misses += 1
            # Rank every candidate once, so any later top_n can be served from the cache.
            ranked = self._inner.rerank(question, hits, len(hits))
            position = {hit.chunk.id: index for index, hit in enumerate(hits)}
            self._entries[key] = [[position[hit.chunk.id], hit.score] for hit in ranked]
            self._save()
        else:
            self.hits += 1
        return [SearchHit(hits[int(index)].chunk, score) for index, score in self._entries[key]][
            :top_n
        ]

    def _key(self, question: str, hits: Sequence[SearchHit]) -> str:
        digest = hashlib.sha256()
        for part in (
            self._model,
            question,
            *(f"{h.chunk.id}\n{h.chunk.contextual_text()}" for h in hits),
        ):
            digest.update(part.encode())
            digest.update(b"\0")
        return digest.hexdigest()

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._entries))
