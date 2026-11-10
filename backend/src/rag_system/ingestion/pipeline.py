"""PDF -> pages -> cleaned text -> chunks -> embeddings -> vector store."""

import logging
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from rag_system.domain import TextSource
from rag_system.ingestion.chunking import chunk_sections, document_id_for, embedding_text
from rag_system.ingestion.cleaning import clean_pages
from rag_system.ingestion.pdf_reader import read_pdf
from rag_system.ingestion.structure import build_sections
from rag_system.ports import Embedder, OcrEngine, VectorStore

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


class IngestionService:
    def __init__(
        self,
        embedder: Embedder,
        store: VectorStore,
        ocr: OcrEngine | None,
        *,
        ocr_min_text_chars: int,
        chunk_size_chars: int,
        chunk_overlap_chars: int,
    ) -> None:
        self._embedder = embedder
        self._store = store
        self._ocr = ocr
        self._ocr_min_text_chars = ocr_min_text_chars
        self._chunk_size_chars = chunk_size_chars
        self._chunk_overlap_chars = chunk_overlap_chars

    def ingest_pdf(self, path: Path) -> IngestReport:
        document_name = path.name
        document_id = document_id_for(document_name)

        content = read_pdf(path, self._ocr, self._ocr_min_text_chars)
        pages = clean_pages(content.pages)
        sections = build_sections(pages, content.outline)
        chunks = chunk_sections(
            document_id, document_name, sections, self._chunk_size_chars, self._chunk_overlap_chars
        )
        logger.info(
            "%s: %d pages, %d sections -> %d chunks",
            document_name,
            len(pages),
            len(sections),
            len(chunks),
        )

        # Embed before deleting, so a failed embedding run leaves the old version searchable.
        vectors = self._embedder.embed_documents([embedding_text(chunk) for chunk in chunks])
        self._store.delete_document(document_id)
        self._store.upsert(chunks, vectors)

        sources = Counter(page.source for page in pages)
        return IngestReport(
            document_id=document_id,
            document_name=document_name,
            page_count=len(pages),
            ocr_pages=sources[TextSource.OCR],
            empty_pages=sources[TextSource.EMPTY],
            section_count=len(sections),
            chunk_count=len(chunks),
        )
