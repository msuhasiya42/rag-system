from collections.abc import Sequence

import pytest

from rag_system.domain import Chunk, SearchHit
from rag_system.retrieval import (
    DenseRetriever,
    HybridRetriever,
    RerankingRetriever,
    Retriever,
    SparseRetriever,
    reciprocal_rank_fusion,
)
from tests.fakes import (
    FakeEmbedder,
    FakeReranker,
    FakeSparseEncoder,
    InMemorySparseStore,
    InMemoryVectorStore,
)


def _chunk(name: str, text: str = "") -> Chunk:
    return Chunk(f"doc#{name}", "doc", "doc.pdf", 0, text or name, 1, 1)


def _hits(*names: str) -> list[SearchHit]:
    return [SearchHit(_chunk(name), 1.0) for name in names]


class ScriptedRetriever(Retriever):
    def __init__(self, *names: str) -> None:
        self.names = names
        self.depths: list[int] = []

    def retrieve(self, question: str, top_k: int) -> list[SearchHit]:
        self.depths.append(top_k)
        return _hits(*self.names)[:top_k]


def _ids(hits: Sequence[SearchHit]) -> list[str]:
    return [hit.chunk.id.split("#")[1] for hit in hits]


def test_rrf_rewards_chunks_ranked_well_by_both_lists() -> None:
    dense = _hits("a", "b", "c")
    sparse = _hits("c", "d", "a")

    fused = reciprocal_rank_fusion([dense, sparse], k=60)

    # a: 1/61 + 1/63, c: 1/63 + 1/61 (tie, a first as seen first), b: 1/62, d: 1/62
    assert _ids(fused) == ["a", "c", "b", "d"]
    assert fused[0].score == pytest.approx(1 / 61 + 1 / 63)


def test_rrf_uses_ranks_not_raw_scores() -> None:
    huge_score = [SearchHit(_chunk("x"), 1000.0)]
    small_scores = [SearchHit(_chunk("y"), 0.1), SearchHit(_chunk("x"), 0.05)]

    fused = reciprocal_rank_fusion([huge_score, small_scores])

    assert _ids(fused) == ["x", "y"]  # x is in both lists, not because of its score of 1000


def test_hybrid_asks_every_retriever_for_its_candidates_and_returns_top_k() -> None:
    first, second = ScriptedRetriever("a", "b"), ScriptedRetriever("b", "c")

    hits = HybridRetriever([first, second], candidates_per_retriever=20).retrieve("q", top_k=2)

    assert _ids(hits) == ["b", "a"]
    assert first.depths == second.depths == [20]


def test_hybrid_finds_an_exact_term_that_dense_search_ranks_low() -> None:
    chunks = [
        _chunk("cash", "Cash and cash equivalents $ 47,719 $ 44,333"),
        _chunk("prose", "We hold substantial liquidity and keep strong reserves at year end"),
        _chunk("other", "Insurance float grew during the year"),
    ]
    embedder, encoder = FakeEmbedder(), FakeSparseEncoder()
    dense_store, sparse_store = InMemoryVectorStore(), InMemorySparseStore()
    texts = [chunk.text for chunk in chunks]
    dense_store.upsert(chunks, embedder.embed_documents(texts))
    sparse_store.upsert(chunks, encoder.encode_documents(texts))
    dense = DenseRetriever(embedder, dense_store)
    sparse = SparseRetriever(encoder, sparse_store)

    hits = HybridRetriever([dense, sparse], 10).retrieve("cash equivalents 47,719", top_k=1)

    assert _ids(hits) == ["cash"]


def test_reranker_reorders_a_wider_candidate_list() -> None:
    inner = ScriptedRetriever("noise one", "noise two", "senior notes due 2027")
    reranker = FakeReranker()

    hits = RerankingRetriever(inner, reranker, candidates=20).retrieve("senior notes due", 1)

    assert _ids(hits) == ["senior notes due 2027"]
    assert inner.depths == [20]  # asked for more than the 1 result it returns
    assert reranker.calls == [("senior notes due", 3, 1)]


def test_reranker_cannot_recover_what_retrieval_missed() -> None:
    inner = ScriptedRetriever("noise one", "noise two")

    hits = RerankingRetriever(inner, FakeReranker(), candidates=20).retrieve("senior notes", 1)

    assert _ids(hits) == ["noise one"]  # the best of what it was given, not the right chunk


def test_weighted_rrf_lets_one_list_count_for_less() -> None:
    dense = _hits("a", "b")
    sparse = _hits("b", "a")

    equal = reciprocal_rank_fusion([dense, sparse])
    dense_trusted = reciprocal_rank_fusion([dense, sparse], weights=[1.0, 0.5])

    assert equal[0].score == pytest.approx(equal[1].score)  # a perfect tie
    assert _ids(dense_trusted) == ["a", "b"]  # dense's order wins
