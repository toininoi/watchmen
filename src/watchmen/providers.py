"""Provider abstraction — auth, endpoint, request/response shape per LLM provider.

Watchmen historically only called OpenRouter, which speaks the OpenAI
chat-completions wire format. As of 0.7 we support OpenAI and Anthropic
direct in addition to OpenRouter. OpenAI is wire-compatible with OpenRouter
(same JSON, different URL + auth header). Anthropic uses its Messages API,
which has a different request/response shape — we translate to/from the
OpenAI chat-completions shape so the agent loop in agent.py stays
provider-agnostic.

Adding a new provider should be one Provider subclass here, not a refactor
of agent.py.
"""

from __future__ import annotations

import json
from dataclasses import dataclass


PROVIDER_NAMES = ("openrouter", "openai", "anthropic")

# Human-friendly display labels — used by doctor + onboarding so the UI
# reads "OpenRouter key" / "OpenAI key" / "Anthropic key" instead of
# the lowercase identifier.
PROVIDER_DISPLAY = {
    "openrouter": "OpenRouter",
    "openai":     "OpenAI",
    "anthropic":  "Anthropic",
}


def display_name(provider: str) -> str:
    """Title-cased display label, falling back to the raw name."""
    return PROVIDER_DISPLAY.get(provider, provider)


# ─── Canonical wire format ────────────────────────────────────────────────
#
# Everything outside this module sees the OpenAI chat-completions shape:
#   request:  {"model", "messages", "tools"}
#   response: {"choices": [{"message": {"content", "tool_calls"}}],
#              "usage": {"prompt_tokens", "completion_tokens",
#                        "prompt_tokens_details": {"cached_tokens"}},
#              "model"}
# Translators in this module convert provider-native shapes to/from that.


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    detail: str


class Provider:
    """Base provider. Subclass for each backend."""

    name: str = "base"
    endpoint: str = ""
    # Default model for this provider when WATCHMEN_DEFAULT_MODEL is unset.
    # Each provider has different model naming conventions so the picker
    # can't just default to one global string.
    default_model: str = ""

    def headers(self, api_key: str, *, agent_name: str = "") -> dict[str, str]:
        """Auth + content headers for a chat-completions call."""
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    def translate_request(
        self, *, model: str, messages: list[dict], tools: list[dict]
    ) -> dict:
        """Translate the canonical chat-completions request into the
        provider's wire format. Default is identity (OpenAI-compatible)."""
        return {"model": model, "messages": messages, "tools": tools}

    def translate_response(self, raw: dict) -> dict:
        """Translate provider response back into the canonical chat-completions
        shape. Default is identity."""
        return raw

    def probe(self, api_key: str, *, timeout: float = 10.0) -> ProbeResult:
        """Live-validate the key by hitting an inexpensive endpoint."""
        raise NotImplementedError


# ─── OpenRouter ────────────────────────────────────────────────────────────


class OpenRouterProvider(Provider):
    name = "openrouter"
    endpoint = "https://openrouter.ai/api/v1/chat/completions"
    default_model = "deepseek/deepseek-v4-flash"

    def headers(self, api_key: str, *, agent_name: str = "") -> dict[str, str]:
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            # OpenRouter app attribution.
            "HTTP-Referer": "https://github.com/firstbatchxyz/watchmen",
            "X-Title": f"watchmen:{agent_name}" if agent_name else "watchmen",
        }

    def probe(self, api_key: str, *, timeout: float = 10.0) -> ProbeResult:
        import httpx
        try:
            r = httpx.get(
                "https://openrouter.ai/api/v1/auth/key",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=timeout,
            )
        except httpx.RequestError as e:
            return ProbeResult(False, f"connection error: {type(e).__name__}")
        if r.status_code == 200:
            try:
                info = (r.json() or {}).get("data") or {}
            except ValueError:
                info = {}
            usage = info.get("usage")
            limit = info.get("limit")
            if usage is not None and limit is not None and limit > 0:
                return ProbeResult(True, f"valid · credits used ${float(usage):.2f} of ${float(limit):.2f}")
            if usage is not None and limit is None:
                return ProbeResult(True, f"valid · credits used ${float(usage):.2f} (no hard limit)")
            return ProbeResult(True, "valid")
        if r.status_code == 401:
            try:
                msg = (r.json().get("error") or {}).get("message", "")
            except (ValueError, AttributeError):
                msg = ""
            return ProbeResult(False, f"401 · {msg or 'unauthorized'}")
        return ProbeResult(False, f"HTTP {r.status_code} · {r.text[:120]}")


# ─── OpenAI ────────────────────────────────────────────────────────────────


class OpenAIProvider(Provider):
    name = "openai"
    endpoint = "https://api.openai.com/v1/chat/completions"
    # gpt-5-mini is the modern cost-efficient default with tool use support.
    # Override with WATCHMEN_DEFAULT_MODEL=gpt-5 etc. for higher-quality runs.
    default_model = "gpt-5-mini"

    def probe(self, api_key: str, *, timeout: float = 10.0) -> ProbeResult:
        import httpx
        try:
            r = httpx.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=timeout,
            )
        except httpx.RequestError as e:
            return ProbeResult(False, f"connection error: {type(e).__name__}")
        if r.status_code == 200:
            try:
                count = len((r.json() or {}).get("data") or [])
                return ProbeResult(True, f"valid · {count} models accessible")
            except ValueError:
                return ProbeResult(True, "valid")
        if r.status_code == 401:
            return ProbeResult(False, "401 · invalid or revoked key")
        return ProbeResult(False, f"HTTP {r.status_code} · {r.text[:120]}")


