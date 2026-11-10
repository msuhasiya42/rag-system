"""Print and save retrieval evaluation results."""

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from rag_system.answer_eval import AnswerResult, AnswerSummary
from rag_system.answering import format_pages
from rag_system.domain import SearchHit
from rag_system.evaluation import CaseResult, Summary

SECTION_TAIL_CHARS = 45


def format_report(results: Sequence[CaseResult], summary: Summary, ks: Sequence[int]) -> str:
    rows = [f"{'id':4} {'category':13} {'first':>5} {'R@5':>5}  top result"]
    for result in results:
        top = _describe(result.hits[0]) if result.hits else "-"
        rank = result.first_relevant_rank()
        rows.append(
            f"{result.case.id:4} {result.case.category:13} {rank or 'miss':>5} "
            f"{result.recall_at(5):>5.0%}  {top}"
        )
    metrics = [
        f"Questions scored: {summary.questions} (unanswerable ones are not retrieved)",
        "Hit@K       " + _by_k(summary.hit, ks),
        "Recall@K    " + _by_k(summary.recall, ks),
        "Precision@K " + _by_k(summary.precision, ks),
        "nDCG@K      " + _by_k(summary.ndcg, ks),
        f"MRR         {summary.mrr:.3f}",
        f"Latency     p50 {summary.latency_p50_ms:.0f} ms, p95 {summary.latency_p95_ms:.0f} ms",
    ]
    return "\n".join(rows) + "\n\n" + "\n".join(metrics)


