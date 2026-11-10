from typing import Any

import pytest
from flask.testing import FlaskClient

from rag_system.answering import AnswerService
from rag_system.api.app import create_app
from rag_system.domain import Chunk
from rag_system.retrieval import DenseRetriever
from tests.fakes import FakeChatModel, FakeEmbedder, InMemoryVectorStore


def _client(model: FakeChatModel) -> FlaskClient:
    store = InMemoryVectorStore()
    chunk = Chunk("report#00000", "report", "report.pdf", 0, "Revenue was 12 million", 3, 4)
    store.upsert([chunk], FakeEmbedder().embed_documents([chunk.text]))
    return create_app(
        AnswerService(DenseRetriever(FakeEmbedder(), store), model), default_top_k=5
    ).test_client()


def test_ask_returns_the_answer_and_numbered_sources() -> None:
    response = _client(FakeChatModel(reply="Revenue was $12 million [1].")).post(
        "/api/ask", json={"question": "What was revenue?"}
    )

    assert response.status_code == 200
    body: dict[str, Any] = response.get_json()
    assert body["answer"] == "Revenue was $12 million [1]."
    assert body["modelId"] == "fake-model"
    source = body["sources"][0]
    assert source["number"] == 1
    assert source["documentName"] == "report.pdf"
    assert source["pagesLabel"] == "pages 3-4"
    assert source["section"] == ""


@pytest.mark.parametrize(
    "payload",
    [{}, {"question": ""}, {"question": "ok", "topK": 0}, {"question": "ok", "extra": 1}],
)
def test_invalid_requests_get_a_400_with_details(payload: dict[str, Any]) -> None:
    response = _client(FakeChatModel()).post("/api/ask", json=payload)

    assert response.status_code == 400
    error = response.get_json()["error"]
    assert error["code"] == "invalid_request"
    assert error["details"]


def test_a_non_json_body_is_rejected() -> None:
    response = _client(FakeChatModel()).post("/api/ask", data="question", content_type="text/plain")

    assert response.status_code == 400


def test_model_failure_returns_502_without_internal_details() -> None:
    response = _client(FakeChatModel(fail=True)).post("/api/ask", json={"question": "Revenue?"})

    assert response.status_code == 502
    error = response.get_json()["error"]
    assert error["code"] == "upstream_unavailable"
    assert "model down" not in error["message"]


def test_unknown_routes_return_json_errors() -> None:
    response = _client(FakeChatModel()).get("/api/nope")

    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "not_found"


def test_health() -> None:
    assert _client(FakeChatModel()).get("/api/health").get_json() == {"status": "ok"}