# ─── Anthropic ─────────────────────────────────────────────────────────────


class AnthropicProvider(Provider):
    name = "anthropic"
    endpoint = "https://api.anthropic.com/v1/messages"
    default_model = "claude-haiku-4-5-20251001"
    # Cap on output tokens per turn — Anthropic requires this field, OpenAI
    # treats it as optional. We pick a generous default; callers that need
    # less can rely on the model stopping early.
    _max_tokens_per_turn = 8192

    def headers(self, api_key: str, *, agent_name: str = "") -> dict[str, str]:
        return {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

    def translate_request(
        self, *, model: str, messages: list[dict], tools: list[dict]
    ) -> dict:
        """Chat-completions → Messages API.

        Key differences:
        - system is a top-level field, not a message
        - messages only contain user/assistant
        - tool results come back as content blocks inside a user message
        - tool calls go out as content blocks inside an assistant message
        - tool schema uses `input_schema` not `parameters`
        """
        system_parts: list[str] = []
        out_msgs: list[dict] = []

        for m in messages:
            role = m.get("role")
            if role == "system":
                if m.get("content"):
                    system_parts.append(m["content"])
                continue

            if role == "tool":
                # OpenAI uses {role: tool, tool_call_id, content}; Anthropic
                # wraps the result in a user message with a tool_result block.
                out_msgs.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": m.get("tool_call_id", ""),
                        "content": m.get("content", ""),
                    }],
                })
                continue

            if role == "assistant":
                blocks: list[dict] = []
                text = m.get("content") or ""
                if text:
                    blocks.append({"type": "text", "text": text})
                for tc in (m.get("tool_calls") or []):
                    try:
                        tc_args = json.loads(tc["function"].get("arguments") or "{}")
                    except (KeyError, json.JSONDecodeError):
                        tc_args = {}
                    blocks.append({
                        "type": "tool_use",
                        "id": tc.get("id", ""),
                        "name": tc.get("function", {}).get("name", ""),
                        "input": tc_args,
                    })
                # Anthropic rejects empty content arrays; fall back to a
                # single empty text block if the assistant message somehow
                # carries neither text nor tool calls.
                if not blocks:
                    blocks = [{"type": "text", "text": ""}]
                out_msgs.append({"role": "assistant", "content": blocks})
                continue

            # user (or anything else): pass through
            out_msgs.append({"role": role or "user", "content": m.get("content", "")})

        anthropic_tools: list[dict] = []
        for t in (tools or []):
            fn = t.get("function") if t.get("type") == "function" else t
            if not fn:
                continue
            anthropic_tools.append({
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
            })

        body = {
            "model": model,
            "max_tokens": self._max_tokens_per_turn,
            "messages": out_msgs,
        }
        if system_parts:
            body["system"] = "\n\n".join(system_parts)
        if anthropic_tools:
            body["tools"] = anthropic_tools
        return body

    def translate_response(self, raw: dict) -> dict:
        """Messages API → chat-completions shape.

        Anthropic returns `content` as a list of blocks (text + tool_use).
        The rest of agent.py reads `choices[0].message.content` and
        `choices[0].message.tool_calls`, so we fold the blocks into that
        shape.
        """
        text_parts: list[str] = []
        tool_calls: list[dict] = []
        for block in (raw.get("content") or []):
            btype = block.get("type")
            if btype == "text":
                text_parts.append(block.get("text") or "")
            elif btype == "tool_use":
                tool_calls.append({
                    "id": block.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input") or {}),
                    },
                })

        usage = raw.get("usage") or {}
        return {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "".join(text_parts),
                    "tool_calls": tool_calls or None,
                },
                "finish_reason": raw.get("stop_reason"),
            }],
            "model": raw.get("model"),
            "usage": {
                "prompt_tokens": int(usage.get("input_tokens") or 0),
                "completion_tokens": int(usage.get("output_tokens") or 0),
                "prompt_tokens_details": {
                    "cached_tokens": int(usage.get("cache_read_input_tokens") or 0),
                },
            },
        }

    def probe(self, api_key: str, *, timeout: float = 10.0) -> ProbeResult:
        import httpx
        try:
            r = httpx.get(
                "https://api.anthropic.com/v1/models",
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
                timeout=timeout,
            )
        except httpx.RequestError as e:
            return ProbeResult(False, f"connection error: {type(e).__name__}")
        if r.status_code == 200:
            try:
                count = len((r.json() or {}).get("data") or [])
                return ProbeResult(True, f"valid · {count} models accessible")
            except ValueError:
                return ProbeResult(True, "valid")
        if r.status_code in (401, 403):
            return ProbeResult(False, f"{r.status_code} · invalid or revoked key")
        return ProbeResult(False, f"HTTP {r.status_code} · {r.text[:120]}")


# ─── Registry ──────────────────────────────────────────────────────────────


_PROVIDERS: dict[str, Provider] = {
    "openrouter": OpenRouterProvider(),
    "openai": OpenAIProvider(),
    "anthropic": AnthropicProvider(),
}


def get_provider(name: str) -> Provider:
    """Return the Provider instance for `name`. Raises ValueError on unknown."""
    if name not in _PROVIDERS:
        raise ValueError(
            f"unknown provider: {name!r} (valid: {', '.join(PROVIDER_NAMES)})"
        )
    return _PROVIDERS[name]


def all_providers() -> dict[str, Provider]:
    """Map of provider name → instance. Used by CLI listings and doctor."""
    return dict(_PROVIDERS)
