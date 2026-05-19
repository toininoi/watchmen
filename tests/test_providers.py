"""Tests for the provider abstraction.

Covers the three pieces that determine whether multi-provider routing
actually works end-to-end:

1. `config.active_provider()` selection — explicit env var, auto-detect by
   key presence, default fallback. Breaking this would silently route
   curator runs to the wrong backend on installs that use multiple keys.

2. Provider request/response translation — especially Anthropic's Messages
   API ↔ OpenAI chat-completions translation, which is the riskiest part
   of the refactor. We assert the wire shapes for both directions so a
   regression in the Anthropic adapter is caught in unit tests, not
   discovered halfway through a curator run.

3. `agent.chat_call()` end-to-end — dispatches through the provider, posts
   to its endpoint, parses the response. We stub httpx so nothing hits the
   network and assert the right URL + headers + body shape were used.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from watchmen import agent, config, providers


# ─── Provider selection ────────────────────────────────────────────────────


def test_active_provider_explicit_env_wins(monkeypatch):
    """An explicit WATCHMEN_PROVIDER beats the auto-detect heuristic. This
    is the override path power users will reach for when they have keys
    for multiple providers on disk."""
    monkeypatch.setenv("WATCHMEN_PROVIDER", "anthropic")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-xxx")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-yyy")
    assert config.active_provider() == "anthropic"


def test_active_provider_ignores_unknown_explicit(monkeypatch, tmp_path):
    """A WATCHMEN_PROVIDER set to a junk value (typo, stale config) falls
    back to the auto-detect path rather than crashing every command —
    keeps the user out of a `watchmen <anything>` outage if their .env
    drifts."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("WATCHMEN_PROVIDER", "gemini")  # not supported
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real")
    # Unknown explicit value → auto-detect picks first present-key in priority order
    assert config.active_provider() == "openai"


def test_active_provider_auto_detect_priority(monkeypatch, tmp_path):
    """Auto-detect prefers OpenRouter when present (backward compat with
    existing OPENROUTER_API_KEY-only installs), then OpenAI, then Anthropic.
    Critical for the upgrade path — pre-0.7 users must not switch backends
    by accident when the new provider abstraction lands."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("WATCHMEN_PROVIDER", raising=False)

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-only")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert config.active_provider() == "anthropic"

    monkeypatch.setenv("OPENAI_API_KEY", "sk-oa")
    assert config.active_provider() == "openai"  # openai outranks anthropic

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or")
    assert config.active_provider() == "openrouter"  # openrouter outranks both


def test_active_provider_default_when_no_keys(monkeypatch, tmp_path):
    """Empty install (no env vars, no .env file) defaults to openrouter so
    the first-run wizard's prompts still match what the docs describe."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("WATCHMEN_PROVIDER", "OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    assert config.active_provider() == "openrouter"


