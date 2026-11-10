"""Small helpers for building pages and lines in tests."""

from rag_system.domain import PageText, TextLine, TextSource

BODY = 11.0
LEFT = 45.0


def body(text: str, top: float = 0.0, x0: float = LEFT) -> TextLine:
    return TextLine(text, size=BODY, bold=False, x0=x0, top=top)


def heading(text: str, top: float = 0.0, x0: float = LEFT) -> TextLine:
    return TextLine(text, size=BODY, bold=True, x0=x0, top=top)


def page(number: int, *lines: TextLine) -> PageText:
    """Lines are laid out top to bottom 14 points apart unless they set ``top`` themselves."""
    placed = [
        line if line.top else TextLine(line.text, line.size, line.bold, line.x0, 700 - 14 * i)
        for i, line in enumerate(lines)
    ]
    return PageText(number, placed, TextSource.TEXT_LAYER)


def plain_page(number: int, text: str) -> PageText:
    """A page without font information, like OCR output."""
    return PageText(number, [TextLine(line) for line in text.split("\n")], TextSource.OCR)


def filler(count: int, prefix: str = "Body") -> list[TextLine]:
    """Ordinary body lines; three or more make the page's left margin unambiguous."""
    return [body(f"{prefix} sentence number {i} about the business.") for i in range(count)]
