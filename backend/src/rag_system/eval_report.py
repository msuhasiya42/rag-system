"""Print and save retrieval evaluation results."""

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from rag_system.answering import format_pages
from rag_system.domain import SearchHit
from rag_system.evaluation import CaseResult, Summary

SECTION_TAIL_CHARS = 45


def format_report(results: Sequence[CaseResult], summary: Summary, ks: Sequence[int]) -> str:
    rows = [f"{'id':4} {'category':13} {'first':>5} {'R@5':>5}  top result"]
    for result in results:
        top = _describe(result.hits[0]) if result.hits else "-"
        if not result.case.evidence:  # unanswerable: show how confident the top result looks
            rows.append(f"{result.case.id:4} {result.case.category:13} {'-':>5} {'-':>5}  {top}")
            continue
        rank = result.first_relevant_rank()
        rows.append(
            f"{result.case.id:4} {result.case.category:13} {rank or 'miss':>5} "
            f"{result.recall_at(5):>5.0%}  {top}"
        )
    metrics = [
        f"Questions scored: {summary.questions} (unanswerable ones excluded)",
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
