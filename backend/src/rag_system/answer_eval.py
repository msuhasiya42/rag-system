"""End-to-end answer evaluation: does the system answer the golden questions correctly?

Each question is answered by the full pipeline (retrieval + answer model), then scored:

- correctness (LLM judge): correct / partial / incorrect against the golden expected facts;
  for unanswerable questions, correct means the answer says the report does not contain it.
- faithfulness (LLM judge): every claim in the answer is supported by the retrieved sources.
- citations (deterministic): [n] markers point at real sources; answers to answerable
  questions cite at least one.
- key numbers (deterministic): the figures in the expected facts appear in the answer. A
  judge-free cross-check on the LLM judge.
- evidence in context (deterministic): the golden evidence was among the sources the model
  saw. A wrong answer with evidence in context is a generation failure; without it, a
  retrieval failure.
"""

import json
import re
import statistics
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ValidationError

from rag_system.answering import AnswerService, describe_source
from rag_system.domain import Answer
from rag_system.evaluation import GoldenCase
from rag_system.ports import ChatModel, UpstreamServiceError

JUDGE_SOURCE_CHARS = 1500  # per source shown to the judge; chunks are at most ~1,800

JUDGE_SYSTEM_PROMPT = """You grade answers from a question-answering system about a company \
annual report. Be strict and literal. Reply with one JSON object and nothing else."""

JUDGE_TEMPLATE = """Question: {question}

Answerable from the report: {answerable}
Expected facts (the reference answer):
{facts}

Sources the system was given:
{sources}

System answer:
<answer>
{answer}
</answer>

Grade the answer:
- "correctness":
  - answerable question: "correct" if it states every expected fact (equivalent formatting and
    rounding are fine, e.g. "$6,824 million" = "$6.8 billion"); "partial" if it states some
    expected facts and nothing that contradicts them; "incorrect" if it misses the key fact,
    contradicts it, or says the information is not available.
  - unanswerable question: "correct" if it says the sources do not contain the answer and does
    not invent one; otherwise "incorrect".
- "abstained": true if the answer says the information is not in the sources.
- "faithful": true if every factual claim in the answer is supported by the sources above
  (judge only against the sources, not the expected facts).
- "explanation": one short sentence.

Reply as JSON: {{"correctness": "...", "abstained": true|false, "faithful": true|false,
"explanation": "..."}}"""


class Verdict(BaseModel):
    correctness: Literal["correct", "partial", "incorrect"]
    abstained: bool
    faithful: bool
    explanation: str = ""


class JudgeError(Exception):
    """The judge's reply could not be read as a verdict."""


class AnswerJudge:
    def __init__(self, model: ChatModel) -> None:
        self._model = model

    def grade(self, case: GoldenCase, answer: Answer) -> tuple[Verdict, str]:
        """Returns the verdict and the id of the model that judged."""
        sources = "\n\n".join(
            f"[{n}] {describe_source(hit)}\n{hit.chunk.text[:JUDGE_SOURCE_CHARS]}"
            for n, hit in enumerate(answer.sources, start=1)
        )
        prompt = JUDGE_TEMPLATE.format(
            question=case.question,
            answerable="yes" if case.answerable else "no",
            facts="\n".join(f"- {fact}" for fact in case.expected_facts),
            sources=sources or "(none)",
            answer=answer.text,
        )
        reply = self._model.generate(JUDGE_SYSTEM_PROMPT, prompt)
        return parse_verdict(reply.text), reply.model_id


def parse_verdict(text: str) -> Verdict:
    match = re.search(r"\{.*\}", text, re.DOTALL)  # tolerate ```json fences or a preamble
    if not match:
        raise JudgeError(f"No JSON object in judge reply: {text[:200]!r}")
    try:
        return Verdict.model_validate(json.loads(match.group(0)))
    except (json.JSONDecodeError, ValidationError) as error:
        raise JudgeError(f"Unreadable judge reply: {text[:200]!r}") from error


