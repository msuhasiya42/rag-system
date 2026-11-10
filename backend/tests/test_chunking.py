import pytest

from rag_system.domain import TextLine
from rag_system.ingestion.chunking import chunk_sections, document_id_for
from rag_system.ingestion.structure import Block, BlockKind, PlacedLine, Section


def _lines(count: int, prefix: str = "line", page: int = 1, width: int = 40) -> list[PlacedLine]:
    return [PlacedLine(page, TextLine(f"{prefix} {i} " + "x" * width)) for i in range(count)]


def _paragraph(*lines: PlacedLine) -> Block:
    return Block(BlockKind.PARAGRAPH, list(lines))


def _table(*texts: str) -> Block:
    return Block(BlockKind.TABLE, [PlacedLine(1, TextLine(text)) for text in texts])


def _chunk(sections: list[Section], size: int = 300, overlap: int = 60) -> list[str]:
    return [chunk.text for chunk in chunk_sections("doc", "doc.pdf", sections, size, overlap)]


def test_a_small_section_is_one_chunk_tagged_with_its_path() -> None:
    sections = [Section(("Form 10-K", "Item 1A. Risk Factors"), [_paragraph(*_lines(3))])]

    [chunk] = chunk_sections("doc", "doc.pdf", sections, 300, 60)

    assert chunk.section == "Form 10-K > Item 1A. Risk Factors"
    assert chunk.contextual_text().startswith("Form 10-K > Item 1A. Risk Factors\nline 0")


def test_chunks_never_cross_a_section_boundary() -> None:
    sections = [
        Section(("A",), [_paragraph(*_lines(2, "alpha"))]),
        Section(("B",), [_paragraph(*_lines(2, "beta"))]),
    ]

    chunks = chunk_sections("doc", "doc.pdf", sections, 1000, 100)

    assert [(chunk.section, chunk.text.split()[0]) for chunk in chunks] == [
        ("A", "alpha"),
        ("B", "beta"),
    ]


def test_a_large_section_is_split_with_overlap_inside_the_section() -> None:
    paragraphs = [_paragraph(*_lines(2, f"p{n}")) for n in range(6)]

    texts = _chunk([Section(("Big",), paragraphs)], size=300, overlap=120)

    assert len(texts) > 1
    assert all(len(text) <= 300 for text in texts)
    for previous, current in zip(texts, texts[1:], strict=False):
        assert current.split("\n")[0] in previous.split("\n")  # starts with the previous tail


def test_a_table_that_fits_is_kept_whole_in_the_next_chunk() -> None:
    table = _table("2025 2024", "Revenue 100 90", "Costs 60 50", "Profit 40 40")
    section = Section(("S",), [_paragraph(*_lines(7, width=30)), table])  # 266 + 54 > 300

    texts = _chunk([section], size=300, overlap=0)

    assert texts[-1] == "2025 2024\nRevenue 100 90\nCosts 60 50\nProfit 40 40"


def test_table_rows_are_not_repeated_as_overlap() -> None:
    section = Section(
        ("S",), [_table("Revenue 100 90", "Costs 60 50"), _paragraph(*_lines(6, width=40))]
    )

    texts = _chunk([section], size=200, overlap=150)

    assert sum("Revenue 100 90" in text for text in texts) == 1


def test_a_large_table_is_split_between_rows_and_repeats_its_column_headings() -> None:
    rows = [f"Business unit number {i} {i * 10},000 {i * 9},000" for i in range(20)]
    table = _table("(dollars in millions)", "2025 2024", *rows)

    texts = _chunk([Section(("S",), [table])], size=300, overlap=0)

    assert len(texts) > 1
    assert all(text.startswith("(dollars in millions)\n2025 2024\n") for text in texts)
    assert all(len(text) <= 300 for text in texts)
    body_rows = [line for text in texts for line in text.split("\n")[2:]]
    assert body_rows == rows


def test_a_paragraph_longer_than_a_chunk_is_split_between_lines() -> None:
    texts = _chunk([Section(("S",), [_paragraph(*_lines(20))])], size=300, overlap=0)

    assert len(texts) > 1
    assert all(len(text) <= 300 for text in texts)


def test_a_very_long_line_is_split_at_word_boundaries() -> None:
    long_line = " ".join(f"word{i}" for i in range(200))
    section = Section((), [_paragraph(PlacedLine(1, TextLine(long_line)))])

    texts = _chunk([section], size=200, overlap=0)

    assert all(len(text) <= 200 for text in texts)
    assert " ".join(texts) == long_line


def test_chunks_record_the_pages_they_span() -> None:
    section = Section(("S",), [_paragraph(*_lines(2, page=3), *_lines(2, page=4))])

    [chunk] = chunk_sections("doc", "doc.pdf", [section], 1000, 0)

    assert (chunk.page_start, chunk.page_end) == (3, 4)


def test_chunks_without_words_are_skipped() -> None:
    sections = [
        Section((), [_paragraph(PlacedLine(1, TextLine("2025")))]),
        Section(
            ("Item 1B. Unresolved Staff Comments",), [_paragraph(PlacedLine(2, TextLine("None.")))]
        ),
    ]

    chunks = chunk_sections("doc", "doc.pdf", sections, 300, 0)

    assert [chunk.text for chunk in chunks] == ["None."]
    assert chunks[0].id == "doc#00000"


def test_overlap_must_be_smaller_than_chunk_size() -> None:
    with pytest.raises(ValueError):
        chunk_sections("doc", "doc.pdf", [], size_chars=100, overlap_chars=100)


def test_document_id_is_a_slug_of_the_file_name() -> None:
    assert document_id_for("Berkshire 2025 Annual_Report.PDF") == "berkshire-2025-annual-report"
