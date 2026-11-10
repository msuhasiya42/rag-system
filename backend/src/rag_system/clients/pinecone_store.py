"""Pinecone serverless indexes as vector stores. Chunk text is kept in vector metadata.

Two indexes hold the same chunks: a dense index (cosine, meaning) and a sparse index
(dot product, exact terms). Pinecone serverless can only mix both kinds in one index with the
dot-product metric, so separate indexes keep the dense one on cosine and let each be queried in
parallel and fused with reciprocal rank fusion.
"""

import logging
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from pinecone import Pinecone, ServerlessSpec
from pinecone.errors import PineconeError

from rag_system.domain import Chunk, SearchHit, SearchUnit, SparseVector
from rag_system.ports import UpstreamServiceError, VectorStore

logger = logging.getLogger(__name__)

# About 1024 floats plus up to ~2 KB of text each: 100 vectors stays well under the request cap.
UPSERT_BATCH_SIZE = 100
DELETE_BATCH_SIZE = 1000


class _PineconeIndex:
    """Index lifecycle, batching, deletion and result parsing shared by both stores."""

    def __init__(self, client: Pinecone, index_name: str, namespace: str) -> None:
        self._client = client
        self._index_name = index_name
        self._namespace = namespace
        self._index: Any = None  # resolved on first use; resolving needs a network call

    def ensure_index(self, cloud: str, region: str) -> bool:
        """Create the index if it does not exist. Returns True when it was created."""
        try:
            if self._client.has_index(self._index_name):
                return False
            logger.info("Creating Pinecone index %s (%s/%s)", self._index_name, cloud, region)
            self._client.create_index(
                name=self._index_name,
                spec=ServerlessSpec(cloud=cloud, region=region),
                timeout=300,
                **self._index_options(),
            )
            return True
        except PineconeError as error:
            raise UpstreamServiceError(f"Pinecone index setup failed: {error}") from error

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

    def _index_options(self) -> dict[str, Any]:
        raise NotImplementedError

    def _upsert_records(self, records: list[dict[str, Any]]) -> None:
        try:
            for start in range(0, len(records), UPSERT_BATCH_SIZE):
                self._get_index().upsert(
                    vectors=records[start : start + UPSERT_BATCH_SIZE],
                    namespace=self._namespace,
                    show_progress=False,
                )
        except PineconeError as error:
            raise UpstreamServiceError(f"Pinecone upsert failed: {error}") from error

    def _query(self, top_k: int, **vector: Any) -> list[SearchHit]:
        try:
            response = self._get_index().query(
                top_k=top_k, include_metadata=True, namespace=self._namespace, **vector
            )
        except PineconeError as error:
            raise UpstreamServiceError(f"Pinecone query failed: {error}") from error
        return [
            _hit_from(match.id, match.metadata or {}, match.score) for match in response.matches
        ]

    def _ids_with_prefix(self, prefix: str) -> Iterator[str]:
        for page in self._get_index().list(prefix=prefix, namespace=self._namespace):
            for item in page:
                yield item.id

    def _get_index(self) -> Any:
        if self._index is None:
            self._index = self._client.index(name=self._index_name)
        return self._index


class PineconeDenseStore(_PineconeIndex, VectorStore[Sequence[float]]):
    def __init__(self, client: Pinecone, index_name: str, namespace: str, dimension: int) -> None:
        super().__init__(client, index_name, namespace)
        self._dimension = dimension

    def upsert(self, units: Sequence[SearchUnit], vectors: Sequence[Sequence[float]]) -> None:
        self._upsert_records(
            [
                {"id": unit.id, "values": list(vector), "metadata": _metadata(unit)}
                for unit, vector in zip(units, vectors, strict=True)
            ]
        )

    def search(self, vector: Sequence[float], top_k: int) -> list[SearchHit]:
        return self._query(top_k, vector=list(vector))

    def _index_options(self) -> dict[str, Any]:
        return {"dimension": self._dimension, "metric": "cosine"}


class PineconeSparseStore(_PineconeIndex, VectorStore[SparseVector]):
    def upsert(self, units: Sequence[SearchUnit], vectors: Sequence[SparseVector]) -> None:
        self._upsert_records(
            [
                {"id": unit.id, "sparse_values": _sparse(vector), "metadata": _metadata(unit)}
                for unit, vector in zip(units, vectors, strict=True)
                if vector.indices  # Pinecone rejects empty sparse vectors
            ]
        )

    def search(self, vector: SparseVector, top_k: int) -> list[SearchHit]:
        if not vector.indices:
            return []
        return self._query(top_k, sparse_vector=_sparse(vector))

    def _index_options(self) -> dict[str, Any]:
        return {"metric": "dotproduct", "vector_type": "sparse"}


def _sparse(vector: SparseVector) -> dict[str, list[int] | list[float]]:
    return {"indices": vector.indices, "values": vector.values}


def _metadata(unit: SearchUnit) -> dict[str, str | int]:
    """The parent chunk's fields, so a matching row can return its whole chunk without a
    second lookup. Rows repeat the chunk text; at under 2 KB per record that is cheap."""
    chunk = unit.chunk
    return {
        "chunk_id": chunk.id,
        "matched": unit.matched,
        "document_id": chunk.document_id,
        "document_name": chunk.document_name,
        "chunk_index": chunk.chunk_index,
        "page_start": chunk.page_start,
        "page_end": chunk.page_end,
        "section": chunk.section,
        "text": chunk.text,
    }


def _hit_from(record_id: str, metadata: Mapping[str, Any], score: float) -> SearchHit:
    return SearchHit(_chunk_from(record_id, metadata), score, str(metadata.get("matched", "")))


def _chunk_from(record_id: str, metadata: Mapping[str, Any]) -> Chunk:
    return Chunk(
        # Records written before table-row units existed have no chunk_id: they are chunks.
        id=str(metadata.get("chunk_id") or record_id),
        document_id=str(metadata.get("document_id", "")),
        document_name=str(metadata.get("document_name", "")),
        chunk_index=int(metadata.get("chunk_index", 0)),
        text=str(metadata.get("text", "")),
        page_start=int(metadata.get("page_start", 0)),
        page_end=int(metadata.get("page_end", 0)),
        section=str(metadata.get("section", "")),
    )
