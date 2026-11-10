from rag_system.domain import OutlineEntry, TextLine
from rag_system.ingestion.structure import (
    BlockKind,
    Section,
    build_sections,
    is_table_row,
    split_table_row,
)
from tests.builders import BODY, LEFT, body, filler, heading, page, plain_page


def _paths(sections: list[Section]) -> list[str]:
    return [" > ".join(section.path) for section in sections]


def _text(section: Section) -> str:
    return "\n".join(line.text for block in section.blocks for line in block.lines)


def test_numbering_sets_heading_levels() -> None:
    pages = [
        page(
            1,
            heading("Item 7. Management's Discussion"),
            *filler(3),
            heading("Insurance Underwriting"),
            *filler(3),
            heading("(1) Significant accounting policies"),
            *filler(3),
            heading("Item 8. Financial Statements"),
            *filler(3),
        )
    ]

    paths = _paths(build_sections(pages, []))

    assert paths == [
        "Item 7. Management's Discussion",
        "Item 7. Management's Discussion > Insurance Underwriting",
        "Item 7. Management's Discussion > (1) Significant accounting policies",
        "Item 8. Financial Statements",
    ]


def test_outline_entries_anchor_on_their_heading_line_mid_page() -> None:
    pages = [
        page(1, heading("Item 1. Business"), *filler(4, "Business")),
        page(2, *filler(3, "More business"), heading("Item 1A. Risk Factors"), *filler(3, "Risk")),
    ]
    outline = [
        OutlineEntry(0, "Form 10-K", 1),
        OutlineEntry(1, "Business", 1),
        OutlineEntry(1, "Risk Factors", 2),
    ]

    sections = build_sections(pages, outline)

    assert _paths(sections) == [
        "Form 10-K > Item 1. Business",
        "Form 10-K > Item 1A. Risk Factors",
    ]
    assert "More business sentence" in _text(sections[0])  # page 2 before the heading
    assert "Risk sentence" in _text(sections[1])


def test_outline_entry_without_a_matching_line_starts_at_the_top_of_its_page() -> None:
    pages = [page(1, *filler(3, "Intro")), page(2, *filler(3, "Letter"))]

    sections = build_sections(pages, [OutlineEntry(0, "CEO's Letter", 2)])

    assert _paths(sections) == ["", "CEO's Letter"]
    assert "Letter sentence" in _text(sections[1])


def test_a_heading_repeated_on_a_continuation_page_does_not_start_a_new_section() -> None:
    pages = [
        page(1, heading("Insurance Underwriting"), *filler(3, "First")),
        page(2, heading("Insurance Underwriting"), *filler(3, "Second")),
    ]

    sections = build_sections(pages, [])

    assert _paths(sections) == ["Insurance Underwriting"]
    assert "Second sentence" in _text(sections[0])


def test_running_headers_at_the_top_of_pages_are_dropped() -> None:
    pages = [
        page(n, heading("Notes to Financial Statements", x0=200), *filler(3, f"Page{n}"))
        for n in range(1, 5)
    ]

    sections = build_sections(pages, [])

    assert _paths(sections) == [""]
    assert "Notes to Financial Statements" not in _text(sections[0])


def test_a_heading_wrapped_onto_two_lines_is_one_heading() -> None:
    pages = [
        page(
            1,
            heading("Unfavorable conditions may impair our ability to", top=700),
            heading("access capital markets at a reasonable cost.", top=688),
            *[body(line.text, top=670 - 14 * i) for i, line in enumerate(filler(3))],
        )
    ]

    paths = _paths(build_sections(pages, []))

    assert paths == [
        "Unfavorable conditions may impair our ability to access capital markets at a reasonable "
        "cost."
    ]


def test_headings_on_a_contents_page_are_ignored() -> None:
    contents = page(
        1,
        heading("Table of Contents"),
        *[heading(f"Item {n}. Topic number {n} K-{n * 3}") for n in range(1, 7)],
    )

    sections = build_sections([contents], [])

    assert _paths(sections) == [""]


def test_bold_table_labels_and_numbers_are_not_headings() -> None:
    pages = [page(1, *filler(3), heading("Assets:"), heading("2025 2024"), heading("x y"))]

    assert _paths(build_sections(pages, [])) == [""]


def test_tables_keep_their_column_headings_and_label_rows() -> None:
    small = BODY * 0.8
    pages = [
        page(
            1,
            body("Results are summarized below."),
            TextLine("(dollars in millions)", size=small, x0=LEFT),
            TextLine("2025 2024", size=small, bold=True, x0=300),
            body("Assets:"),
            body("Cash and equivalents $ 47,719 $ 44,333"),
            body("Inventories 24,424 24,008"),
            body("Total 72,143 68,341"),
            body("The increase reflected higher cash balances across the businesses."),
        )
    ]

    [section] = build_sections(pages, [])

    kinds = [block.kind for block in section.blocks]
    assert kinds == [BlockKind.PARAGRAPH, BlockKind.TABLE, BlockKind.PARAGRAPH]
    table = [line.text for line in section.blocks[1].lines]
    assert table[:3] == ["(dollars in millions)", "2025 2024", "Assets:"]
    assert table[-1] == "Total 72,143 68,341"


def test_an_indented_line_starts_a_new_paragraph() -> None:
    pages = [
        page(
            1,
            body("First paragraph starts here", x0=LEFT + 20),
            body("and continues on this line."),
            body("Second paragraph starts here", x0=LEFT + 20),
            body("and ends here."),
            body("Filler line one."),
        )
    ]

    [section] = build_sections(pages, [])

    assert [len(block.lines) for block in section.blocks] == [2, 3]


def test_text_without_font_information_is_one_section_and_one_paragraph() -> None:
    sections = build_sections([plain_page(1, "INVOICE\nTotal due 4,200 USD\nThank you")], [])

    assert _paths(sections) == [""]
    assert [block.kind for block in sections[0].blocks] == [BlockKind.PARAGRAPH]


def test_table_rows_end_in_two_or_more_numeric_cells() -> None:
    assert is_table_row("Cash and cash equivalents* $ 47,719 $ 44,333")
    assert is_table_row("1966 (3.4) (11.7)")
    assert is_table_row("Building — — 19,609")
    assert not is_table_row("Revenues increased in 2025")
    assert not is_table_row("Amount % Amount %")


def test_split_table_row_separates_label_and_cells() -> None:
    assert split_table_row("Cash and cash equivalents* $ 47,719 $ 44,333") == (
        "Cash and cash equivalents*",
        ["47,719", "44,333"],
    )
    assert split_table_row("Building — — 19,609") == ("Building", ["—", "—", "19,609"])
    assert split_table_row("No numbers here") == ("No numbers here", [])
