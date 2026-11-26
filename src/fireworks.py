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
from dataclasses import dataclass
from typing import Any

API_URL = "https://api.fireworks.ai/inference/v1/chat/completions"
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})


class FireworksError(RuntimeError):
    """The Fireworks API returned an error, or could not be reached."""


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.prompt_tokens + other.prompt_tokens,
            self.completion_tokens + other.completion_tokens,
            self.cached_tokens + other.cached_tokens,
        )


@dataclass(frozen=True)
class Completion:
    content: str
    finish_reason: str
    usage: Usage
    latency_s: float


def json_schema_format(name: str, schema: dict[str, Any]) -> dict[str, Any]:
    """`response_format` value that constrains the reply to a JSON schema."""
    return {"type": "json_schema", "json_schema": {"name": name, "schema": schema}}


def chat(
    messages: list[dict[str, str]],
    *,
    model: str,
    api_key: str,
    response_format: dict[str, Any] | None = None,
    reasoning_effort: str | None = None,
    max_tokens: int = 2048,
    temperature: float = 0.0,
    timeout_s: float = 60.0,
    retries: int = 2,
) -> Completion:
    """Send one chat-completions request and return the first choice."""
    body = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "response_format": response_format,
        "reasoning_effort": reasoning_effort,
    }
    payload = json.dumps({k: v for k, v in body.items() if v is not None}).encode()
    start = time.perf_counter()
    data = _post_with_retries(payload, api_key, timeout_s, retries)
    return _parse_completion(data, latency_s=time.perf_counter() - start)


def _post_with_retries(payload: bytes, api_key: str, timeout_s: float, retries: int) -> dict[str, Any]:
    for attempt in range(retries + 1):
        last_attempt = attempt == retries
        try:
            return _post(payload, api_key, timeout_s)
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


def _post(payload: bytes, api_key: str, timeout_s: float) -> dict[str, Any]:
    request = urllib.request.Request(
        API_URL,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        return json.load(response)


def _parse_completion(data: dict[str, Any], latency_s: float) -> Completion:
    try:
        choice = data["choices"][0]
    except (KeyError, IndexError) as exc:
        raise FireworksError(f"Unexpected response shape: {str(data)[:300]}") from exc
    usage = data.get("usage") or {}
    details = usage.get("prompt_tokens_details") or {}
    return Completion(
        content=choice.get("message", {}).get("content") or "",
        finish_reason=choice.get("finish_reason") or "",
        usage=Usage(
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            cached_tokens=details.get("cached_tokens", 0) or 0,
        ),
        latency_s=latency_s,
    )
