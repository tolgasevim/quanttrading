"""The language-model adapter (PRD §8 style: one small interface, one class per vendor).

The rest of the app talks to `LlmClient` only. `AnthropicLlm` is the real one; tests use a fake.
The SDK is imported on first use, so the app still starts when the package is missing and the AI
page then says "not set up" (503) instead of the whole API failing.

Claude Opus 5.5 facts this file relies on: thinking is always on (so `thinking` is not sent),
effort goes in `output_config`, a forced `tool_choice` is rejected (so tools use `auto` and the
prompt says which tool to call), and the content blocks of a reply go back unchanged in a tool
loop (`raw_content`). The server-side fallback beta lets the API answer with another model when
the first one cannot; the model that answered is in `LlmReply.model`.
"""

import importlib
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LlmError(Exception):
    """Base of every adapter error. `status` is the HTTP status the API answers with."""

    status = 502


class LlmNotConfigured(LlmError):
    status = 503


class LlmAuthError(LlmError):
    """The key was rejected. It is a set-up problem, not the user's."""

    status = 503


class LlmRateLimited(LlmError):
    status = 429


class LlmBadRequest(LlmError):
    status = 502


class LlmUnavailable(LlmError):
    status = 502


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0  # not served from the cache
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class LlmReply:
    text: str
    tool_calls: list[ToolCall]
    stop_reason: str | None
    usage: Usage
    model: str
    request_id: str | None = None
    refused: bool = False
    # The reply's content blocks exactly as received, for the next request of a tool loop.
    raw_content: list[Any] = field(default_factory=list)


class LlmClient(Protocol):
    def send(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> LlmReply: ...


def _count(value: Any) -> int:
    return int(value) if isinstance(value, int | float) else 0


class AnthropicLlm:
    def __init__(
        self,
        *,
        api_key: str | None,
        model: str,
        effort: str,
        max_tokens: int,
        fallbacks: bool,
        timeout: float = 90.0,
    ) -> None:
        if not api_key:
            raise LlmNotConfigured("the AI is not set up: no API key (QT_ANTHROPIC_API_KEY)")
        try:
            self._sdk = importlib.import_module("anthropic")
        except ImportError as exc:
            raise LlmNotConfigured(
                "the AI is not set up: the anthropic package is missing"
            ) from exc
        # One retry, not the default two: a question makes up to four calls, and the page waits.
        self._client = self._sdk.Anthropic(api_key=api_key, timeout=timeout, max_retries=1)
        self._model = model
        self._effort = effort
        self._max_tokens = max_tokens
        self._fallbacks = fallbacks

    def send(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> LlmReply:
        params: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "system": system,
            "messages": messages,
            "tools": tools,
            "tool_choice": {"type": "auto"},
            "output_config": {"effort": self._effort},
        }
        if self._fallbacks:
            params["betas"] = [FALLBACK_BETA]
            params["fallbacks"] = "default"
        try:
            response = self._client.beta.messages.create(**params)
        except Exception as exc:  # mapped below; the key is never part of the message
            raise self._map(exc) from None
        return self._reply(response)

    def _map(self, exc: Exception) -> LlmError:
        sdk = self._sdk
        if isinstance(exc, sdk.AuthenticationError | sdk.PermissionDeniedError):
            return LlmAuthError("the AI provider rejected the API key")
        if isinstance(exc, sdk.RateLimitError):
            return LlmRateLimited("the AI provider is rate limiting requests, try again soon")
        if isinstance(exc, sdk.BadRequestError | sdk.NotFoundError):
            return LlmBadRequest("the AI provider rejected the request")
        if isinstance(exc, sdk.APIConnectionError | sdk.APIStatusError):
            return LlmUnavailable("the AI provider is not reachable right now")
        # An unexpected error, e.g. a TypeError from a request field this SDK version does not
        # know. The type is logged so it can be found; the message might hold request data.
        log.warning("AI request failed: %s", type(exc).__name__)
        return LlmError("the AI request failed")

    def _reply(self, response: Any) -> LlmReply:
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in response.content:
            kind = getattr(block, "type", None)
            if kind == "text":
                text_parts.append(block.text)
            elif kind == "tool_use":
                calls.append(ToolCall(block.id, block.name, dict(block.input)))
        usage = response.usage
        details = getattr(response, "stop_details", None)
        refused = response.stop_reason == "refusal" or bool(
            details and getattr(details, "category", None)
        )
        return LlmReply(
            text="\n".join(text_parts).strip(),
            tool_calls=calls,
            stop_reason=response.stop_reason,
            usage=Usage(
                _count(usage.input_tokens),
                _count(usage.output_tokens),
                _count(getattr(usage, "cache_read_input_tokens", 0)),
                _count(getattr(usage, "cache_creation_input_tokens", 0)),
            ),
            model=str(getattr(response, "model", self._model)),
            request_id=getattr(response, "_request_id", None),
            refused=refused,
            raw_content=list(response.content),
        )


_shared: dict[tuple[Any, ...], LlmClient] = {}


def default_client() -> LlmClient:
    """One client per setting, reused: each holds a connection pool."""
    from quant.config import get_settings

    s = get_settings()
    key = (s.anthropic_api_key, s.llm_model, s.llm_effort, s.llm_max_tokens, s.llm_fallbacks)
    if key not in _shared:
        _shared[key] = AnthropicLlm(
            api_key=s.anthropic_api_key,
            model=s.llm_model,
            effort=s.llm_effort,
            max_tokens=s.llm_max_tokens,
            fallbacks=s.llm_fallbacks,
        )
    return _shared[key]
