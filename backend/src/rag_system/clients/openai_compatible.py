"""Chat answers from any provider with an OpenAI-compatible chat completions endpoint.

Gemini, Groq, GitHub Models, Cerebras, Mistral and OpenRouter all expose one, so a provider
change is configuration: base URL, API key and model ids.
"""

import logging

import httpx

from rag_system.ports import ChatModel, ChatReply, UpstreamServiceError

logger = logging.getLogger(__name__)

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"

# Worth trying the next model: overloaded (503), rate or quota limited (429), model retired or
# not offered to this key (404), or a server error.
FALLBACK_STATUS_CODES = frozenset({404, 429, 500, 502, 503, 504})


class OpenAICompatibleChatModel(ChatModel):
    def __init__(
        self,
        client: httpx.Client,
        base_url: str,
        api_key: str,
        model_ids: list[str],
        max_tokens: int,
        reasoning_effort: str | None = None,
    ) -> None:
        if not model_ids:
            raise ValueError("At least one answer model id is required")
        self._client = client
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._model_ids = model_ids
        self._max_tokens = max_tokens
        # Thinking models (Gemini 3.x) spend part of max_tokens reasoning; "low" keeps answers fast.
        self._reasoning_effort = reasoning_effort

    def generate(self, system_prompt: str, user_prompt: str) -> ChatReply:
        failures: list[str] = []
        for model_id in self._model_ids:
            body: dict[str, object] = {
                "model": model_id,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "max_tokens": self._max_tokens,
                "temperature": 0.1,
            }
            if self._reasoning_effort:
                body["reasoning_effort"] = self._reasoning_effort
            try:
                response = self._client.post(self._url, headers=self._headers, json=body)
            except httpx.HTTPError as error:  # timeouts, connection errors
                logger.warning("Model %s unreachable (%s); trying the next one", model_id, error)
                failures.append(f"{model_id}: {type(error).__name__}")
                continue
            if response.status_code in FALLBACK_STATUS_CODES:
                logger.warning(
                    "Model %s returned %d; trying the next one", model_id, response.status_code
                )
                failures.append(f"{model_id}: HTTP {response.status_code}")
                continue
            if response.status_code != 200:
                raise UpstreamServiceError(f"{model_id} failed: HTTP {response.status_code}")
            text = _message_text(response.json())
            if not text:  # e.g. the whole token budget went to reasoning
                failures.append(f"{model_id}: empty answer")
                continue
            return ChatReply(text=text, model_id=model_id)
        raise UpstreamServiceError("All answer models failed: " + "; ".join(failures))


def _message_text(payload: object) -> str:
    """choices[0].message.content, or "" when the response does not have that shape."""
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return ""
    message = choices[0].get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return content.strip() if isinstance(content, str) else ""