def test_provider_key_returns_none_for_unset(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert config.provider_key("anthropic") is None


def test_provider_key_returns_none_for_unknown_provider():
    assert config.provider_key("gemini") is None


def test_default_model_respects_explicit_override(monkeypatch):
    """WATCHMEN_DEFAULT_MODEL lets users pin a specific model regardless of
    provider — the override path most likely to be used in tests + CI."""
    monkeypatch.setenv("WATCHMEN_DEFAULT_MODEL", "gpt-5")
    monkeypatch.setenv("WATCHMEN_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert config.default_model() == "gpt-5"


def test_default_model_falls_back_to_provider_default(monkeypatch, tmp_path):
    """Without an override, each provider's `default_model` attribute drives
    the choice — so `watchmen analyze` picks deepseek on openrouter,
    gpt-5-mini on openai, claude-haiku on anthropic without user input."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("WATCHMEN_DEFAULT_MODEL", raising=False)
    monkeypatch.setenv("WATCHMEN_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    assert config.default_model() == providers.get_provider("anthropic").default_model


# ─── Provider registry ─────────────────────────────────────────────────────


def test_get_provider_known():
    assert providers.get_provider("openrouter").name == "openrouter"
    assert providers.get_provider("openai").name == "openai"
    assert providers.get_provider("anthropic").name == "anthropic"


def test_get_provider_unknown_raises():
    with pytest.raises(ValueError, match="unknown provider"):
        providers.get_provider("gemini")


# ─── OpenRouter / OpenAI: identity translation ─────────────────────────────


def test_openrouter_translate_request_is_identity():
    """OpenRouter and OpenAI both speak the chat-completions wire format —
    no translation needed. The translate_request default is identity; a
    regression would silently break the OpenRouter path for every user."""
    prov = providers.get_provider("openrouter")
    body = prov.translate_request(
        model="deepseek/deepseek-v4-flash",
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "noop"}}],
    )
    assert body == {
        "model": "deepseek/deepseek-v4-flash",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function", "function": {"name": "noop"}}],
    }


def test_openrouter_headers_carry_app_attribution():
    """OpenRouter app-attribution headers (HTTP-Referer + X-Title) appear
    in the developer dashboard. We want watchmen runs to be identifiable
    so traffic spikes get attributed to the right tool."""
    prov = providers.get_provider("openrouter")
    h = prov.headers("sk-or-test", agent_name="analyst")
    assert h["Authorization"] == "Bearer sk-or-test"
    assert h["HTTP-Referer"] == "https://github.com/firstbatchxyz/watchmen"
    assert h["X-Title"] == "watchmen:analyst"


def test_openai_headers_minimal():
    """OpenAI ignores referer/title headers; sending them is harmless but
    cluttery. We keep its header set minimal so production logs are clean."""
    prov = providers.get_provider("openai")
    h = prov.headers("sk-test")
    assert h == {"Authorization": "Bearer sk-test", "Content-Type": "application/json"}


# ─── Anthropic: Messages API translation ───────────────────────────────────


def test_anthropic_headers_use_x_api_key():
    """Anthropic uses `x-api-key`, not `Authorization: Bearer`. Getting this
    wrong is the single most common source of 401s when migrating between
    providers — explicit test so a regression here can't slip through."""
    prov = providers.get_provider("anthropic")
    h = prov.headers("sk-ant-test")
    assert h["x-api-key"] == "sk-ant-test"
    assert h["anthropic-version"] == "2023-06-01"
    assert "Authorization" not in h


def test_anthropic_request_lifts_system_to_top_level():
    """Anthropic's Messages API rejects `system` messages inside the
    `messages` list — `system` must be a top-level field. The translator
    moves it; if this regresses, every analyst run on Anthropic 400s."""
    prov = providers.get_provider("anthropic")
    body = prov.translate_request(
        model="claude-haiku-4-5-20251001",
        messages=[
            {"role": "system", "content": "you are an analyst"},
            {"role": "user", "content": "summarize today"},
        ],
        tools=[],
    )
    assert body["system"] == "you are an analyst"
    assert body["messages"] == [{"role": "user", "content": "summarize today"}]
    assert "max_tokens" in body  # required field — without it Anthropic rejects


def test_anthropic_request_translates_tool_schema():
    """OpenAI tools use `{"function": {"name", "parameters"}}`; Anthropic
    uses `{"name", "input_schema"}`. The translator unwraps and renames —
    a regression here breaks every curator skill-finder call."""
    prov = providers.get_provider("anthropic")
    body = prov.translate_request(
        model="claude-haiku-4-5-20251001",
        messages=[{"role": "user", "content": "go"}],
        tools=[{
            "type": "function",
            "function": {
                "name": "search",
                "description": "search the corpus",
                "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
            },
        }],
    )
    assert body["tools"] == [{
        "name": "search",
        "description": "search the corpus",
        "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
    }]


def test_anthropic_request_translates_assistant_tool_calls_to_blocks():
    """The agent loop replays prior turns. An assistant message with
    OpenAI-style `tool_calls` must convert to Anthropic content blocks on
    each replay, otherwise multi-turn tool dispatch never converges."""
    prov = providers.get_provider("anthropic")
    body = prov.translate_request(
        model="m",
        messages=[
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "thinking...", "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {"name": "search", "arguments": json.dumps({"q": "foo"})},
            }]},
            {"role": "tool", "tool_call_id": "call_1", "content": "result"},
        ],
        tools=[],
    )
    asst = body["messages"][1]
    assert asst["role"] == "assistant"
    assert any(b["type"] == "text" and b["text"] == "thinking..." for b in asst["content"])
    tool_use = next(b for b in asst["content"] if b["type"] == "tool_use")
    assert tool_use["id"] == "call_1"
    assert tool_use["name"] == "search"
    assert tool_use["input"] == {"q": "foo"}

    # Tool result becomes a user message with a tool_result block — required
    # so the model can correlate the result with its earlier call.
    tool_msg = body["messages"][2]
    assert tool_msg["role"] == "user"
    assert tool_msg["content"][0]["type"] == "tool_result"
    assert tool_msg["content"][0]["tool_use_id"] == "call_1"


