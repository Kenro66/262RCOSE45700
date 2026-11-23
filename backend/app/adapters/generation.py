"""Synchronous generation using the agreed temporary OpenAI-compatible API."""

from dataclasses import dataclass
from math import isfinite
from typing import Any
from urllib.parse import urlsplit

import httpx

from .config import generation_settings, required, settings


class GenerationError(RuntimeError):
    """Generation configuration, transport or response failed."""


@dataclass(frozen=True)
class GenerationResult:
    text: str
    tool_calls: list[dict[str, Any]]


def _generation_parameters(response_format, max_tokens, temperature) -> dict[str, Any]:
    if response_format is not None and not isinstance(response_format, dict):
        raise ValueError("response_format must be an object")
    if max_tokens is not None and (type(max_tokens) is not int or max_tokens <= 0):
        raise ValueError("max_tokens must be a positive integer")
    if temperature is not None and (isinstance(temperature, bool) or not isinstance(temperature, (int, float))
                                    or not isfinite(temperature) or not 0 <= temperature <= 2):
        raise ValueError("temperature must be finite and between 0 and 2")
    payload: dict[str, Any] = {}
    if response_format is not None:
        payload["response_format"] = response_format
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if temperature is not None:
        payload["temperature"] = temperature
    return payload


def _configured_parameters(config, response_format, max_tokens, temperature) -> dict[str, Any]:
    maximum = config.get("GENERATION_MAX_TOKENS", "").strip()
    temp = config.get("GENERATION_TEMPERATURE", "").strip()
    if max_tokens is None and maximum:
        max_tokens = int(maximum)
    if temperature is None and temp:
        temperature = float(temp)
    return _generation_parameters(response_format, max_tokens, temperature)


def generate(messages: str | list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, *,
             response_format: dict[str, Any] | None = None, max_tokens: int | None = None,
             temperature: float | None = None, provider_config: dict[str, str] | None = None) -> GenerationResult:
    """Return text and/or tool calls; executing tools is the caller's responsibility."""
    if isinstance(messages, str):
        if not messages.strip():
            raise ValueError("Question must not be blank")
        messages = [{"role": "user", "content": messages}]
    if not isinstance(messages, list) or not messages or any(not isinstance(m, dict) or not isinstance(m.get("role"), str) for m in messages):
        raise ValueError("messages must be a non-empty list of chat messages")
    if tools is not None and (not isinstance(tools, list) or any(not isinstance(tool, dict) for tool in tools)):
        raise ValueError("tools must be a list of tool definitions")
    _generation_parameters(response_format, max_tokens, temperature)
    try:
        config = generation_settings(settings() if provider_config is None else provider_config)
        parameters = _configured_parameters(config, response_format, max_tokens, temperature)
        model = required(config, "GENERATION_MODEL")
        base = required(config, "GENERATION_BASE_URL").rstrip("/")
        parsed = urlsplit(base)
        _ = parsed.port  # Validate malformed or out-of-range ports before sending a request.
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("GENERATION_BASE_URL must be an HTTP(S) API root without credentials or query")
        timeout = float(config.get("GENERATION_TIMEOUT_SECONDS", "60"))
        if not isfinite(timeout) or timeout <= 0:
            raise ValueError("GENERATION_TIMEOUT_SECONDS must be positive and finite")
    except ValueError as exc:
        raise GenerationError(str(exc)) from None
    payload: dict[str, Any] = {"model": model, "messages": messages}
    if tools is not None:
        payload["tools"] = tools
    payload.update(parameters)
    headers = {}
    key = config.get("GENERATION_API_KEY", "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout, connect=min(timeout, 10)), follow_redirects=False) as client:
            response = client.post(f"{base}/chat/completions", json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
    except httpx.TimeoutException:
        raise GenerationError("Generation request timed out; check GENERATION_BASE_URL and service load") from None
    except httpx.HTTPStatusError as exc:
        raise GenerationError(f"Generation service returned HTTP {exc.response.status_code}; check model, address and credentials") from None
    except httpx.RequestError:
        raise GenerationError("Generation connection failed; check GENERATION_BASE_URL") from None
    except (ValueError, TypeError):
        raise GenerationError("Generation service returned invalid JSON, or request is not JSON serializable") from None
    try:
        message = data["choices"][0]["message"]
        text = message.get("content")
        text = "" if text is None else text
        calls = message.get("tool_calls")
        calls = [] if calls is None else calls
        if not isinstance(text, str) or not isinstance(calls, list):
            raise ValueError
        for call in calls:
            if not isinstance(call, dict) or not isinstance(call.get("id"), str) or not call["id"] or call.get("type") != "function":
                raise ValueError
            function = call.get("function")
            if not isinstance(function, dict) or not isinstance(function.get("name"), str) or not function["name"] or not isinstance(function.get("arguments"), str):
                raise ValueError
        if not text.strip() and not calls:
            raise ValueError
        return GenerationResult(text=text, tool_calls=calls)
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        raise GenerationError("Generation service returned no valid text or tool calls") from None


def generate_text(messages: str | list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, *,
                  response_format: dict[str, Any] | None = None, max_tokens: int | None = None,
                  temperature: float | None = None, provider_config: dict[str, str] | None = None) -> str:
    """Return non-empty text; use generate() when tool-only output is possible."""
    options: dict[str, Any] = {}
    for key, value in (("response_format", response_format), ("max_tokens", max_tokens),
                       ("temperature", temperature), ("provider_config", provider_config)):
        if value is not None:
            options[key] = value
    result = generate(messages, tools, **options)
    if not result.text.strip():
        raise GenerationError("Model returned tool calls only; use generate() to access tool_calls")
    return result.text
