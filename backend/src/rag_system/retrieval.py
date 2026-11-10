"""Retrieval pipelines built from small, stackable retrievers.

    basic          DenseRetriever                                  meaning only
    hybrid         HybridRetriever(Dense, Sparse)  + RRF            meaning + exact terms
    advanced       RerankingRetriever(Hybrid)                       + cross-encoder reranking
    basic-rerank   RerankingRetriever(Dense)                        isolates the reranker's effect

Every retriever answers ``retrieve(question, top_k)``, so they wrap each other freely.
"""

from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Literal, Protocol, get_args

from rag_system.domain import SearchHit, SparseVector
from rag_system.ports import Embedder, Reranker, SparseEncoder, VectorStore

Pipeline = Literal["basic", "hybrid", "advanced", "basic-rerank"]
PIPELINES: tuple[Pipeline, ...] = get_args(Pipeline)
PIPELINE_DESCRIPTIONS: dict[Pipeline, str] = {
    "basic": "Dense vector search",
    "hybrid": "Dense + sparse, fused with RRF",
    "advanced": "Hybrid + cross-encoder rerank",
    "basic-rerank": "Dense + cross-encoder rerank",
}
USES_SPARSE: frozenset[Pipeline] = frozenset({"hybrid", "advanced"})
USES_RERANKER: frozenset[Pipeline] = frozenset({"advanced", "basic-rerank"})

# Stores may return several table rows of one chunk; fetch extra so that after removing
# repeats there are still top_k distinct chunks.
OVERFETCH = 3

# The usual RRF constant from Cormack et al. (2009): damps the gap between ranks 1 and 2 so one
# list's top result cannot dominate the fusion on its own.
RRF_K = 60


class Retriever(Protocol):
    def retrieve(self, question: str, top_k: int) -> list[SearchHit]: ...


class DenseRetriever(Retriever):
    def __init__(self, embedder: Embedder, store: VectorStore[Sequence[float]]) -> None:
        self._embedder = embedder
        self._store = store

    def retrieve(self, question: str, top_k: int) -> list[SearchHit]:
        hits = self._store.search(self._embedder.embed_query(question), top_k * OVERFETCH)
        return unique_chunks(hits)[:top_k]


class SparseRetriever(Retriever):
    def __init__(self, encoder: SparseEncoder, store: VectorStore[SparseVector]) -> None:
        self._encoder = encoder
        self._store = store

    def retrieve(self, question: str, top_k: int) -> list[SearchHit]:
        hits = self._store.search(self._encoder.encode_query(question), top_k * OVERFETCH)
        return unique_chunks(hits)[:top_k]


class HybridRetriever(Retriever):
    """Runs several retrievers in parallel and fuses their rankings with RRF.

    Dense cosine scores and sparse dot-product scores are on different scales, so they are not
    added; RRF uses only each chunk's rank in each list. ``weights`` scale each list's votes, so
    a noisier retriever can count for less (weighted RRF).
    """

    def __init__(
        self,
        retrievers: Sequence[Retriever],
        candidates_per_retriever: int,
        weights: Sequence[float] | None = None,
        rrf_k: int = RRF_K,
    ) -> None:
        if weights is not None and len(weights) != len(retrievers):
            raise ValueError("one weight per retriever is required")
        self._retrievers = list(retrievers)
        self._candidates = candidates_per_retriever
        self._weights = list(weights) if weights is not None else [1.0] * len(retrievers)
        self._rrf_k = rrf_k

    def retrieve(self, question: str, top_k: int) -> list[SearchHit]:
        depth = max(top_k, self._candidates)
        with ThreadPoolExecutor(max_workers=len(self._retrievers)) as pool:
            rankings = list(pool.map(lambda r: r.retrieve(question, depth), self._retrievers))
        return reciprocal_rank_fusion(rankings, k=self._rrf_k, weights=self._weights)[:top_k]


class RerankingRetriever(Retriever):
    """Fetches a wider candidate list, then lets a cross-encoder reorder it.

    It can only reorder what the inner retriever found: a chunk missing from the candidates
    cannot be recovered.

    With ``blend_weight`` set, the reranker's order is fused with the inner order by weighted
    RRF instead of replacing it. A cross-encoder favours literal word overlap and can bury a
    paraphrase match the inner retriever ranked first; blending keeps that vote.
    """

    def __init__(
        self,
        inner: Retriever,
        reranker: Reranker,
        candidates: int,
        blend_weight: float | None = None,
        rrf_k: int = RRF_K,
    ) -> None:
        self._inner = inner
        self._reranker = reranker
        self._candidates = candidates
        self._blend_weight = blend_weight
        self._rrf_k = rrf_k

    def retrieve(self, question: str, top_k: int) -> list[SearchHit]:
        candidates = self._inner.retrieve(question, max(top_k, self._candidates))
        if self._blend_weight is None:
            return self._reranker.rerank(question, candidates, top_k)
        reranked = self._reranker.rerank(question, candidates, len(candidates))
        weights = [1.0, self._blend_weight]
        return reciprocal_rank_fusion([candidates, reranked], k=self._rrf_k, weights=weights)[
            :top_k
        ]


def unique_chunks(hits: Sequence[SearchHit]) -> list[SearchHit]:
    """Keep each chunk once, at its best rank (small-to-big: a matching row stands for its
    chunk)."""
    seen: set[str] = set()
    unique = []
    for hit in hits:
        if hit.chunk.id not in seen:
            seen.add(hit.chunk.id)
            unique.append(hit)
    return unique


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[SearchHit]],
    k: int = RRF_K,
    weights: Sequence[float] | None = None,
) -> list[SearchHit]:
    """score(chunk) = sum over lists of weight / (k + rank). Chunks ranked well in several lists
    win."""
    weights = weights if weights is not None else [1.0] * len(rankings)
    scores: dict[str, float] = {}
    hits: dict[str, SearchHit] = {}
    for ranking, weight in zip(rankings, weights, strict=True):
        for rank, hit in enumerate(ranking, start=1):
            scores[hit.chunk.id] = scores.get(hit.chunk.id, 0.0) + weight / (k + rank)
            hits.setdefault(hit.chunk.id, hit)
    ordered = sorted(scores, key=lambda chunk_id: scores[chunk_id], reverse=True)
    return [SearchHit(hits[chunk_id].chunk, scores[chunk_id]) for chunk_id in ordered]
