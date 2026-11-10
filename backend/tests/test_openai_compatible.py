"""The OpenAI-compatible chat adapter against a stubbed HTTP transport."""

import json

import httpx
import pytest

from rag_system.clients.openai_compatible import OpenAICompatibleChatModel
from rag_system.ports import UpstreamServiceError


def _model(
    responses: dict[str, httpx.Response], seen: list[dict[str, object]]
) -> OpenAICompatibleChatModel:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append({**body, "auth": request.headers["authorization"], "url": str(request.url)})
        return responses[body["model"]]

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return OpenAICompatibleChatModel(
        client, "https://api.example/v1/", "secret", ["fast", "backup"], 300, "low"
    )


def _answer(text: str | None) -> httpx.Response:
    return httpx.Response(
        200, json={"choices": [{"message": {"role": "assistant", "content": text}}]}
    )


def test_sends_system_and_user_messages_and_returns_the_answer() -> None:
    seen: list[dict[str, object]] = []

    reply = _model({"fast": _answer(" Dividends: none since 1967 [1]. ")}, seen).generate(
        "sys", "q"
    )

    assert (reply.text, reply.model_id) == ("Dividends: none since 1967 [1].", "fast")
    sent = seen[0]
    assert sent["url"] == "https://api.example/v1/chat/completions"
    assert sent["auth"] == "Bearer secret"
    assert sent["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "q"},
    ]
    assert (sent["max_tokens"], sent["reasoning_effort"]) == (300, "low")


@pytest.mark.parametrize("status", [404, 429, 503])
def test_falls_back_when_a_model_is_overloaded_limited_or_retired(status: int) -> None:
    seen: list[dict[str, object]] = []
    responses = {"fast": httpx.Response(status, json={}), "backup": _answer("ok")}

    reply = _model(responses, seen).generate("s", "u")

    assert reply.model_id == "backup"
    assert [call["model"] for call in seen] == ["fast", "backup"]


def test_an_empty_answer_counts_as_a_failure() -> None:
    reply = _model({"fast": _answer(None), "backup": _answer("ok")}, []).generate("s", "u")

    assert reply.model_id == "backup"


def test_a_bad_request_is_not_retried_on_other_models() -> None:
    seen: list[dict[str, object]] = []

    with pytest.raises(UpstreamServiceError, match="HTTP 400"):
        _model({"fast": httpx.Response(400, json={})}, seen).generate("s", "u")
    assert len(seen) == 1


def test_raises_when_every_model_fails() -> None:
    responses = {"fast": httpx.Response(503, json={}), "backup": httpx.Response(429, json={})}

    with pytest.raises(UpstreamServiceError, match="All answer models failed"):
        _model(responses, []).generate("s", "u")
