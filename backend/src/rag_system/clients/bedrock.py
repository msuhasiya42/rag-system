"""Amazon Bedrock: Titan text embeddings and chat answers through the Converse API."""

import json
import logging
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

from botocore.exceptions import BotoCoreError, ClientError

if TYPE_CHECKING:  # stubs are a dev dependency
    from mypy_boto3_bedrock_runtime import BedrockRuntimeClient

from rag_system.ports import ChatModel, ChatReply, Embedder, UpstreamServiceError

logger = logging.getLogger(__name__)

# Errors after which the next model in the list is worth trying. Others (for example a
# ValidationException for a malformed request) would fail the same way on every model.
FALLBACK_ERROR_CODES = frozenset(
    {
        "ThrottlingException",
        "ServiceUnavailableException",
        "ModelNotReadyException",
        "ModelTimeoutException",
        "InternalServerException",
        "AccessDeniedException",  # model not enabled for this account
        "ResourceNotFoundException",  # model not offered in this region
    }
)


class TitanEmbedder(Embedder):
    """Titan Text Embeddings V2 takes one text per call, so calls run concurrently."""

    def __init__(
        self, client: "BedrockRuntimeClient", model_id: str, dimensions: int, concurrency: int
    ) -> None:
        self._client = client
        self._model_id = model_id
        self._dimensions = dimensions
        self._concurrency = concurrency

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if len(texts) <= 1:
            return [self._embed_one(text) for text in texts]
        with ThreadPoolExecutor(max_workers=self._concurrency) as pool:
            return list(pool.map(self._embed_one, texts))

    def embed_query(self, text: str) -> list[float]:
        return self._embed_one(text)  # Titan embeds queries and documents the same way

    def _embed_one(self, text: str) -> list[float]:
        body = json.dumps({"inputText": text, "dimensions": self._dimensions, "normalize": True})
        try:
            response = self._client.invoke_model(
                modelId=self._model_id,
                body=body,
                contentType="application/json",
                accept="application/json",
            )
        except (ClientError, BotoCoreError) as error:
            raise UpstreamServiceError(f"Embedding failed: {error}") from error
        embedding: list[float] = json.loads(response["body"].read())["embedding"]
        return embedding


class BedrockChatModel(ChatModel):
    def __init__(
        self, client: "BedrockRuntimeClient", model_ids: list[str], max_tokens: int
    ) -> None:
        if not model_ids:
            raise ValueError("At least one answer model id is required")
        self._client = client
        self._model_ids = model_ids
        self._max_tokens = max_tokens

    def generate(self, system_prompt: str, user_prompt: str) -> ChatReply:
        failures: list[str] = []
        for model_id in self._model_ids:
            try:
                response = self._client.converse(
                    modelId=model_id,
                    system=[{"text": system_prompt}],
                    messages=[{"role": "user", "content": [{"text": user_prompt}]}],
                    inferenceConfig={"maxTokens": self._max_tokens, "temperature": 0.1},
                )
            except ClientError as error:
                code = error.response.get("Error", {}).get("Code", "")
                if code not in FALLBACK_ERROR_CODES:
                    raise UpstreamServiceError(f"{model_id} failed: {code}") from error
                logger.warning("Model %s unavailable (%s); trying the next one", model_id, code)
                failures.append(f"{model_id}: {code}")
                continue
            except BotoCoreError as error:  # timeouts, connection errors
                logger.warning("Model %s unreachable (%s); trying the next one", model_id, error)
                failures.append(f"{model_id}: {type(error).__name__}")
                continue

            # Some models return reasoning blocks before the answer; keep only text blocks.
            content = response["output"].get("message", {}).get("content", [])
            text = "".join(block.get("text", "") for block in content).strip()
            return ChatReply(text=text, model_id=model_id)

        raise UpstreamServiceError("All answer models failed: " + "; ".join(failures))
