"""Find a document's sections, and the paragraphs and tables inside each section.

Sections come from two sources:
1. The PDF outline (bookmarks). Each entry is anchored to the matching heading line on its page,
   because bookmarks only point at a page and a new section often starts mid-page.
2. Heading lines: bold text at body size or larger text, left-aligned (or centred capitals).
   Numbering sets the level: "Item 1A." / "Chapter 3" are level 2, "(12)" / "3.1" level 3,
   other headings level 4. Outline entries take levels 1, 2, 3... by depth.

A heading repeated at the top of a continuation page ("(24) Pension plans" again) does not
start a new section, and running headers (the same styled line at the top of several pages)
are dropped. Without font information (OCR) or an outline, the whole document is one section
and chunking falls back to size-based splitting.
"""

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from enum import StrEnum

from rag_system.domain import OutlineEntry, PageText, TextLine

HEADING_MAX_CHARS = 140
# Bold text at (nearly) body size, or any text this much larger than body text, is a heading.
BOLD_HEADING_MIN_SIZE_RATIO = 0.94
LARGE_HEADING_SIZE_RATIO = 1.15
# How far right of the page's left margin a heading may start (indented sub-headings).
HEADING_MAX_INDENT = 30.0
PAGE_TOP_LINES = 3
RUNNING_HEADER_MIN_PAGES = 3
PARAGRAPH_INDENT = 8.0
# A vertical gap larger than this many line heights starts a new paragraph.
PARAGRAPH_GAP_LINE_HEIGHTS = 1.9
TABLE_MAX_GAP_LINES = 2  # label lines such as "Assets:" between table rows
TABLE_GAP_LINE_MAX_CHARS = 80
TABLE_HEADER_MAX_LINES = 8  # column headings above the first row
SMALL_TEXT_RATIO = 0.92
# A bold line this close below another bold line may be its wrapped continuation.
WRAPPED_HEADING_MAX_GAP = 1.5
CONTENTS_MIN_REFERENCES = 5

_LEVEL_2 = re.compile(r"^(item\s+\d+[a-z]?\.|chapter\s+\w+|part\s+[ivx\d]+\b)", re.IGNORECASE)
_LEVEL_3 = re.compile(r"^(\(\d+\)|\d+(\.\d+)*\.?\s+[A-Za-z])")
_WORD = re.compile(r"[A-Za-z]{3}")
_NUMBER = re.compile(r"^[(\-–—$]*\d[\d,.]*%?\)?%?$")
_DASH = frozenset({"—", "–", "-"})
_UNITS = re.compile(r"\b(millions|thousands|billions|percent)\b|%", re.IGNORECASE)
_BULLET = re.compile(r"^[•◦▪●\-–]\s")
_PROSE = re.compile(r"[a-z]{2,} [a-z]{2,} [a-z]{2,} [a-z]{2,}")  # four lowercase words in a row
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
# A contents entry: a title ending in a page number ("Risk Factors 24"), or a prefixed page
# number alone ("K-24"). 1-3 digits, so prose ending in a year does not count, and lone numbers
# (chart values) do not count.
_PAGE_REFERENCE = re.compile(r"([A-Za-z].*\s([A-Z]{1,2}-)?\d{1,3}$)|(^[A-Z]{1,2}-\d{1,3}$)")
_ENDS_WITH_CONNECTOR = re.compile(
    r"\b(of|and|or|the|to|in|for|with|on|by|from|at|as|a|an|our|its|their)$", re.IGNORECASE
)


class BlockKind(StrEnum):
    PARAGRAPH = "paragraph"
    TABLE = "table"


@dataclass(frozen=True)
class PlacedLine:
    page_number: int
    line: TextLine

    @property
    def text(self) -> str:
        return self.line.text


@dataclass
class Block:
    kind: BlockKind
    lines: list[PlacedLine]


@dataclass
class Section:
    path: tuple[str, ...]
    blocks: list[Block] = field(default_factory=list)


@dataclass(frozen=True)
class _Layout:
    body_size: float | None  # None when the document has no font information
    left_margins: dict[int, float]


