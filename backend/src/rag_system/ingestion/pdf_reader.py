"""Extract per-page text lines (with font style) and the bookmark outline from a PDF.

pypdfium2 reads the text layer. It is much faster than pdfplumber on large reports (about 2 s
against 40 s for a 154-page annual report, style included) and does not merge words or repeat
characters on tightly kerned or bold text. Pages that look scanned fall back to OCR.
"""

import ctypes
import io
import logging
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_raw

from rag_system.domain import OutlineEntry, PageText, TextLine, TextSource
from rag_system.ports import OcrEngine

logger = logging.getLogger(__name__)

# 2x of 72 dpi = 144 dpi: enough for OCR, small enough for Textract's synchronous size limit.
OCR_RENDER_SCALE = 2.0
OUTLINE_MAX_DEPTH = 4
BOLD_WEIGHT = 600
# A line is bold when this share of its visible characters is bold.
BOLD_LINE_SHARE = 0.8


@dataclass(frozen=True)
class PdfContent:
    pages: list[PageText]
    outline: list[OutlineEntry]


def read_pdf(path: Path, ocr: OcrEngine | None, min_text_chars: int) -> PdfContent:
    """Read every page, OCR'ing pages that look scanned.

    A page is treated as scanned when its text layer has fewer than ``min_text_chars``
    characters and it contains an image. Blank pages (no text, no image) are not sent to OCR.
    """
    document = pdfium.PdfDocument(path)
    try:
        pages = [
            _read_page(document[index], index + 1, ocr, min_text_chars)
            for index in range(len(document))
        ]
        return PdfContent(pages=pages, outline=_read_outline(document))
    finally:
        document.close()


def _read_page(
    page: pdfium.PdfPage, page_number: int, ocr: OcrEngine | None, min_text_chars: int
) -> PageText:
    lines = _text_lines(page.get_textpage())
    char_count = sum(len(line.text.strip()) for line in lines)
    if char_count >= min_text_chars:
        return PageText(page_number, lines, TextSource.TEXT_LAYER)

    if ocr is not None and _has_image(page):
        logger.info("OCR page %d (text layer has %d chars)", page_number, char_count)
        ocr_text = ocr.read_image(_render_png(page))
        ocr_lines = [TextLine(text) for text in ocr_text.splitlines() if text.strip()]
        if ocr_lines:
            return PageText(page_number, ocr_lines, TextSource.OCR)

    return PageText(page_number, lines, TextSource.TEXT_LAYER if lines else TextSource.EMPTY)


@dataclass
class _LineBuilder:
    chars: list[str] = field(default_factory=list)
    sizes: Counter[float] = field(default_factory=Counter)
    bold_chars: int = 0
    visible_chars: int = 0
    x0: float | None = None
    top: float = 0.0

    def add(self, char: str, size: float, bold: bool, left: float, top: float) -> None:
        self.chars.append(char)
        if char.isspace():
            return
        self.visible_chars += 1
        self.sizes[size] += 1
        self.bold_chars += bold
        self.x0 = left if self.x0 is None else self.x0
        self.top = max(self.top, top)

    def build(self) -> TextLine | None:
        text = "".join(self.chars).strip()
        if not text or not self.visible_chars:
            return None
        return TextLine(
            text=text,
            size=self.sizes.most_common(1)[0][0],
            bold=self.bold_chars / self.visible_chars >= BOLD_LINE_SHARE,
            x0=self.x0 or 0.0,
            top=self.top,
        )


def _text_lines(textpage: pdfium.PdfTextPage) -> list[TextLine]:
    """Rebuild lines from characters, keeping size, weight and position.

    Font size comes from the glyph box height rather than FPDFText_GetFontSize: many PDFs
    (including the Berkshire report) scale text through the transformation matrix, so the
    reported font size is 1.0 everywhere.
    """
    lines: list[TextLine] = []
    builder = _LineBuilder()
    font_name = ctypes.create_string_buffer(128)
    flags = ctypes.c_int()
    for index in range(textpage.count_chars()):
        char = chr(pdfium_raw.FPDFText_GetUnicode(textpage, index))
        if char in "\r\n":
            if line := builder.build():
                lines.append(line)
            builder = _LineBuilder()
            continue
        if ord(char) < 32 or char == "￾":
            char = " "  # control characters, e.g. \x11 used as leader dots
        left, bottom, _right, top = textpage.get_charbox(index, loose=True)
        pdfium_raw.FPDFText_GetFontInfo(textpage, index, font_name, 128, ctypes.byref(flags))
        bold = (
            pdfium_raw.FPDFText_GetFontWeight(textpage, index) >= BOLD_WEIGHT
            or b"bold" in font_name.value.lower()
        )
        builder.add(char, round(top - bottom, 1), bold, left, top)
    if line := builder.build():
        lines.append(line)
    return lines


def _read_outline(document: pdfium.PdfDocument) -> list[OutlineEntry]:
    entries: list[OutlineEntry] = []
    for bookmark in document.get_toc(max_depth=OUTLINE_MAX_DEPTH):
        destination = bookmark.get_dest()
        page_index = destination.get_index() if destination is not None else None
        title = (bookmark.get_title() or "").strip()
        if page_index is not None and title:
            entries.append(OutlineEntry(bookmark.level, title, page_index + 1))
    return entries


def _has_image(page: pdfium.PdfPage) -> bool:
    images = page.get_objects(filter=[pdfium_raw.FPDF_PAGEOBJ_IMAGE], max_depth=3)
    return next(iter(images), None) is not None


def _render_png(page: pdfium.PdfPage) -> bytes:
    image = page.render(scale=OCR_RENDER_SCALE, grayscale=True).to_pil()
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()
