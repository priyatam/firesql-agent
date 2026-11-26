"""Minimal Fireworks AI chat-completions client, standard library only.

Fireworks serves an OpenAI-compatible REST API, so one chat call is one JSON POST to
https://api.fireworks.ai/inference/v1/chat/completions. This is the only module that
talks to the network.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

API_URL = "https://api.fireworks.ai/inference/v1/chat/completions"
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})


class FireworksError(RuntimeError):
    """The Fireworks API returned an error, or could not be reached."""


class Message(BaseModel):
    """One chat message."""

    model_config = ConfigDict(frozen=True)
    role: Literal["system", "user", "assistant"]
    content: str


class Usage(BaseModel):
    model_config = ConfigDict(frozen=True)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            cached_tokens=self.cached_tokens + other.cached_tokens,
        )


class Completion(BaseModel):
    model_config = ConfigDict(frozen=True)
    content: str
    finish_reason: str
    usage: Usage
    latency_s: float


def json_schema_format(name: str, schema: dict[str, Any]) -> dict[str, Any]:
    """`response_format` value that constrains the reply to a JSON schema."""
    return {"type": "json_schema", "json_schema": {"name": name, "schema": schema}}


def chat(
    messages: Sequence[Message],
    *,
    model: str,
    api_key: str,
    response_format: dict[str, Any] | None = None,
    reasoning_effort: str | None = None,
    max_tokens: int = 2048,
    temperature: float = 0.0,
    timeout_s: float = 60.0,
    retries: int = 2,
    session_id: str | None = None,
) -> Completion:
    """Send one chat-completions request and return the first choice.

    `session_id` routes requests that share a prompt prefix to the same replica, so the prompt cache hits.
    """
    body = {
        "model": model,
        "messages": [m.model_dump() for m in messages],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "response_format": response_format,
        "reasoning_effort": reasoning_effort,
    }
    payload = json.dumps({k: v for k, v in body.items() if v is not None}).encode()
    start = time.perf_counter()
    data = _post_with_retries(payload, api_key, timeout_s, retries, session_id)
    return _parse_completion(data, latency_s=time.perf_counter() - start)


def _post_with_retries(payload: bytes, api_key: str, timeout_s: float, retries: int, session_id: str | None) -> dict[str, Any]:
    for attempt in range(retries + 1):
        last_attempt = attempt == retries
        try:
            return _post(payload, api_key, timeout_s, session_id)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            if exc.code in RETRYABLE_STATUS and not last_attempt:
                time.sleep(0.5 * 2**attempt)
                continue
            raise FireworksError(f"Fireworks returned HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            if not last_attempt:
                time.sleep(0.5 * 2**attempt)
                continue
            raise FireworksError(f"Could not reach Fireworks: {exc}") from exc
    raise AssertionError("unreachable")


def _post(payload: bytes, api_key: str, timeout_s: float, session_id: str | None) -> dict[str, Any]:
    request = urllib.request.Request(
        API_URL,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            **({"x-session-affinity": session_id} if session_id else {}),
        },
    )
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        return json.load(response)


class _ApiMessage(BaseModel):
    content: str | None = None


class _ApiChoice(BaseModel):
    message: _ApiMessage = _ApiMessage()
    finish_reason: str | None = None


class _ApiPromptDetails(BaseModel):
    cached_tokens: int | None = 0


class _ApiUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    prompt_tokens_details: _ApiPromptDetails | None = None


class _ApiResponse(BaseModel):
    """The parts of a chat-completions response we use."""

    choices: list[_ApiChoice]
    usage: _ApiUsage | None = None


def _parse_completion(data: dict[str, Any], latency_s: float) -> Completion:
    try:
        response = _ApiResponse.model_validate(data)
        choice = response.choices[0]
    except (ValidationError, IndexError) as exc:
        raise FireworksError(f"Unexpected response shape: {str(data)[:300]}") from exc
    usage = response.usage or _ApiUsage()
    details = usage.prompt_tokens_details or _ApiPromptDetails()
    return Completion(
        content=choice.message.content or "",
        finish_reason=choice.finish_reason or "",
        usage=Usage(
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            cached_tokens=details.cached_tokens or 0,
        ),
        latency_s=latency_s,
    )
