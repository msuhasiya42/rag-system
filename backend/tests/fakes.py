"""In-memory stand-ins for Bedrock, Pinecone and Textract. No network in tests."""

import hashlib
import math
import re
from collections import Counter
from collections.abc import Callable, Sequence
from typing import TypeVar

from rag_system.domain import Chunk, SearchHit, SearchUnit, SparseVector
from rag_system.ports import (
    ChatModel,
    ChatReply,
    Embedder,
    OcrEngine,
    Reranker,
    SparseEncoder,
    UpstreamServiceError,
    VectorStore,
)

V = TypeVar("V")


def _words(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower())


def _bucket(word: str, size: int) -> int:
    return int(hashlib.md5(word.encode()).hexdigest(), 16) % size


class FakeEmbedder(Embedder):
    """Bag-of-words vectors: texts sharing words are similar, so retrieval is meaningful."""

    DIMENSIONS = 64

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.queries: list[str] = []

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        return self._vector(text)

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.DIMENSIONS
        for word in _words(text):
            vector[_bucket(word, self.DIMENSIONS)] += 1.0
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]


class FakeSparseEncoder(SparseEncoder):
    """Exact-word counts: only texts sharing literal words match, like keyword search."""

    VOCABULARY = 10_000

    def encode_documents(self, texts: Sequence[str]) -> list[SparseVector]:
        return [self._encode(text) for text in texts]

    def encode_query(self, text: str) -> SparseVector:
        return self._encode(text)

    def _encode(self, text: str) -> SparseVector:
        counts = Counter(_bucket(word, self.VOCABULARY) for word in _words(text))
        return SparseVector(indices=list(counts), values=[float(n) for n in counts.values()])


def _dense_dot(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def _sparse_dot(a: SparseVector, b: SparseVector) -> float:
    weights = dict(zip(b.indices, b.values, strict=True))
    return sum(
        value * weights.get(index, 0.0) for index, value in zip(a.indices, a.values, strict=True)
    )


def as_units(chunks: Sequence[Chunk]) -> list[SearchUnit]:
    return [SearchUnit.for_chunk(chunk) for chunk in chunks]


class InMemoryStore(VectorStore[V]):
    def __init__(self, score: Callable[[V, V], float]) -> None:
        self.records: dict[str, tuple[SearchUnit, V]] = {}
        self.operations: list[str] = []
        self._score = score

    def upsert(self, units: Sequence[SearchUnit], vectors: Sequence[V]) -> None:
        self.operations.append("upsert")
        for unit, vector in zip(units, vectors, strict=True):
            self.records[unit.id] = (unit, vector)

    def delete_document(self, document_id: str) -> None:
        self.operations.append(f"delete:{document_id}")
        for key in [key for key in self.records if key.startswith(f"{document_id}#")]:
            del self.records[key]

    def search(self, vector: V, top_k: int) -> list[SearchHit]:
        scored = [
            SearchHit(unit.chunk, self._score(vector, stored), unit.matched)
            for unit, stored in self.records.values()
        ]
        matches = [hit for hit in scored if hit.score > 0]
        return sorted(matches, key=lambda hit: hit.score, reverse=True)[:top_k]


class InMemoryVectorStore(InMemoryStore[Sequence[float]]):
    def __init__(self) -> None:
        super().__init__(_dense_dot)


class InMemorySparseStore(InMemoryStore[SparseVector]):
    def __init__(self) -> None:
        super().__init__(_sparse_dot)


class FakeReranker(Reranker):
    """Scores by how many question words appear in the chunk text."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int, int]] = []  # question, candidates, top_n

    def rerank(self, question: str, hits: Sequence[SearchHit], top_n: int) -> list[SearchHit]:
        self.calls.append((question, len(hits), top_n))
        wanted = set(_words(question))
        scored = [
            SearchHit(hit.chunk, float(len(wanted & set(_words(hit.chunk.text))))) for hit in hits
        ]
        return sorted(scored, key=lambda hit: hit.score, reverse=True)[:top_n]


class FakeChatModel(ChatModel):
    def __init__(self, reply: str = "The answer [1].", fail: bool = False) -> None:
        self.reply = reply
        self.fail = fail
        self.prompts: list[tuple[str, str]] = []

    def generate(self, system_prompt: str, user_prompt: str) -> ChatReply:
        self.prompts.append((system_prompt, user_prompt))
        if self.fail:
            raise UpstreamServiceError("model down")
        return ChatReply(text=self.reply, model_id="fake-model")


class FakeOcr(OcrEngine):
    def __init__(self, text: str = "Scanned invoice total 4,200 USD") -> None:
        self.text = text
        self.images: list[bytes] = []

    def read_image(self, png_bytes: bytes) -> str:
        self.images.append(png_bytes)
        return self.text
