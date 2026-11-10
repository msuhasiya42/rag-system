from rag_system.domain import PageText, TextLine, TextSource
from rag_system.ingestion.cleaning import clean_pages


def _page(number: int, text: str) -> PageText:
    return PageText(number, [TextLine(line) for line in text.split("\n")], TextSource.TEXT_LAYER)


def test_repeated_headers_and_page_numbers_are_removed() -> None:
    pages = [
        _page(n, f"ACME Corp Annual Report 2025\nBody text of page {n}.\nPage {n} of 6")
        for n in range(1, 7)
    ]

    cleaned = clean_pages(pages)

    assert [page.text for page in cleaned] == [f"Body text of page {n}." for n in range(1, 7)]


def test_section_style_page_numbers_are_removed_even_in_short_documents() -> None:
    cleaned = clean_pages([_page(1, "K-55\nManagement discussion.")])

    assert cleaned[0].text == "Management discussion."


def test_repeated_text_in_the_body_is_kept() -> None:
    text = "line one\nline two\nNet income increased\nline four\nline five"
    cleaned = clean_pages([_page(n, text) for n in range(1, 6)])

    assert "Net income increased" in cleaned[0].text


def test_words_split_across_lines_are_joined_and_spaces_collapsed() -> None:
    cleaned = clean_pages([_page(1, "The  infor-\nmation was   audited.")])

    assert cleaned[0].text == "The information\nwas audited."


def test_numbers_split_by_a_hyphen_are_not_joined() -> None:
    cleaned = clean_pages([_page(1, "Fiscal years 2024-\n2025 compared.")])

    assert cleaned[0].text == "Fiscal years 2024-\n2025 compared."


def test_leader_dots_are_removed() -> None:
    cleaned = clean_pages([_page(1, "1965 ........ 49.5% 10.0%\nRisk Factors . . . . . K-24")])

    assert cleaned[0].text == "1965 49.5% 10.0%\nRisk Factors K-24"


def test_style_is_kept_on_cleaned_lines() -> None:
    page = PageText(
        1, [TextLine("Item 1.  Business", 11.0, True, 45.0, 700.0)], TextSource.TEXT_LAYER
    )

    [line] = clean_pages([page])[0].lines

    assert (line.text, line.bold, line.size) == ("Item 1. Business", True, 11.0)
