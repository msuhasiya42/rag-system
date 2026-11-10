"""Interfaces for the external services, so the core logic can be tested with fakes.

Implementations inherit these Protocols explicitly (``class PineconeVectorStore(VectorStore)``).
Structural typing alone would work, but explicit inheritance lets mypy check each class where it
is defined and lets IDEs navigate from an interface method to its implementations.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from rag_system.domain import Chunk, SearchHit


class UpstreamServiceError(Exception):
    """An external service (Bedrock, Textract, Pinecone) failed or is unavailable."""


class Embedder(Protocol):
    """Documents and questions are embedded separately: retrieval models such as
    llama-text-embed-v2 encode a stored passage and a search query differently, and mixing them
    up lowers search quality. Models without the distinction (Titan) treat both the same."""

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one vector per text, in the same order."""
        ...

    def embed_query(self, text: str) -> list[float]: ...


class VectorStore(Protocol):
    def upsert(self, chunks: Sequence[Chunk], vectors: Sequence[Sequence[float]]) -> None: ...

    def delete_document(self, document_id: str) -> None: ...

    def search(self, vector: Sequence[float], top_k: int) -> list[SearchHit]: ...


@dataclass(frozen=True)
class ChatReply:
    text: str
    model_id: str


class ChatModel(Protocol):
    def generate(self, system_prompt: str, user_prompt: str) -> ChatReply: ...


class OcrEngine(Protocol):
    def read_image(self, png_bytes: bytes) -> str:
        """Return the text found in a PNG image, one line per text line."""
        ...