def test_anthropic_response_folds_blocks_into_openai_shape():
    """Agent.run reads `choices[0].message.content` and `.tool_calls` —
    the response translator must reshape Anthropic's content-block list
    into that flat OpenAI form, or the entire dispatch loop breaks."""
    prov = providers.get_provider("anthropic")
    raw = {
        "id": "msg_1",
        "model": "claude-haiku-4-5-20251001",
        "content": [
            {"type": "text", "text": "I'll search."},
            {"type": "tool_use", "id": "tu_1", "name": "search", "input": {"q": "foo"}},
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 12, "output_tokens": 5, "cache_read_input_tokens": 3},
    }
    out = prov.translate_response(raw)
    msg = out["choices"][0]["message"]
    assert msg["content"] == "I'll search."
    assert msg["tool_calls"][0]["function"]["name"] == "search"
    assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {"q": "foo"}
    assert out["usage"]["prompt_tokens"] == 12
    assert out["usage"]["completion_tokens"] == 5
    assert out["usage"]["prompt_tokens_details"]["cached_tokens"] == 3


def test_anthropic_response_no_tool_calls_returns_none():
    """When the model only produces text (no tool use), tool_calls must be
    None (not empty list) — agent.run uses truthiness to decide whether
    to dispatch tools, and an empty list would still be falsy but the
    OpenAI wire format conventionally omits the field entirely."""
    prov = providers.get_provider("anthropic")
    out = prov.translate_response({
        "model": "m",
        "content": [{"type": "text", "text": "all done"}],
        "usage": {"input_tokens": 1, "output_tokens": 2},
    })
    assert out["choices"][0]["message"]["tool_calls"] is None


# ─── End-to-end: chat_call dispatches through the right provider ──────────


