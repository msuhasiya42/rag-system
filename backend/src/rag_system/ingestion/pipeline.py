"""PDF -> pages -> cleaned text -> chunks -> embeddings -> vector store."""

import logging
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from rag_system.domain import SparseVector, TextSource
from rag_system.ingestion.chunking import document_id_for, search_units
from rag_system.ingestion.cleaning import clean_pages
from rag_system.ingestion.pdf_reader import read_pdf
from rag_system.ingestion.structure import build_sections
from rag_system.ports import Embedder, OcrEngine, SparseEncoder, VectorStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IngestReport:
    document_id: str
    document_name: str
    page_count: int
    ocr_pages: int
    empty_pages: int
    section_count: int
    chunk_count: int
    table_row_count: int


@dataclass(frozen=True)
class SparseIndexing:
    """The optional keyword-style index written next to the dense one, for hybrid search."""

    encoder: SparseEncoder
    store: VectorStore[SparseVector]


class IngestionService:
    def __init__(
        self,
        embedder: Embedder,
        store: VectorStore[Sequence[float]],
        ocr: OcrEngine | None,
        *,
        sparse: SparseIndexing | None = None,
        ocr_min_text_chars: int,
        chunk_size_chars: int,
        chunk_overlap_chars: int,
        index_table_rows: bool = False,
    ) -> None:
        self._embedder = embedder
        self._store = store
        self._sparse = sparse
        self._ocr = ocr
        self._ocr_min_text_chars = ocr_min_text_chars
        self._chunk_size_chars = chunk_size_chars
        self._chunk_overlap_chars = chunk_overlap_chars
        self._index_table_rows = index_table_rows

    def ingest_pdf(self, path: Path) -> IngestReport:
        document_name = path.name
        document_id = document_id_for(document_name)

        content = read_pdf(path, self._ocr, self._ocr_min_text_chars)
        pages = clean_pages(content.pages)
        sections = build_sections(pages, content.outline)
        units = search_units(
            document_id,
            document_name,
            sections,
            self._chunk_size_chars,
            self._chunk_overlap_chars,
            table_rows=self._index_table_rows,
        )
        chunk_count = sum(1 for unit in units if not unit.matched)
        logger.info(
            "%s: %d pages, %d sections -> %d chunks",
            document_name,
            len(pages),
            len(sections),
            chunk_count,
        )

        # Encode everything before deleting, so a failed run leaves the old version searchable.
        texts = [unit.text for unit in units]
        vectors = self._embedder.embed_documents(texts)
        sparse_vectors = self._sparse.encoder.encode_documents(texts) if self._sparse else []

        self._store.delete_document(document_id)
        self._store.upsert(units, vectors)
        if self._sparse:
            self._sparse.store.delete_document(document_id)
            self._sparse.store.upsert(units, sparse_vectors)

        sources = Counter(page.source for page in pages)
        return IngestReport(
            document_id=document_id,
            document_name=document_name,
            page_count=len(pages),
            ocr_pages=sources[TextSource.OCR],
            empty_pages=sources[TextSource.EMPTY],
            section_count=len(sections),
            chunk_count=chunk_count,
            table_row_count=len(units) - chunk_count,
        )
