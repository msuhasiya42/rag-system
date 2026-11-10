"""Pinecone serverless index as the vector store. Chunk text is kept in vector metadata."""

import logging
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from pinecone import Pinecone, ServerlessSpec
from pinecone.errors import PineconeError

from rag_system.domain import Chunk, SearchHit
from rag_system.ports import UpstreamServiceError, VectorStore

logger = logging.getLogger(__name__)

# About 1024 floats plus up to ~2 KB of text each: 100 vectors stays well under the request cap.
UPSERT_BATCH_SIZE = 100
DELETE_BATCH_SIZE = 1000
REQUEST_TIMEOUT_SECONDS = 30.0


class PineconeVectorStore(VectorStore):
    def __init__(self, api_key: str, index_name: str, namespace: str) -> None:
        self._client = Pinecone(api_key=api_key, timeout=REQUEST_TIMEOUT_SECONDS)
        self._index_name = index_name
        self._namespace = namespace
        self._index: Any = None  # resolved on first use; resolving needs a network call

    def ensure_index(self, dimension: int, cloud: str, region: str) -> bool:
        """Create the index if it does not exist. Returns True when it was created."""
        try:
            if self._client.has_index(self._index_name):
                return False
            logger.info("Creating Pinecone index %s (%s/%s)", self._index_name, cloud, region)
            self._client.create_index(
                name=self._index_name,
                dimension=dimension,
                metric="cosine",
                spec=ServerlessSpec(cloud=cloud, region=region),
                timeout=300,
            )
            return True
        except PineconeError as error:
            raise UpstreamServiceError(f"Pinecone index setup failed: {error}") from error

    def upsert(self, chunks: Sequence[Chunk], vectors: Sequence[Sequence[float]]) -> None:
        if len(chunks) != len(vectors):
            raise ValueError("chunks and vectors must have the same length")
        records = [
            {"id": chunk.id, "values": list(vector), "metadata": _metadata(chunk)}
            for chunk, vector in zip(chunks, vectors, strict=True)
        ]
        try:
            for start in range(0, len(records), UPSERT_BATCH_SIZE):
                self._get_index().upsert(
                    vectors=records[start : start + UPSERT_BATCH_SIZE],
                    namespace=self._namespace,
                )
        except PineconeError as error:
            raise UpstreamServiceError(f"Pinecone upsert failed: {error}") from error

    def delete_document(self, document_id: str) -> None:
        # Serverless indexes cannot delete by metadata filter, so delete by id prefix.
        try:
            ids = list(self._ids_with_prefix(f"{document_id}#"))
            for start in range(0, len(ids), DELETE_BATCH_SIZE):
                self._get_index().delete(
                    ids=ids[start : start + DELETE_BATCH_SIZE], namespace=self._namespace
                )
        except PineconeError as error:
            raise UpstreamServiceError(f"Pinecone delete failed: {error}") from error

    def search(self, vector: Sequence[float], top_k: int) -> list[SearchHit]:
        try:
            response = self._get_index().query(
                vector=list(vector),
                top_k=top_k,
                include_metadata=True,
                namespace=self._namespace,
            )
        except PineconeError as error:
            raise UpstreamServiceError(f"Pinecone query failed: {error}") from error
        return [
            SearchHit(chunk=_chunk_from(match.id, match.metadata or {}), score=match.score)
            for match in response.matches
        ]

    def _ids_with_prefix(self, prefix: str) -> Iterator[str]:
        for page in self._get_index().list(prefix=prefix, namespace=self._namespace):
            for item in page:
                yield item.id

    def _get_index(self) -> Any:
        if self._index is None:
            self._index = self._client.index(name=self._index_name)
        return self._index


def _metadata(chunk: Chunk) -> dict[str, str | int]:
    return {
        "document_id": chunk.document_id,
        "document_name": chunk.document_name,
        "chunk_index": chunk.chunk_index,
        "page_start": chunk.page_start,
        "page_end": chunk.page_end,
        "section": chunk.section,
        "text": chunk.text,
    }


def _chunk_from(vector_id: str, metadata: Mapping[str, Any]) -> Chunk:
    return Chunk(
        id=vector_id,
        document_id=str(metadata.get("document_id", "")),
        document_name=str(metadata.get("document_name", "")),
        chunk_index=int(metadata.get("chunk_index", 0)),
        text=str(metadata.get("text", "")),
        page_start=int(metadata.get("page_start", 0)),
        page_end=int(metadata.get("page_end", 0)),
        section=str(metadata.get("section", "")),
    )
