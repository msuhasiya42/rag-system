"""Remove PDF layout noise that would pollute chunks and embeddings."""

import re
from collections import Counter
from dataclasses import replace

from rag_system.domain import PageText, TextLine

# Lines at the top and bottom of each page that are checked for repeated headers/footers.
MARGIN_LINES = 2
# A margin line repeated on at least this share of pages is a header or footer.
REPEATED_MARGIN_SHARE = 0.3
# Fewer pages than this give too little evidence to call a line "repeated".
MIN_PAGES_FOR_REPEAT_DETECTION = 4

# "12", "- 12 -", "Page 3", "Page 3 of 10", "K-55", "A-2"
_PAGE_NUMBER = re.compile(
    r"^[-–\s]*(page\s+)?([a-z]{1,2}-)?\d{1,4}(\s+of\s+\d{1,4})?[-–\s]*$", re.IGNORECASE
)
# Dot leaders in contents pages and tables: "1965 ........ 49.5%"
_LEADER_DOTS = re.compile(r"(?:\s?\.){4,}")
_SPACES = re.compile(r"[ \t ]+")
# A word broken across lines: "infor-" + "mation was" -> "information" + "was"
_TRAILING_HYPHENATED = re.compile(r"[a-z]-$")
_LEADING_LOWERCASE_WORD = re.compile(r"^([a-z]\S*)\s*")


def clean_pages(pages: list[PageText]) -> list[PageText]:
    normalized = [replace(page, lines=_normalize_lines(page.lines)) for page in pages]
    repeated = _repeated_margin_keys(normalized)
    return [
        replace(page, lines=_join_hyphenated(_drop_margins(page.lines, repeated)))
        for page in normalized
    ]


def _normalize_lines(lines: list[TextLine]) -> list[TextLine]:
    cleaned = (replace(line, text=_normalize_text(line.text)) for line in lines)
    return [line for line in cleaned if line.text]


def _normalize_text(text: str) -> str:
    return _SPACES.sub(" ", _LEADER_DOTS.sub(" ", text)).strip()


def _drop_margins(lines: list[TextLine], repeated_margin_keys: set[str]) -> list[TextLine]:
    margin_indexes = _margin_indexes(len(lines))
    return [
        line
        for index, line in enumerate(lines)
        if not (
            index in margin_indexes
            and (_PAGE_NUMBER.match(line.text) or _margin_key(line.text) in repeated_margin_keys)
        )
    ]


def _join_hyphenated(lines: list[TextLine]) -> list[TextLine]:
    """Move the rest of a hyphen-split word up to the line where it starts."""
    joined = list(lines)
    for index in range(len(joined) - 1):
        current, following = joined[index], joined[index + 1]
        match = _LEADING_LOWERCASE_WORD.match(following.text)
        if _TRAILING_HYPHENATED.search(current.text) and match:
            joined[index] = replace(current, text=current.text[:-1] + match.group(1))
            joined[index + 1] = replace(following, text=following.text[match.end() :])
    return [line for line in joined if line.text]


def _repeated_margin_keys(pages: list[PageText]) -> set[str]:
    if len(pages) < MIN_PAGES_FOR_REPEAT_DETECTION:
        return set()
    counts: Counter[str] = Counter()
    for page in pages:
        indexes = _margin_indexes(len(page.lines))
        counts.update({_margin_key(page.lines[index].text) for index in indexes})
    threshold = max(2, REPEATED_MARGIN_SHARE * len(pages))
    return {key for key, count in counts.items() if count >= threshold}


def _margin_indexes(line_count: int) -> set[int]:
    # On a short page, the margins must not reach the body text in the middle.
    margin = min(MARGIN_LINES, line_count // 2)
    return set(range(margin)) | set(range(line_count - margin, line_count))


def _margin_key(text: str) -> str:
    """Normalize so 'Page 3' and 'Page 4' count as the same footer."""
    return re.sub(r"\d+", "#", text.lower())
