"""Interfaces for the external services, so the core logic can be tested with fakes.

Implementations inherit these Protocols explicitly (``class PineconeDenseStore(VectorStore[...])``).
Structural typing alone would work, but explicit inheritance lets mypy check each class where it
is defined and lets IDEs navigate from an interface method to its implementations.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, TypeVar

from rag_system.domain import Chunk, SearchHit, SparseVector

# The kind of vector a store holds: dense (list of floats) or sparse.
VectorT = TypeVar("VectorT", contravariant=True)


class UpstreamServiceError(Exception):
    """An external service (Bedrock, Textract, Pinecone) failed or is unavailable."""


class Embedder(Protocol):
    """Dense embeddings capture meaning ("broken headphones" ~ "damaged electronics").

    Documents and questions are embedded separately: retrieval models such as
    llama-text-embed-v2 encode a stored passage and a search query differently, and mixing them
    up lowers search quality. Models without the distinction (Titan) treat both the same."""

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one vector per text, in the same order."""
        ...

    def embed_query(self, text: str) -> list[float]: ...


class SparseEncoder(Protocol):
    """Sparse vectors weight the exact terms in a text ("1.125%", "Senior", "Notes", "2027"),
    playing the role BM25 plays in classic search: strong on codes, ids and figures."""

    def encode_documents(self, texts: Sequence[str]) -> list[SparseVector]: ...

    def encode_query(self, text: str) -> SparseVector: ...


class VectorStore(Protocol[VectorT]):
    def upsert(self, chunks: Sequence[Chunk], vectors: Sequence[VectorT]) -> None: ...

    def delete_document(self, document_id: str) -> None: ...

    def search(self, vector: VectorT, top_k: int) -> list[SearchHit]: ...


class Reranker(Protocol):
    """A cross-encoder reads the question and each candidate together and scores relevance.
    Slower than vector search, so it only reorders a short candidate list."""

    def rerank(self, question: str, hits: Sequence[SearchHit], top_n: int) -> list[SearchHit]:
        """Return the ``top_n`` most relevant hits, best first, scored by the reranker."""
        ...


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
