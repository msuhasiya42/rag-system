"""The Bedrock adapters against a stub client: response parsing and model fallback."""

import io
import json
from typing import Any

import pytest
from botocore.exceptions import ClientError

from rag_system.clients.bedrock import BedrockChatModel, TitanEmbedder
from rag_system.ports import UpstreamServiceError


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "Converse")


class StubBedrock:
    def __init__(self, failures: dict[str, str] | None = None) -> None:
        self.failures = failures or {}
        self.converse_calls: list[str] = []
        self.embedded: list[dict[str, Any]] = []

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        model_id = kwargs["modelId"]
        self.converse_calls.append(model_id)
        if model_id in self.failures:
            raise _client_error(self.failures[model_id])
        content = [{"reasoningContent": {"reasoningText": {"text": "hidden"}}}, {"text": " Hi "}]
        return {"output": {"message": {"role": "assistant", "content": content}}}

    def invoke_model(self, **kwargs: Any) -> dict[str, Any]:
        request = json.loads(kwargs["body"])
        self.embedded.append(request)
        vector = [float(len(request["inputText"]))] * request["dimensions"]
        return {"body": io.BytesIO(json.dumps({"embedding": vector}).encode())}


def test_chat_returns_only_text_blocks() -> None:
    reply = BedrockChatModel(StubBedrock(), ["model-a"], 100).generate("sys", "user")  # type: ignore[arg-type]

    assert (reply.text, reply.model_id) == ("Hi", "model-a")


def test_chat_falls_back_when_a_model_is_throttled() -> None:
    stub = StubBedrock(failures={"model-a": "ThrottlingException"})

    reply = BedrockChatModel(stub, ["model-a", "model-b"], 100).generate("s", "u")  # type: ignore[arg-type]

    assert reply.model_id == "model-b"
    assert stub.converse_calls == ["model-a", "model-b"]


def test_chat_does_not_fall_back_on_a_bad_request() -> None:
    stub = StubBedrock(failures={"model-a": "ValidationException"})
    model = BedrockChatModel(stub, ["model-a", "model-b"], 100)  # type: ignore[arg-type]

    with pytest.raises(UpstreamServiceError):
        model.generate("s", "u")
    assert stub.converse_calls == ["model-a"]


def test_chat_raises_when_every_model_fails() -> None:
    stub = StubBedrock(failures={"a": "ThrottlingException", "b": "AccessDeniedException"})

    with pytest.raises(UpstreamServiceError, match="All answer models failed"):
        BedrockChatModel(stub, ["a", "b"], 100).generate("s", "u")  # type: ignore[arg-type]


def test_embeddings_keep_input_order_and_request_the_configured_size() -> None:
    stub = StubBedrock()
    embedder = TitanEmbedder(stub, "titan", dimensions=4, concurrency=4)  # type: ignore[arg-type]

    vectors = embedder.embed_documents(["a", "bbb", "cc"])

    assert [vector[0] for vector in vectors] == [1.0, 3.0, 2.0]
    assert all(request["dimensions"] == 4 and request["normalize"] for request in stub.embedded)
