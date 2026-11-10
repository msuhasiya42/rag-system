"""In-memory stand-ins for Bedrock, Pinecone and Textract. No network in tests."""

import hashlib
import math
import re
from collections.abc import Sequence

from rag_system.domain import Chunk, SearchHit
from rag_system.ports import (
    ChatModel,
    ChatReply,
    Embedder,
    OcrEngine,
    UpstreamServiceError,
    VectorStore,
)


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
        for word in re.findall(r"\w+", text.lower()):
            bucket = int(hashlib.md5(word.encode()).hexdigest(), 16) % self.DIMENSIONS
            vector[bucket] += 1.0
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]


class InMemoryVectorStore(VectorStore):
    def __init__(self) -> None:
        self.records: dict[str, tuple[Chunk, list[float]]] = {}
        self.operations: list[str] = []

    def upsert(self, chunks: Sequence[Chunk], vectors: Sequence[Sequence[float]]) -> None:
        self.operations.append("upsert")
        for chunk, vector in zip(chunks, vectors, strict=True):
            self.records[chunk.id] = (chunk, list(vector))

    def delete_document(self, document_id: str) -> None:
        self.operations.append(f"delete:{document_id}")
        for key in [key for key in self.records if key.startswith(f"{document_id}#")]:
            del self.records[key]

    def search(self, vector: Sequence[float], top_k: int) -> list[SearchHit]:
        scored = [
            SearchHit(chunk, sum(a * b for a, b in zip(vector, stored, strict=True)))
            for chunk, stored in self.records.values()
        ]
        return sorted(scored, key=lambda hit: hit.score, reverse=True)[:top_k]


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