def save_results(
    directory: Path,
    label: str,
    config: Mapping[str, object],
    results: Sequence[CaseResult],
    summary: Summary,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    path = directory / f"{stamp}-{label}.json"
    payload = {
        "label": label,
        "createdAt": stamp,
        "config": dict(config),
        "summary": asdict(summary),
        "cases": [
            {
                "id": result.case.id,
                "split": result.case.split,
                "category": result.case.category,
                "firstRelevantRank": result.first_relevant_rank() if result.case.evidence else None,
                "latencyMs": round(result.latency_ms, 1),
                "retrieved": [
                    {
                        "section": hit.chunk.section,
                        "pages": format_pages(hit),
                        "score": round(hit.score, 4),
                        "relevant": result.relevant(rank) if result.case.evidence else None,
                    }
                    for rank, hit in enumerate(result.hits, start=1)
                ],
            }
            for result in results
        ],
    }
    path.write_text(json.dumps(payload, indent=2))
    return path


def _describe(hit: SearchHit) -> str:
    section = hit.chunk.section
    if len(section) > SECTION_TAIL_CHARS:
        section = "…" + section[-SECTION_TAIL_CHARS:]
    return f"{section or '(no section)'}, {format_pages(hit)}  ({hit.score:.2f})"


def _by_k(values: Mapping[int, float], ks: Sequence[int]) -> str:
    return "  ".join(f"@{k}: {values[k]:.0%}" for k in ks)


def format_scoreboard(runs: Sequence[tuple[str, str, Summary]]) -> str:
    """One row per pipeline: (name, description, summary). Changes are against the first row."""
    header = (
        f"{'pipeline':13} {'how':31} {'Hit@1':>6} {'Hit@5':>6} {'Recall@5':>9} "
        f"{'nDCG@5':>7} {'MRR':>6} {'p50 ms':>7} {'p95 ms':>7}"
    )
    rows = [header, "-" * len(header)]
    baseline = runs[0][2]
    for name, description, summary in runs:
        delta = summary.mrr - baseline.mrr
        change = f"  ({delta:+.3f} MRR)" if summary is not baseline else ""
        rows.append(
            f"{name:13} {description:31} {summary.hit[1]:>6.0%} {summary.hit[5]:>6.0%} "
            f"{summary.recall[5]:>9.0%} {summary.ndcg[5]:>7.0%} {summary.mrr:>6.3f} "
            f"{summary.latency_p50_ms:>7.0f} {summary.latency_p95_ms:>7.0f}{change}"
        )
    return "\n".join(rows)


def format_rank_matrix(runs: Sequence[tuple[str, Sequence[CaseResult]]]) -> str:
    """Rank of the first relevant chunk for each question under each pipeline."""
    names = [name for name, _ in runs]
    rows = [f"{'id':4} {'split':5} {'category':13} " + " ".join(f"{name:>13}" for name in names)]
    for index, result in enumerate(runs[0][1]):
        if not result.case.evidence:
            continue
        ranks = [str(results[index].first_relevant_rank() or "miss") for _, results in runs]
        rows.append(
            f"{result.case.id:4} {result.case.split:5} {result.case.category:13} "
            + " ".join(f"{rank:>13}" for rank in ranks)
        )
    return "\n".join(rows)


def format_answer_report(results: Sequence[AnswerResult], summary: AnswerSummary) -> str:
    rows = [f"{'id':4} {'category':13} {'verdict':10} {'faithful':8} {'cites':5} {'cause':10}"]
    for r in results:
        verdict = r.verdict.correctness if r.verdict else "ERROR"
        faithful = ("yes" if r.verdict.faithful else "NO") if r.verdict else "-"
        rows.append(
            f"{r.case.id:4} {r.case.category:13} {verdict:10} {faithful:8} "
            f"{'ok' if r.citations_valid else 'BAD':5} {r.failure_cause:10}"
        )
    numbers = f"{summary.key_numbers_found:.0%}" if summary.key_numbers_found is not None else "n/a"
    lines = [
        f"Questions: {summary.questions} ({summary.answerable} answerable graded, "
        f"{summary.errors} errors)",
        f"Answer accuracy (answerable): correct {summary.correct:.0%}, "
        f"partial {summary.partial:.0%}, incorrect {summary.incorrect:.0%}",
        f"Unanswerable handled correctly (said not in report): {summary.abstention_correct:.0%}",
        f"Faithful to sources: {summary.faithful:.0%}",
        f"Valid citations: {summary.citations_valid:.0%}",
        f"Key numbers found in answers (no LLM): {numbers}",
        f"Not correct because of: retrieval {summary.retrieval_failures}, "
        f"generation {summary.generation_failures}",
    ]
    return "\n".join(rows) + "\n\n" + "\n".join(lines)


def save_answer_results(
    directory: Path,
    label: str,
    config: Mapping[str, object],
    results: Sequence[AnswerResult],
    summary: AnswerSummary,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    path = directory / f"{stamp}-{label}-answers.json"
    payload = {
        "label": label,
        "createdAt": stamp,
        "config": dict(config),
        "summary": asdict(summary),
        "cases": [
            {
                "id": r.case.id,
                "split": r.case.split,
                "category": r.case.category,
                "question": r.case.question,
                "expectedFacts": r.case.expected_facts,
                "answer": r.answer.text,
                "answerModel": r.answer_model,
                "judgeModel": r.judge_model,
                "verdict": r.verdict.model_dump() if r.verdict else None,
                "error": r.error,
                "citationsValid": r.citations_valid,
                "keyNumbersFound": r.key_numbers_found,
                "evidenceInContext": r.evidence_in_context,
                "failureCause": r.failure_cause,
                "sources": [
                    format_pages(hit) + " | " + hit.chunk.section for hit in r.answer.sources
                ],
                "latencyMs": round(r.latency_ms),
            }
            for r in results
        ],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    path.with_suffix(".md").write_text(format_answer_markdown(label, config, results, summary))
    return path


def format_answer_markdown(
    label: str,
    config: Mapping[str, object],
    results: Sequence[AnswerResult],
    summary: AnswerSummary,
) -> str:
    """One readable page: the scores, then every question with its answer and verdict."""
    numbers = f"{summary.key_numbers_found:.0%}" if summary.key_numbers_found is not None else "n/a"
    lines = [
        f"# Answer evaluation: {label}",
        "",
        "Settings: " + ", ".join(f"{key} = {value}" for key, value in config.items()),
        "",
        "| Measure | Result |",
        "|---|---|",
        f"| Answerable questions graded | {summary.answerable} |",
        f"| Correct | {summary.correct:.0%} |",
        f"| Partial | {summary.partial:.0%} |",
        f"| Incorrect | {summary.incorrect:.0%} |",
        f"| Unanswerable handled correctly | {summary.abstention_correct:.0%} |",
        f"| Faithful to sources | {summary.faithful:.0%} |",
        f"| Valid citations | {summary.citations_valid:.0%} |",
        f"| Key numbers found (no LLM) | {numbers} |",
        f"| Not correct: retrieval / generation | {summary.retrieval_failures} / "
        f"{summary.generation_failures} |",
        f"| Errors | {summary.errors} |",
        "",
        "Verdicts come from an LLM judge (the answer model itself, the only working model), so",
        "they may be lenient; the key-number check does not use an LLM.",
        "",
    ]
    for r in results:
        verdict = r.verdict.correctness.upper() if r.verdict else "ERROR"
        lines += [
            f"## {r.case.id} · {verdict} · {r.case.category} · {r.case.split} split",
            "",
            f"**Question:** {r.case.question}",
            "",
            "**Expected:** " + "; ".join(r.case.expected_facts),
            "",
            "**Answer** (" + (r.answer_model or "no model") + "):",
            "",
            "> " + (r.answer.text or r.error or "(empty)").replace("\n", "\n> "),
            "",
        ]
        if r.verdict:
            faithful = "yes" if r.verdict.faithful else "no"
            lines.append(
                f"**Judge:** {r.verdict.correctness}; faithful: {faithful}; {r.verdict.explanation}"
            )
        checks = [
            f"citations {'ok' if r.citations_valid else 'invalid'}",
            f"evidence in context: {'yes' if r.evidence_in_context else 'no'}",
        ]
        if r.key_numbers_found is not None:
            checks.append(f"key numbers found: {r.key_numbers_found:.0%}")
        if r.failure_cause:
            checks.append(f"failure cause: {r.failure_cause}")
        lines += ["", "**Checks:** " + ", ".join(checks), "", "**Sources:**", ""]
        lines += [
            f"{n}. {hit.chunk.section or '(no section)'}, {format_pages(hit)}"
            for n, hit in enumerate(r.answer.sources, start=1)
        ]
        lines.append("")
    return "\n".join(lines)
