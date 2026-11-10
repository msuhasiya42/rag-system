from pathlib import Path

from rag_system.ingestion.pipeline import IngestionService, SparseIndexing
from tests.fakes import (
    FakeEmbedder,
    FakeOcr,
    FakeSparseEncoder,
    InMemorySparseStore,
    InMemoryVectorStore,
)


def _service(store: InMemoryVectorStore) -> IngestionService:
    return IngestionService(
        FakeEmbedder(),
        store,
        FakeOcr(),
        ocr_min_text_chars=50,
        chunk_size_chars=400,
        chunk_overlap_chars=50,
    )


def test_ingest_indexes_text_and_ocr_pages(mixed_pdf: Path) -> None:
    store = InMemoryVectorStore()

    report = _service(store).ingest_pdf(mixed_pdf)

    assert report.document_id == "mixed-report"
    assert (report.page_count, report.ocr_pages, report.empty_pages) == (3, 1, 1)
    assert report.chunk_count == len(store.records)
    chunks = [chunk for chunk, _ in store.records.values()]
    revenue = next(chunk for chunk in chunks if "Annual revenue grew" in chunk.text)
    assert revenue.section == "Revenue Overview"  # from the bookmark and bold heading
    assert any("Scanned invoice total" in chunk.text for chunk in chunks)


def test_the_sparse_index_gets_the_same_chunks(mixed_pdf: Path) -> None:
    dense, sparse = InMemoryVectorStore(), InMemorySparseStore()
    service = IngestionService(
        FakeEmbedder(),
        dense,
        None,
        sparse=SparseIndexing(FakeSparseEncoder(), sparse),
        ocr_min_text_chars=50,
        chunk_size_chars=400,
        chunk_overlap_chars=50,
    )

    service.ingest_pdf(mixed_pdf)

    assert set(sparse.records) == set(dense.records)
    assert sparse.operations == ["delete:mixed-report", "upsert"]


def test_the_section_path_is_embedded_with_the_chunk_text(mixed_pdf: Path) -> None:
    embedder = FakeEmbedder()
    service = IngestionService(
        embedder,
        InMemoryVectorStore(),
        None,
        ocr_min_text_chars=50,
        chunk_size_chars=400,
        chunk_overlap_chars=50,
    )

    service.ingest_pdf(mixed_pdf)

    assert embedder.calls[0][0].startswith("Revenue Overview\nAnnual revenue grew")


def test_reingesting_a_document_replaces_its_old_chunks(mixed_pdf: Path) -> None:
    store = InMemoryVectorStore()
    service = _service(store)
    service.ingest_pdf(mixed_pdf)
    stale = "mixed-report#09999"
    store.records[stale] = next(iter(store.records.values()))

    service.ingest_pdf(mixed_pdf)

    assert stale not in store.records
    assert store.operations[-2:] == ["delete:mixed-report", "upsert"]
