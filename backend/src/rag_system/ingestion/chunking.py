"""Pack each section's paragraphs and tables into chunks of about ``size_chars``.

Rules:
- A chunk never crosses a section boundary, so it is about one topic, and it carries the
  section's heading path for embedding and citations.
- A small section becomes one chunk. A large one is split between blocks; a paragraph longer
  than a chunk is split between lines.
- A table is never split when it fits in a chunk. A larger table is split between rows and
  each piece repeats the column headings, so numbers keep their year and unit labels.
- Consecutive chunks of the same section overlap by up to ``overlap_chars`` of trailing
  paragraph text. Table rows are not repeated as overlap.
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass

from rag_system.domain import Chunk, SearchUnit, TextLine
from rag_system.ingestion.structure import (
    Block,
    BlockKind,
    PlacedLine,
    Section,
    is_table_row,
    split_table_row,
)

PATH_SEPARATOR = " > "
_WORD = re.compile(r"[A-Za-z]{3}")


def document_id_for(file_name: str) -> str:
    """A stable id from the file name, so re-ingesting a file replaces its old chunks."""
    stem = file_name.rsplit(".", 1)[0]
    return re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-") or "document"


def chunk_sections(
    document_id: str,
    document_name: str,
    sections: list[Section],
    size_chars: int,
    overlap_chars: int,
) -> list[Chunk]:
    packed = _chunk_with_lines(document_id, document_name, sections, size_chars, overlap_chars)
    return [chunk for chunk, _ in packed]


def search_units(
    document_id: str,
    document_name: str,
    sections: list[Section],
    size_chars: int,
    overlap_chars: int,
    table_rows: bool,
) -> list[SearchUnit]:
    """Every chunk, plus (with ``table_rows``) one unit per labelled table row."""
    packed = _chunk_with_lines(document_id, document_name, sections, size_chars, overlap_chars)
    units = [SearchUnit.for_chunk(chunk) for chunk, _ in packed]
    if table_rows:
        units.extend(_table_row_units(sections, packed))
    return units


def _chunk_with_lines(
    document_id: str,
    document_name: str,
    sections: list[Section],
    size_chars: int,
    overlap_chars: int,
) -> list[tuple[Chunk, list[PlacedLine]]]:
    if overlap_chars >= size_chars:
        raise ValueError("overlap_chars must be smaller than size_chars")
    packed: list[tuple[Chunk, list[PlacedLine]]] = []
    for section in sections:
        path = PATH_SEPARATOR.join(section.path)
        for lines in _pack(section.blocks, size_chars, overlap_chars):
            if not any(_WORD.search(line.text) for line in lines):
                continue  # only numbers or symbols, e.g. a lone year on a cover page
            chunk = _make_chunk(document_id, document_name, len(packed), path, lines)
            packed.append((chunk, lines))
    return packed


def _table_row_units(
    sections: list[Section], packed: list[tuple[Chunk, list[PlacedLine]]]
) -> list[SearchUnit]:
    """One unit per labelled row, written as a sentence that carries what a lone row lacks:
    the table, the group label above the row, and each value paired with its column year
    ("CONSOLIDATED BALANCE SHEETS: Total assets: 2025: 1,222,176; 2024: 1,153,881")."""
    owner: dict[int, Chunk] = {}  # line object -> the first chunk that holds it
    for chunk, lines in packed:
        for line in lines:
            owner.setdefault(id(line), chunk)
    rows_per_chunk: dict[str, int] = {}
    units: list[SearchUnit] = []
    for section in sections:
        title = _table_title(section.path)
        for block in section.blocks:
            if block.kind != BlockKind.TABLE:
                continue
            header_end = _table_header_end(block.lines)
            header = block.lines[:header_end]
            years = _column_years(header)
            units_note = _units_note(header)
            # "Assets:" above the first row is a group label, not a column heading.
            group = next((line.text for line in reversed(header) if line.text.endswith(":")), "")
            for line in block.lines[header_end:]:
                if not (is_table_row(line.text) and _has_label(line.text)):
                    group = line.text if line.text.endswith(":") else group
                    continue
                parent = owner.get(id(line))
                if parent is None:
                    continue
                number = rows_per_chunk.get(parent.id, 0)
                rows_per_chunk[parent.id] = number + 1
                text = _linearize(title, group, line.text, years, units_note)
                units.append(SearchUnit(f"{parent.id}-r{number:03d}", text, parent, line.text))
    return units


_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
_UNITS_NOTE = re.compile(r"\((?:dollars )?in (?:millions|thousands|billions)[^)]*\)", re.IGNORECASE)


def _linearize(title: str, group: str, row: str, years: list[str], units_note: str) -> str:
    label, cells = split_table_row(row)
    label = label.rstrip("*").strip()
    # "Total ..." rows sum the whole table, not the group above them.
    if group and not label.lower().startswith("total"):
        label = f"{group.rstrip(':')} {label}"
    if years and len(cells) >= len(years):
        paired = [f"{year}: {cell}" for year, cell in zip(years, cells, strict=False)]
        values = "; ".join(paired + cells[len(years) :])
    else:
        values = " ".join(cells)
    note = f" {units_note}" if units_note else ""
    return f"{title}: {label}: {values}{note}" if title else f"{label}: {values}{note}"


def _table_title(path: tuple[str, ...]) -> str:
    """The section the table sits in; a short title ("Other") gets its parent for context."""
    if not path:
        return ""
    if len(path[-1]) < 12 and len(path) > 1:
        return f"{path[-2]} > {path[-1]}"
    return path[-1]


def _column_years(header: list[PlacedLine]) -> list[str]:
    """The column years from the headings, e.g. "2025 2024 2023 2025 vs 2024" -> 2025, 2024,
    2023 (stops where years repeat, as in percentage-change columns)."""
    for line in reversed(header):
        found = _YEAR.findall(line.text)
        if len(found) >= 2:
            years: list[str] = []
            for year in found:
                if year in years:
                    break
                years.append(year)
            return years
    return []


def _units_note(header: list[PlacedLine]) -> str:
    for line in header:
        if match := _UNITS_NOTE.search(line.text):
            return match.group(0)
    return ""


@dataclass(frozen=True)
class _Unit:
    """Lines that go into one chunk together."""

    lines: list[PlacedLine]
    overlappable: bool  # paragraph text may be repeated at the start of the next chunk


def _pack(blocks: list[Block], size_chars: int, overlap_chars: int) -> list[list[PlacedLine]]:
    packed: list[list[PlacedLine]] = []
    window: list[PlacedLine] = []
    has_new_text = False  # the window holds more than overlap from the previous chunk
    last_overlappable = False
    for unit in _units(blocks, size_chars):
        if window and _length(window) + _length(unit.lines) > size_chars:
            if has_new_text:
                packed.append(window)
                window = _tail(window, overlap_chars) if last_overlappable else []
            # Drop overlap that leaves no room for the next unit.
            while window and _length(window) + _length(unit.lines) > size_chars:
                window = window[1:]
            has_new_text = False
        window = window + unit.lines
        has_new_text = True
        last_overlappable = unit.overlappable
    if has_new_text:
        packed.append(window)
    return packed


def _units(blocks: list[Block], size_chars: int) -> Iterator[_Unit]:
    for block in blocks:
        is_paragraph = block.kind == BlockKind.PARAGRAPH
        if _length(block.lines) <= size_chars:
            yield _Unit(block.lines, is_paragraph)
        elif is_paragraph:
            for piece in _split_lines(block.lines, size_chars):
                yield _Unit(piece, True)
        else:
            for piece in _split_table(block.lines, size_chars):
                yield _Unit(piece, False)


def _split_table(lines: list[PlacedLine], size_chars: int) -> list[list[PlacedLine]]:
    """Split between rows; every piece starts with the column headings."""
    header_end = _table_header_end(lines)
    header, rows = lines[:header_end], lines[header_end:]
    if _length(header) > size_chars // 2:  # headings too long to repeat
        header, rows = [], lines
    return [header + piece for piece in _split_lines(rows, size_chars - _length(header))]


def _split_lines(lines: list[PlacedLine], size_chars: int) -> list[list[PlacedLine]]:
    pieces: list[list[PlacedLine]] = []
    current: list[PlacedLine] = []
    parts = (part for line in lines for part in _split_long_line(line, size_chars))
    for part in parts:
        if current and _length(current) + len(part.text) + 1 > size_chars:
            pieces.append(current)
            current = []
        current.append(part)
    if current:
        pieces.append(current)
    return pieces


def _split_long_line(line: PlacedLine, size_chars: int) -> list[PlacedLine]:
    """OCR and some PDFs produce very long lines; split them at word boundaries."""
    if len(line.text) <= size_chars:
        return [line]
    texts: list[str] = []
    current = ""
    for word in line.text.split(" "):
        while len(word) > size_chars:  # a single "word" longer than a chunk
            if current:
                texts.append(current)
                current = ""
            texts.append(word[:size_chars])
            word = word[size_chars:]
        candidate = f"{current} {word}" if current else word
        if len(candidate) > size_chars:
            texts.append(current)
            current = word
        else:
            current = candidate
    if current:
        texts.append(current)
    return [PlacedLine(line.page_number, TextLine(text)) for text in texts]


def _tail(lines: list[PlacedLine], overlap_chars: int) -> list[PlacedLine]:
    """The trailing lines that fit in the overlap budget."""
    tail: list[PlacedLine] = []
    total = 0
    for line in reversed(lines):
        if total + len(line.text) + 1 > overlap_chars:
            break
        tail.insert(0, line)
        total += len(line.text) + 1
    return tail


def _table_header_end(lines: list[PlacedLine]) -> int:
    """Lines before the first labelled row are column headings ("(dollars in millions)",
    "2025 2024"); 0 when no row has a label."""
    return next(
        (i for i, line in enumerate(lines) if is_table_row(line.text) and _has_label(line.text)),
        0,
    )


def _has_label(text: str) -> bool:
    return re.search(r"[A-Za-z]{2}", text) is not None


def _length(lines: list[PlacedLine]) -> int:
    return sum(len(line.text) + 1 for line in lines)


def _make_chunk(
    document_id: str, document_name: str, index: int, section: str, lines: list[PlacedLine]
) -> Chunk:
    return Chunk(
        id=f"{document_id}#{index:05d}",
        document_id=document_id,
        document_name=document_name,
        chunk_index=index,
        text="\n".join(line.text for line in lines),
        page_start=lines[0].page_number,
        page_end=lines[-1].page_number,
        section=section,
    )
