import math
from pathlib import Path

import pytest

from rag_system.domain import Chunk, SearchHit
from rag_system.evaluation import (
    CaseResult,
    Evidence,
    GoldenCase,
    Location,
    load_golden_set,
    run_retrieval_eval,
    summarize,
)


def _hit(section: str, start: int, end: int | None = None) -> SearchHit:
    chunk = Chunk(f"d#{start}", "d", "d.pdf", 0, "text", start, end or start, section)
    return SearchHit(chunk, 0.5)


def _case(*evidence: Evidence, answerable: bool = True) -> GoldenCase:
    return GoldenCase(
        id="q",
        split="tune",
        category="c",
        question="?",
        answerable=answerable,
        evidence=list(evidence),
        expected_facts=[],
    )


BALANCE = Evidence(pages=[92], section="balance sheets")
CASH_FLOW = Evidence(pages=[96], section="Cash Flows")


def test_evidence_needs_overlapping_pages_and_a_matching_section() -> None:
    assert BALANCE.supported_by(_hit("Statements > CONSOLIDATED BALANCE SHEETS", 92, 93).chunk)
    assert BALANCE.supported_by(_hit("X > Consolidated Balance Sheets", 91, 92).chunk)
    assert not BALANCE.supported_by(_hit("MD&A > Other", 92).chunk)  # right page, wrong section
    assert not BALANCE.supported_by(_hit("Balance Sheets", 95).chunk)  # right section, wrong page
    assert Evidence(pages=[25]).supported_by(_hit("anything", 25).chunk)  # no section required


def test_metrics_for_one_question() -> None:
    hits = [
        _hit("MD&A > Other", 81),
        _hit("Balance Sheets", 92),
        _hit("Notes", 120),
        _hit("Balance Sheets", 93),  # right section, but page 93 is outside the evidence pages
        _hit("Cash Flows", 96),
    ]
    result = CaseResult(_case(BALANCE, CASH_FLOW), hits, latency_ms=10)

    assert result.first_relevant_rank() == 2
    assert result.reciprocal_rank() == 0.5
    assert (result.hit_at(1), result.hit_at(3)) == (0.0, 1.0)
    assert (result.recall_at(3), result.recall_at(5)) == (0.5, 1.0)
    assert result.precision_at(5) == pytest.approx(2 / 5)
    ideal = 1 + 1 / math.log2(3)
    assert result.ndcg_at(5) == pytest.approx((1 / math.log2(3) + 1 / math.log2(6)) / ideal)


def test_repeated_evidence_does_not_inflate_ndcg() -> None:
    hits = [_hit("Balance Sheets", 92), _hit("Balance Sheets", 92)]

    assert CaseResult(_case(BALANCE), hits, 0).ndcg_at(2) == pytest.approx(1.0)


def test_summary_skips_unanswerable_questions_and_reports_latency() -> None:
    answered = CaseResult(_case(BALANCE), [_hit("Balance Sheets", 92)], latency_ms=100)
    missed = CaseResult(_case(CASH_FLOW), [_hit("Other", 1)], latency_ms=300)
    unanswerable = CaseResult(_case(answerable=False), [_hit("Other", 1)], latency_ms=200)

    summary = summarize([answered, missed, unanswerable], ks=[1, 5])

    assert summary.questions == 2
    assert summary.hit == {1: 0.5, 5: 0.5}
    assert summary.mrr == 0.5
    assert (summary.latency_p50_ms, summary.latency_p95_ms) == (200, 300)


def test_run_retrieval_eval_asks_each_question_with_k() -> None:
    asked: list[tuple[str, int]] = []

    def retrieve(question: str, k: int) -> list[SearchHit]:
        asked.append((question, k))
        return []

    results = run_retrieval_eval(retrieve, [_case(BALANCE)], k=7)

    assert asked == [("?", 7)]
    assert results[0].hits == []


def test_the_golden_dataset_file_is_valid() -> None:
    golden = load_golden_set(Path(__file__).parent.parent / "evals" / "golden.json")

    assert len({case.id for case in golden.cases}) == len(golden.cases)
    for case in golden.cases:
        assert bool(case.evidence) == case.answerable, case.id


def test_evidence_is_supported_by_any_of_its_alternative_locations() -> None:
    auditor = Evidence(
        pages=[89],
        section="Internal Control",
        alternatives=[Location(pages=[91], section="Item 8")],
    )

    assert auditor.supported_by(_hit("Report on Internal Control", 89).chunk)
    assert auditor.supported_by(_hit("FORM 10-K > Item 8. Financial Statements", 91).chunk)
    assert not auditor.supported_by(_hit("Item 7", 91).chunk)
