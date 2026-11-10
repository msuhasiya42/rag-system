"""Retrieval evaluation against a golden dataset.

Relevance is judged at the level of evidence items (a page range, optionally narrowed to a
section), not chunk ids, so one golden set can compare chunking strategies.

Metrics, for the top K retrieved chunks of one question:
- Hit@K: at least one chunk supports some evidence item.
- Recall@K: share of evidence items supported by at least one chunk. ("Did we find all of it?")
- Precision@K: share of the K chunks that support any evidence item. ("How clean is it?")
- MRR: 1 / rank of the first relevant chunk, 0 if none in the retrieved list.
- nDCG@K: rewards finding each evidence item early. A chunk gains 1 / log2(rank + 1) only when it
  supports an item not seen higher up; the ideal ranking finds every item in the first places.
Unanswerable questions have no evidence and are left out of these averages.
"""

import math
import statistics
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from rag_system.domain import Chunk, SearchHit


class Location(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pages: list[int] = Field(min_length=1)
    section: str | None = None

    def contains(self, chunk: Chunk) -> bool:
        overlaps = chunk.page_start <= max(self.pages) and min(self.pages) <= chunk.page_end
        in_section = self.section is None or self.section.lower() in chunk.section.lower()
        return overlaps and in_section


class Evidence(Location):
    """One fact the answer needs. Annual reports often repeat a fact (CEO letter and 10-K);
    ``alternatives`` lists the other places, and a chunk from any of them supports the fact."""

    alternatives: list[Location] = []

    def supported_by(self, chunk: Chunk) -> bool:
        return self.contains(chunk) or any(place.contains(chunk) for place in self.alternatives)


Split = Literal["tune", "test"]


class GoldenCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    # "tune" questions may be used to choose settings; "test" questions are held out to check
    # that a tuned setting generalises instead of fitting the questions it was tuned on.
    split: Split
    category: str
    question: str
    answerable: bool
    evidence: list[Evidence]
    expected_facts: list[str]


class GoldenSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document: str
    notes: str = ""
    cases: list[GoldenCase]


def load_golden_set(path: Path) -> GoldenSet:
    return GoldenSet.model_validate_json(path.read_text())


@dataclass(frozen=True)
class CaseResult:
    case: GoldenCase
    hits: list[SearchHit]
    latency_ms: float

    def relevant(self, rank: int) -> bool:
        chunk = self.hits[rank - 1].chunk
        return any(item.supported_by(chunk) for item in self.case.evidence)

    def first_relevant_rank(self) -> int | None:
        return next((r for r in range(1, len(self.hits) + 1) if self.relevant(r)), None)

    def hit_at(self, k: int) -> float:
        rank = self.first_relevant_rank()
        return 1.0 if rank is not None and rank <= k else 0.0

    def recall_at(self, k: int) -> float:
        found = sum(
            any(item.supported_by(hit.chunk) for hit in self.hits[:k])
            for item in self.case.evidence
        )
        return found / len(self.case.evidence)

    def precision_at(self, k: int) -> float:
        return sum(self.relevant(rank) for rank in range(1, min(k, len(self.hits)) + 1)) / k

    def reciprocal_rank(self) -> float:
        rank = self.first_relevant_rank()
        return 1.0 / rank if rank else 0.0

    def ndcg_at(self, k: int) -> float:
        seen: set[int] = set()
        dcg = 0.0
        for rank, hit in enumerate(self.hits[:k], start=1):
            new = {
                index
                for index, item in enumerate(self.case.evidence)
                if index not in seen and item.supported_by(hit.chunk)
            }
            if new:
                seen |= new
                dcg += 1 / math.log2(rank + 1)
        ideal = sum(
            1 / math.log2(rank + 1) for rank in range(1, min(k, len(self.case.evidence)) + 1)
        )
        return dcg / ideal


Retrieve = Callable[[str, int], list[SearchHit]]


def run_retrieval_eval(retrieve: Retrieve, cases: Sequence[GoldenCase], k: int) -> list[CaseResult]:
    results = []
    for case in cases:
        started = time.perf_counter()
        hits = retrieve(case.question, k)
        results.append(CaseResult(case, hits, (time.perf_counter() - started) * 1000))
    return results


@dataclass(frozen=True)
class Summary:
    questions: int
    hit: dict[int, float]
    recall: dict[int, float]
    precision: dict[int, float]
    ndcg: dict[int, float]
    mrr: float
    latency_p50_ms: float
    latency_p95_ms: float


def summarize(results: Sequence[CaseResult], ks: Sequence[int]) -> Summary:
    scored = [result for result in results if result.case.evidence]
    if not scored:
        raise ValueError("No answerable questions with evidence to score")
    latencies = sorted(result.latency_ms for result in results)

    def mean_at_each_k(metric: Callable[[CaseResult, int], float]) -> dict[int, float]:
        return {k: statistics.fmean(metric(result, k) for result in scored) for k in ks}

    return Summary(
        questions=len(scored),
        hit=mean_at_each_k(CaseResult.hit_at),
        recall=mean_at_each_k(CaseResult.recall_at),
        precision=mean_at_each_k(CaseResult.precision_at),
        ndcg=mean_at_each_k(CaseResult.ndcg_at),
        mrr=statistics.fmean(result.reciprocal_rank() for result in scored),
        latency_p50_ms=_percentile(latencies, 50),
        latency_p95_ms=_percentile(latencies, 95),
    )


def _percentile(sorted_values: Sequence[float], percent: float) -> float:
    """Nearest-rank percentile: simple and exact enough for a few dozen samples."""
    rank = max(1, math.ceil(percent / 100 * len(sorted_values)))
    return sorted_values[rank - 1]
