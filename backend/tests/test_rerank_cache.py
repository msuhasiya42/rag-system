from pathlib import Path

from rag_system.domain import Chunk, SearchHit
from rag_system.rerank_cache import CachingReranker
from tests.fakes import FakeReranker


def _hits(*texts: str) -> list[SearchHit]:
    return [
        SearchHit(Chunk(f"d#{i}", "d", "d.pdf", i, text, 1, 1), 0.0) for i, text in enumerate(texts)
    ]


def test_a_repeated_request_is_served_from_disk(tmp_path: Path) -> None:
    path = tmp_path / "rerank.json"
    inner = FakeReranker()
    candidates = _hits("noise", "senior notes due 2027", "other")

    first = CachingReranker(inner, "bge", path).rerank("senior notes", candidates, 2)
    again = CachingReranker(inner, "bge", path)  # a new run, reading the saved file
    second = again.rerank("senior notes", candidates, 2)

    assert [hit.chunk.id for hit in first] == [hit.chunk.id for hit in second] == ["d#1", "d#0"]
    assert len(inner.calls) == 1  # the second run made no request
    assert (again.hits, again.misses) == (1, 0)


def test_any_change_in_candidates_or_model_is_a_miss(tmp_path: Path) -> None:
    path = tmp_path / "rerank.json"
    inner = FakeReranker()
    CachingReranker(inner, "bge", path).rerank("q", _hits("a", "b"), 2)

    CachingReranker(inner, "bge", path).rerank("q", _hits("a", "b changed"), 2)
    CachingReranker(inner, "cohere", path).rerank("q", _hits("a", "b"), 2)

    assert len(inner.calls) == 3


def test_the_whole_ranking_is_cached_so_any_top_n_is_served(tmp_path: Path) -> None:
    path = tmp_path / "rerank.json"
    inner = FakeReranker()
    candidates = _hits("x", "y", "z")
    CachingReranker(inner, "bge", path).rerank("q", candidates, 1)

    three = CachingReranker(inner, "bge", path).rerank("q", candidates, 3)

    assert len(three) == 3
    assert inner.calls == [("q", 3, 3)]  # asked once, for every candidate