def build_sections(pages: list[PageText], outline: list[OutlineEntry]) -> list[Section]:
    pages = [replace(page, lines=_merge_wrapped_headings(page.lines)) for page in pages]
    layout = _Layout(_body_size(pages), {p.page_number: _left_margin(p.lines) for p in pages})
    levels = {
        (page.page_number, index): level
        for page in pages
        if not _is_contents_page(page)
        for index, line in enumerate(page.lines)
        if (level := _heading_level(line, layout, page.page_number)) is not None
    }
    running = _running_headers(pages, layout)
    anchors, page_starts = _anchor_outline(outline, pages, levels)

    sections: list[Section] = []
    path: list[tuple[int, str]] = []
    content: list[PlacedLine] = []

    def open_section(level: int, title: str) -> None:
        nonlocal path, content
        _close(sections, path, content, layout)
        path = [item for item in path if item[0] < level] + [(level, title)]
        content = []

    for page in pages:
        for level, title in page_starts.get(page.page_number, []):
            open_section(level, title)
        for index, line in enumerate(page.lines):
            key = (page.page_number, index)
            if key in anchors:
                open_section(*anchors[key])
            elif key in running:
                continue
            elif key in levels:
                if not _continues_path(line.text, path):
                    open_section(levels[key], line.text)
            else:
                content.append(PlacedLine(page.page_number, line))
    _close(sections, path, content, layout)
    return sections


def is_table_row(text: str) -> bool:
    """A line ending in two or more numeric cells: "Goodwill 55,945 56,860", "2025 2024"."""
    cells = 0
    has_digit = False
    for token in reversed(text.split()):
        if token == "$":
            continue
        if _NUMBER.match(token):
            cells += 1
            has_digit = True
        elif token in _DASH:
            cells += 1
        else:
            break
    return cells >= 2 and has_digit


def _close(
    sections: list[Section], path: list[tuple[int, str]], content: list[PlacedLine], layout: _Layout
) -> None:
    if content:
        sections.append(Section(tuple(title for _, title in path), _blocks(content, layout)))


def _heading_level(line: TextLine, layout: _Layout, page_number: int) -> int | None:
    text = line.text
    if layout.body_size is None or not line.size:
        return None
    if len(text) > HEADING_MAX_CHARS or not _WORD.search(text) or text.endswith(":"):
        return None
    if is_table_row(text):
        return None
    styled = (line.bold and line.size >= layout.body_size * BOLD_HEADING_MIN_SIZE_RATIO) or (
        line.size >= layout.body_size * LARGE_HEADING_SIZE_RATIO
    )
    aligned = line.x0 <= layout.left_margins[page_number] + HEADING_MAX_INDENT or text.isupper()
    if not (styled and aligned):
        return None
    if _LEVEL_2.match(text):
        return 2
    if _LEVEL_3.match(text):
        return 3
    return 4


def _merge_wrapped_headings(lines: list[TextLine]) -> list[TextLine]:
    """Join a bold heading that wraps onto a second line ("...impair our ability to" +
    "access capital markets"). Two separate headings stay separate."""
    merged: list[TextLine] = []
    for line in lines:
        previous = merged[-1] if merged else None
        if previous is not None and _wraps_onto(previous, line):
            merged[-1] = replace(previous, text=f"{previous.text} {line.text}")
        else:
            merged.append(line)
    return merged


def _wraps_onto(first: TextLine, second: TextLine) -> bool:
    if not (first.bold and second.bold and first.size and second.size):
        return False
    adjacent = 0 < first.top - second.top <= first.size * WRAPPED_HEADING_MAX_GAP
    aligned = abs(first.x0 - second.x0) <= 2
    continues = second.text[:1].islower() or _ENDS_WITH_CONNECTOR.search(first.text) is not None
    return adjacent and aligned and continues


def _is_contents_page(page: PageText) -> bool:
    """A table of contents: many lines are, or end with, page references ("Risk Factors K-24")."""
    references = sum(
        bool(_PAGE_REFERENCE.search(line.text)) and not is_table_row(line.text)
        for line in page.lines
    )
    return references >= CONTENTS_MIN_REFERENCES and references >= len(page.lines) * 0.3


def _running_headers(pages: list[PageText], layout: _Layout) -> set[tuple[int, int]]:
    """Styled lines repeated at the top of several pages, e.g. "Notes to Financial Statements"."""
    positions: defaultdict[str, list[tuple[int, int]]] = defaultdict(list)
    for page in pages:
        margin = layout.left_margins[page.page_number]
        for index, line in enumerate(page.lines[:PAGE_TOP_LINES]):
            centered = line.x0 > margin + HEADING_MAX_INDENT
            if (line.bold or centered) and _WORD.search(line.text):
                positions[_normalize(line.text)].append((page.page_number, index))
    return {
        position
        for found in positions.values()
        if len({page for page, _ in found}) >= RUNNING_HEADER_MIN_PAGES
        for position in found
    }


