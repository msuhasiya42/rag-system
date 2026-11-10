from collections.abc import Sequence

import pytest

from rag_system.answer_eval import (
    AnswerJudge,
    AnswerResult,
    JudgeError,
    Verdict,
    parse_verdict,
    run_answer_eval,
    summarize_answers,
)
from rag_system.answering import AnswerService
from rag_system.domain import Answer, Chunk, SearchHit
from rag_system.evaluation import Evidence, GoldenCase
from rag_system.ports import ChatModel, ChatReply
from rag_system.retrieval import DenseRetriever
from tests.fakes import FakeEmbedder, InMemoryVectorStore, as_units

GEICO = Chunk(
    "r#1",
    "r",
    "r.pdf",
    1,
    "GEICO pre-tax underwriting earnings $ 6,824 $ 7,813",
    61,
    61,
    "Item 7 > Insurance—Underwriting",
)
OTHER = Chunk("r#2", "r", "r.pdf", 2, "Railroad route miles 32,500", 55, 55, "Item 2")


def _case(
    answerable: bool = True, facts: Sequence[str] = ("$6,824 million in 2025",)
) -> GoldenCase:
    evidence = [Evidence(pages=[61], section="Underwriting")] if answerable else []
    return GoldenCase(
        id="q",
        split="test",
        category="table_number",
        question="GEICO earnings 2025?",
        answerable=answerable,
        evidence=evidence,
        expected_facts=list(facts),
    )


def _result(
    text: str, sources: list[Chunk], verdict: Verdict | None, case: GoldenCase | None = None
) -> AnswerResult:
    answer = Answer(text, "m", [SearchHit(chunk, 1.0) for chunk in sources])
    return AnswerResult(case or _case(), answer, verdict, "", "m", "m", 1.0)


CORRECT = Verdict(correctness="correct", abstained=False, faithful=True)
WRONG = Verdict(correctness="incorrect", abstained=False, faithful=True)


def test_parse_verdict_accepts_fenced_json_and_rejects_garbage() -> None:
    verdict = parse_verdict(
        '```json\n{"correctness": "partial", "abstained": false, "faithful": true}\n```'
    )

    assert verdict.correctness == "partial"
    with pytest.raises(JudgeError):
        parse_verdict("looks fine to me")
    with pytest.raises(JudgeError):
        parse_verdict('{"correctness": "great", "abstained": false, "faithful": true}')


def test_citations_must_point_at_real_sources_and_exist_for_answerable_questions() -> None:
    assert _result("Earnings were $6,824 million [1].", [GEICO], CORRECT).citations_valid
    assert not _result("Earnings were $6,824 million [3].", [GEICO], CORRECT).citations_valid
    assert not _result("Earnings were $6,824 million.", [GEICO], CORRECT).citations_valid
    unanswerable = _result("Not in the sources.", [GEICO], CORRECT, _case(answerable=False))
    assert unanswerable.citations_valid


def test_key_numbers_ignore_formatting() -> None:
    case = _case(facts=["$6,824 million in 2025 ($7,813 million in 2024)"])

    assert _result("It was 6824 million [1].", [GEICO], CORRECT, case).key_numbers_found == 0.5
    assert _result("$6,824m vs $7,813m [1]", [GEICO], CORRECT, case).key_numbers_found == 1.0


def test_wrong_answers_are_blamed_on_retrieval_only_when_evidence_was_missing() -> None:
    assert _result("Wrong [1].", [OTHER], WRONG).failure_cause == "retrieval"
    assert _result("Wrong [1].", [GEICO], WRONG).failure_cause == "generation"
    assert _result("Right [1].", [GEICO], CORRECT).failure_cause == ""


class ScriptedModel(ChatModel):
    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []

    def generate(self, system_prompt: str, user_prompt: str) -> ChatReply:
        self.prompts.append(user_prompt)
        return ChatReply(self.replies.pop(0), "scripted")


def test_run_answers_then_judges_each_question_and_summarizes() -> None:
    store = InMemoryVectorStore()
    store.upsert(as_units([GEICO]), FakeEmbedder().embed_documents([GEICO.text]))
    answerer = ScriptedModel("GEICO earned $6,824 million [1].", "The sources do not say.")
    judge = ScriptedModel(
        '{"correctness": "correct", "abstained": false, "faithful": true}',
        '{"correctness": "correct", "abstained": true, "faithful": true}',
    )
    service = AnswerService(DenseRetriever(FakeEmbedder(), store), answerer)
    pauses: list[float] = []

    results = run_answer_eval(
        service, AnswerJudge(judge), [_case(), _case(answerable=False)], 3, 2.0, pauses.append
    )
    summary = summarize_answers(results)

    assert "GEICO earned $6,824 million [1]." in judge.prompts[0]
    assert "Expected facts" in judge.prompts[0] and "$6,824 million in 2025" in judge.prompts[0]
    assert (summary.correct, summary.abstention_correct, summary.citations_valid) == (1.0, 1.0, 1.0)
    assert summary.key_numbers_found == 1.0
    assert pauses == [2.0] * 4  # after every answer and every judgement


def test_an_unreadable_judgement_is_recorded_as_an_error() -> None:
    store = InMemoryVectorStore()
    store.upsert(as_units([GEICO]), FakeEmbedder().embed_documents([GEICO.text]))
    service = AnswerService(DenseRetriever(FakeEmbedder(), store), ScriptedModel("answer [1]"))

    [result] = run_answer_eval(service, AnswerJudge(ScriptedModel("no json")), [_case()], 3)

    assert result.verdict is None and "judge reply" in result.error
    assert summarize_answers([result]).errors == 1


def test_a_year_counts_as_a_key_number_only_when_it_is_the_answer() -> None:
    due = _case(facts=["2027"])

    assert _result("They are due in 2027 [1].", [GEICO], CORRECT, due).key_numbers_found == 1.0


def test_markdown_report_lists_every_question_with_its_answer(tmp_path: object) -> None:
    from rag_system.eval_report import format_answer_markdown

    result = _result("GEICO earned $6,824 million [1].", [GEICO], CORRECT)

    page = format_answer_markdown(
        "run", {"pipeline": "advanced"}, [result], summarize_answers([result])
    )

    assert "# Answer evaluation: run" in page
    assert "| Correct | 100% |" in page
    assert "**Question:** GEICO earnings 2025?" in page
    assert "> GEICO earned $6,824 million [1]." in page
    assert "Item 7 > Insurance—Underwriting, page 61" in page
