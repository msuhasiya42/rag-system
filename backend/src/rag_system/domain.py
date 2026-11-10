"""Core data types shared by ingestion, retrieval and answering."""

from dataclasses import dataclass
from enum import StrEnum


class TextSource(StrEnum):
    """How a page's text was obtained."""

    TEXT_LAYER = "text_layer"
    OCR = "ocr"
    EMPTY = "empty"


@dataclass(frozen=True)
class TextLine:
    """One line of text with the layout facts used to find headings, paragraphs and tables.

    Style fields are 0/False when unknown (OCR text has no font information).
    """

    text: str
    size: float = 0.0  # glyph box height in points
    bold: bool = False
    x0: float = 0.0  # left edge
    top: float = 0.0  # top edge; PDF coordinates grow upwards


@dataclass(frozen=True)
class PageText:
    page_number: int  # 1-based, as people cite pages
    lines: list[TextLine]
    source: TextSource

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)


@dataclass(frozen=True)
class OutlineEntry:
    """A PDF bookmark: level 0 is top level."""

    level: int
    title: str
    page_number: int


@dataclass(frozen=True)
class Chunk:
    id: str
    document_id: str
    document_name: str
    chunk_index: int
    text: str
    page_start: int
    page_end: int
    section: str = ""  # heading path, e.g. "Form 10-K > Item 1A. Risk Factors"


@dataclass(frozen=True)
class SearchHit:
    chunk: Chunk
    score: float


@dataclass(frozen=True)
class Answer:
    text: str
    model_id: str | None  # None when no model was called (nothing retrieved)
    sources: list[SearchHit]