def _anchor_outline(
    outline: list[OutlineEntry], pages: list[PageText], levels: dict[tuple[int, int], int]
) -> tuple[dict[tuple[int, int], tuple[int, str]], dict[int, list[tuple[int, str]]]]:
    """Place each bookmark on its heading line, or at the top of its page if none matches."""
    by_number = {page.page_number: page for page in pages}
    anchors: dict[tuple[int, int], tuple[int, str]] = {}
    page_starts: defaultdict[int, list[tuple[int, str]]] = defaultdict(list)
    for entry in outline:
        page = by_number.get(entry.page_number)
        level = entry.level + 1
        wanted = _normalize(entry.title)
        match = None
        if page is not None and len(wanted) >= 3:
            match = next(
                (
                    index
                    for index, line in enumerate(page.lines)
                    if (page.page_number, index) in levels
                    and (page.page_number, index) not in anchors
                    and wanted in _normalize(line.text)
                ),
                None,
            )
        if page is not None and match is not None:
            anchors[(page.page_number, match)] = (level, page.lines[match].text)
        else:
            page_starts[entry.page_number].append((level, entry.title))
    return anchors, page_starts


def _continues_path(text: str, path: list[tuple[int, str]]) -> bool:
    """A heading repeated on a continuation page, e.g. "Management's Discussion and Analysis"
    under "Item 7. Management's Discussion and Analysis of Financial Condition"."""
    normalized = _normalize(text)
    titles = [_normalize(title) for _, title in path]
    return any(
        normalized == title or (len(normalized) >= 10 and normalized in title) for title in titles
    )


def _blocks(lines: list[PlacedLine], layout: _Layout) -> list[Block]:
    blocks: list[Block] = []
    paragraph: list[PlacedLine] = []

    def flush() -> None:
        nonlocal paragraph
        if paragraph:
            blocks.append(Block(BlockKind.PARAGRAPH, paragraph))
            paragraph = []

    index = 0
    while index < len(lines):
        line = lines[index]
        if is_table_row(line.text):
            header = _take_table_header(paragraph, layout)
            flush()
            table, index = _collect_table(lines, index)
            blocks.append(Block(BlockKind.TABLE, header + table))
            continue
        if paragraph and _starts_paragraph(paragraph[-1], line, layout):
            flush()
        paragraph.append(line)
        index += 1
    flush()
    return blocks


def _collect_table(lines: list[PlacedLine], start: int) -> tuple[list[PlacedLine], int]:
    """Rows from ``start``, allowing short label lines between rows. Returns the next index."""
    table = [lines[start]]
    pending: list[PlacedLine] = []
    index = start + 1
    while index < len(lines):
        line = lines[index]
        if is_table_row(line.text):
            table += pending + [line]
            pending = []
        elif len(pending) < TABLE_MAX_GAP_LINES and len(line.text) <= TABLE_GAP_LINE_MAX_CHARS:
            pending.append(line)
        else:
            break
        index += 1
    return table, index - len(pending)  # trailing label lines belong to what follows


def _take_table_header(paragraph: list[PlacedLine], layout: _Layout) -> list[PlacedLine]:
    """Pull column-heading lines ("December 31,", "(in millions)") off the paragraph's end."""
    header: list[PlacedLine] = []
    while (
        paragraph
        and len(header) < TABLE_HEADER_MAX_LINES
        and _looks_like_header(paragraph[-1].line, layout)
    ):
        header.insert(0, paragraph.pop())
    return header


def _looks_like_header(line: TextLine, layout: _Layout) -> bool:
    """Bold or small text above a table, or a units line such as "(dollars in millions)"."""
    if len(line.text) > 120:
        return False
    small = bool(layout.body_size and line.size and line.size < layout.body_size * SMALL_TEXT_RATIO)
    units_line = _UNITS.search(line.text) is not None and not _PROSE.search(line.text)
    return line.bold or small or units_line


def _starts_paragraph(previous: PlacedLine, line: PlacedLine, layout: _Layout) -> bool:
    if _BULLET.match(line.text):
        return True
    if not line.line.size:  # no layout information (OCR)
        return False
    if line.line.x0 >= layout.left_margins[line.page_number] + PARAGRAPH_INDENT:
        return True
    same_page = previous.page_number == line.page_number
    gap = previous.line.top - line.line.top
    return same_page and gap > line.line.size * PARAGRAPH_GAP_LINE_HEIGHTS


def _body_size(pages: list[PageText]) -> float | None:
    sizes: Counter[float] = Counter()
    for page in pages:
        for line in page.lines:
            if line.size:
                sizes[line.size] += len(line.text)
    return sizes.most_common(1)[0][0] if sizes else None


def _left_margin(lines: list[TextLine]) -> float:
    """The smallest left edge shared by several lines (single outliers are ignored)."""
    edges = Counter(round(line.x0) for line in lines if line.size)
    common = [edge for edge, count in edges.items() if count >= 3]
    return float(min(common or edges or [0]))


def _normalize(text: str) -> str:
    return _NON_ALNUM.sub("", text.lower())