@dataclass(frozen=True)
class AnswerResult:
    case: GoldenCase
    answer: Answer
    verdict: Verdict | None  # None when the answer or the judge failed
    error: str
    answer_model: str | None
    judge_model: str | None
    latency_ms: float

    @property
    def citations_valid(self) -> bool:
        cited = {int(n) for n in re.findall(r"\[(\d+)\]", self.answer.text)}
        in_range = all(1 <= n <= len(self.answer.sources) for n in cited)
        return in_range and (bool(cited) or not self.case.answerable)

    @property
    def key_numbers_found(self) -> float | None:
        """Share of the expected facts' figures (3+ digits) that appear in the answer."""
        figures = {n for fact in self.case.expected_facts for n in _figures(fact)}
        # Years give context ("in 2025") unless they are the answer itself ("due 2027").
        wanted = {n for n in figures if not _YEAR.fullmatch(n)} or figures
        if not wanted or not self.case.answerable:
            return None
        found = set(_figures(self.answer.text))
        return len(wanted & found) / len(wanted)

    @property
    def evidence_in_context(self) -> bool:
        return any(
            item.supported_by(hit.chunk)
            for item in self.case.evidence
            for hit in self.answer.sources
        )

    @property
    def failure_cause(self) -> str:
        if self.verdict is None:
            return "error"
        if self.verdict.correctness == "correct":
            return ""
        if self.case.answerable and not self.evidence_in_context:
            return "retrieval"
        return "generation"


_YEAR = re.compile(r"(?:19|20)\d{2}")


def _figures(text: str) -> list[str]:
    """Numbers with three or more digits, commas and leading $ removed: "$6,824" -> "6824"."""
    return [n.replace(",", "") for n in re.findall(r"\d[\d,]{2,}(?:\.\d+)?", text)]


Pause = Callable[[float], None]


def run_answer_eval(
    service: AnswerService,
    judge: AnswerJudge,
    cases: Sequence[GoldenCase],
    top_k: int,
    pause_seconds: float = 0.0,
    sleep: Pause = time.sleep,
    progress: Callable[[str], None] = lambda _: None,
) -> list[AnswerResult]:
    """``pause_seconds`` between model calls keeps free tiers under their per-minute limits."""
    results = []
    for number, case in enumerate(cases, start=1):
        started = time.perf_counter()
        answer = Answer(text="", model_id=None, sources=[])
        verdict, error, judge_model = None, "", None
        try:
            answer = service.answer(case.question, top_k)
            sleep(pause_seconds)
            verdict, judge_model = judge.grade(case, answer)
        except (UpstreamServiceError, JudgeError) as failure:
            error = str(failure)
        sleep(pause_seconds)
        result = AnswerResult(
            case,
            answer,
            verdict,
            error,
            answer.model_id,
            judge_model,
            (time.perf_counter() - started) * 1000,
        )
        results.append(result)
        label = verdict.correctness if verdict else f"ERROR {error[:60]}"
        progress(f"[{number}/{len(cases)}] {case.id} {label}")
    return results


@dataclass(frozen=True)
class AnswerSummary:
    questions: int
    errors: int
    answerable: int
    correct: float  # answerable questions, judged correct
    partial: float
    incorrect: float
    abstention_correct: float  # unanswerable questions answered with "not in the report"
    faithful: float  # all graded answers
    citations_valid: float
    key_numbers_found: float | None
    retrieval_failures: int
    generation_failures: int


def summarize_answers(results: Sequence[AnswerResult]) -> AnswerSummary:
    graded = [r for r in results if r.verdict is not None]
    answerable = [r for r in graded if r.case.answerable]
    unanswerable = [r for r in graded if not r.case.answerable]

    def share(rows: Sequence[AnswerResult], test: Callable[[AnswerResult], bool]) -> float:
        return sum(1 for row in rows if test(row)) / len(rows) if rows else 0.0

    numbers = [r.key_numbers_found for r in answerable if r.key_numbers_found is not None]
    return AnswerSummary(
        questions=len(results),
        errors=len(results) - len(graded),
        answerable=len(answerable),
        correct=share(
            answerable, lambda r: r.verdict is not None and r.verdict.correctness == "correct"
        ),
        partial=share(
            answerable, lambda r: r.verdict is not None and r.verdict.correctness == "partial"
        ),
        incorrect=share(
            answerable, lambda r: r.verdict is not None and r.verdict.correctness == "incorrect"
        ),
        abstention_correct=share(
            unanswerable, lambda r: r.verdict is not None and r.verdict.correctness == "correct"
        ),
        faithful=share(graded, lambda r: r.verdict is not None and r.verdict.faithful),
        citations_valid=share(graded, lambda r: r.citations_valid),
        key_numbers_found=statistics.fmean(numbers) if numbers else None,
        retrieval_failures=sum(1 for r in graded if r.failure_cause == "retrieval"),
        generation_failures=sum(1 for r in graded if r.failure_cause == "generation"),
    )
