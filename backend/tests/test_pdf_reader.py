from pathlib import Path

from rag_system.domain import OutlineEntry, TextSource
from rag_system.ingestion.pdf_reader import read_pdf
from tests.fakes import FakeOcr


def test_text_pages_use_the_text_layer_and_scanned_pages_use_ocr(mixed_pdf: Path) -> None:
    pages = read_pdf(mixed_pdf, FakeOcr(), min_text_chars=50).pages

    assert [page.page_number for page in pages] == [1, 2, 3]
    assert pages[0].source == TextSource.TEXT_LAYER
    assert "Annual revenue grew" in pages[0].text
    assert pages[1].source == TextSource.OCR
    assert pages[1].text == "Scanned invoice total 4,200 USD"
    assert pages[2].source == TextSource.EMPTY


def test_lines_keep_font_style_and_position(mixed_pdf: Path) -> None:
    lines = read_pdf(mixed_pdf, None, min_text_chars=50).pages[0].lines

    title, first_body = lines[0], lines[1]
    assert (title.text, title.bold) == ("Revenue Overview", True)
    assert not first_body.bold
    assert 6 < first_body.size < 14  # 12 pt text, measured from glyph boxes
    assert title.top > first_body.top  # PDF coordinates grow upwards


def test_the_bookmark_outline_is_read(mixed_pdf: Path) -> None:
    outline = read_pdf(mixed_pdf, None, min_text_chars=50).outline

    assert outline == [OutlineEntry(level=0, title="Revenue Overview", page_number=1)]


def test_only_the_image_page_is_sent_to_ocr(mixed_pdf: Path) -> None:
    ocr = FakeOcr()

    read_pdf(mixed_pdf, ocr, min_text_chars=50)

    assert len(ocr.images) == 1  # the blank page has no image, so it is not OCR'd
    assert ocr.images[0].startswith(b"\x89PNG")


def test_without_ocr_scanned_pages_are_empty(mixed_pdf: Path) -> None:
    pages = read_pdf(mixed_pdf, None, min_text_chars=50).pages

    assert pages[1].source == TextSource.EMPTY
    assert pages[1].text.strip() == ""