class _StubResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload
        self.headers = {}
        self.text = json.dumps(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError("err", request=None, response=self)


def test_chat_call_uses_openai_endpoint_when_provider_openai(monkeypatch):
    """End-to-end smoke for the OpenAI direct path. We stub httpx so no
    real call goes out, and assert the URL + auth header match OpenAI's
    public API rather than OpenRouter's."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-oa")
    monkeypatch.setenv("WATCHMEN_PROVIDER", "openai")
    client = MagicMock()
    client.post.return_value = _StubResponse(200, {
        "choices": [{"message": {"content": "ok", "tool_calls": None}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        "model": "gpt-5-mini",
    })

    out = agent.chat_call(client, [{"role": "user", "content": "hi"}], model="gpt-5-mini")

    assert client.post.called
    args, kwargs = client.post.call_args
    assert args[0] == "https://api.openai.com/v1/chat/completions"
    assert kwargs["headers"]["Authorization"] == "Bearer sk-test-oa"
    assert out["choices"][0]["message"]["content"] == "ok"


def test_chat_call_uses_anthropic_endpoint_when_provider_anthropic(monkeypatch):
    """End-to-end smoke for the Anthropic direct path — checks the URL,
    the x-api-key header (NOT Authorization), and that the response
    translator was applied before returning to the caller."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("WATCHMEN_PROVIDER", "anthropic")
    client = MagicMock()
    client.post.return_value = _StubResponse(200, {
        "model": "claude-haiku-4-5-20251001",
        "content": [{"type": "text", "text": "hello"}],
        "usage": {"input_tokens": 4, "output_tokens": 2},
    })

    out = agent.chat_call(client, [{"role": "user", "content": "hi"}], model="claude-haiku-4-5-20251001")

    args, kwargs = client.post.call_args
    assert args[0] == "https://api.anthropic.com/v1/messages"
    assert kwargs["headers"]["x-api-key"] == "sk-ant-test"
    assert "Authorization" not in kwargs["headers"]
    # Response went through Anthropic translator → OpenAI shape
    assert out["choices"][0]["message"]["content"] == "hello"


# ─── settings model command ───────────────────────────────────────────────


def test_settings_model_persists_and_clears(monkeypatch, tmp_path, capsys):
    """`watchmen settings model <name>` should persist the override and
    `--clear` should remove it. This is the menu-driven equivalent of
    `export WATCHMEN_DEFAULT_MODEL=...` — if the persistence breaks,
    daemon-scheduled runs silently revert to the provider default."""
    from watchmen import cli

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("WATCHMEN_DEFAULT_MODEL", raising=False)
    monkeypatch.setenv("WATCHMEN_PROVIDER", "openrouter")

    # Set
    rc = cli.main(["settings", "model", "gpt-5"])
    assert rc == 0
    assert config.read_env_var("WATCHMEN_DEFAULT_MODEL") == "gpt-5"
    assert config.default_model() == "gpt-5"

    # Clear
    rc = cli.main(["settings", "model", "--clear"])
    assert rc == 0
    assert config.read_env_var("WATCHMEN_DEFAULT_MODEL") is None
    # Falls back to the active provider's default
    assert config.default_model() == providers.get_provider("openrouter").default_model


def test_clear_env_var_returns_false_for_missing_key(tmp_path, monkeypatch):
    """`clear_env_var` should distinguish "nothing to clear" from "cleared".
    The settings menu uses this to print "no override was set" vs the
    success message — getting this confused would mislead users."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # No .env file at all
    assert config.clear_env_var("NOT_THERE") is False
    # File exists but key isn't in it
    config.write_env_var("OTHER", "x")
    assert config.clear_env_var("NOT_THERE") is False
    # Key present → returns True
    assert config.clear_env_var("OTHER") is True
    assert config.read_env_var("OTHER") is None


def test_settings_menu_back_and_cancel_never_pass_through_as_data():
    """Regression: questionary 2.x interprets `Choice("Back", value=None)`
    as "use the title as the value", so picking Back leaked the literal
    string "Back" into downstream code that expected a project key /
    provider name. The fix uses explicit sentinel values; this test scans
    the source to make sure no future `value=None` slips back in on a
    nav-action Choice."""
    from pathlib import Path as _Path
    src = (_Path(__file__).parent.parent / "src" / "watchmen" / "commands" / "settings_menu.py").read_text()
    # Strip line comments before scanning so the regression-explainer
    # comment in the module doesn't trip the check.
    code_only = "\n".join(
        line.split("#", 1)[0] for line in src.splitlines()
    )
    assert 'value=None' not in code_only, (
        "settings_menu has a Choice with value=None — questionary treats "
        "that as 'use title as value', which leaks the literal nav-label "
        "(e.g. 'Back') into downstream handlers. Use _BACK / _CANCEL instead."
    )


def test_interactive_settings_falls_back_in_non_tty(monkeypatch, capsys):
    """Running `watchmen settings` with stdin/stdout piped (CI, scripts)
    must NOT block on questionary — instead print the flat-subcommand
    cheatsheet so users aren't stuck."""
    from watchmen.commands import settings_menu
    # Force the non-TTY branch
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    rc = settings_menu.run_interactive_settings()
    assert rc == 0
    out = capsys.readouterr().out
    assert "non-interactive shell detected" in out
    # Must surface every flat subcommand, otherwise the fallback is useless
    for sub in ("list", "show", "set ", "api-key", "provider", "model", "port"):
        assert sub in out, f"fallback missing `settings {sub}`"


# ─── original chat_call test (kept below the new ones) ────────────────────


def test_chat_call_extra_payload_overrides_get_merged(monkeypatch):
    """Caller-supplied kwargs (temperature, max_tokens) flow into the request
    body for both shapes — insights.py relies on this to pin temperature=0.3
    regardless of provider."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("WATCHMEN_PROVIDER", "openrouter")
    client = MagicMock()
    client.post.return_value = _StubResponse(200, {
        "choices": [{"message": {"content": "x"}}],
        "usage": {},
    })

    agent.chat_call(
        client,
        [{"role": "user", "content": "hi"}],
        model="deepseek/deepseek-v4-flash",
        temperature=0.3,
        max_tokens=500,
    )
    _, kwargs = client.post.call_args
    body = kwargs["json"]
    assert body["temperature"] == 0.3
    assert body["max_tokens"] == 500
