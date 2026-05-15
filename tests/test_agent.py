"""Tests for watchmen.agent — the OpenRouter tool-calling loop.

Smoke had zero coverage on this module even though it sits behind every
analyst/curator run. We exercise the retry/backoff math, the API-key
resolution, the cost-ceiling guard, and the tool-call dispatch loop using
a stubbed httpx.Client so nothing reaches the network.
"""

from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from watchmen import agent as _agent
from watchmen.agent import (
    Agent,
    _backoff_seconds,
    _turn_cost,
    call_openrouter,
    load_api_key,
)


# ─── load_api_key ──────────────────────────────────────────────────────────


def test_load_api_key_prefers_environment_variable(monkeypatch):
    """`OPENROUTER_API_KEY` in env wins over the on-disk .env file. This is
    the documented "scriptable override" path — a test that asserts otherwise
    would silently break CI runs that set the key inline."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "env-key-123")
    assert load_api_key() == "env-key-123"


def test_load_api_key_falls_back_to_env_file(monkeypatch, tmp_path):
    """Without an env var, the wizard writes `~/.config/watchmen/.env`. The
    loader must parse that file's `OPENROUTER_API_KEY=...` line, stripping
    quotes — broken parsing here means every fresh-install user has to
    re-export the key manually."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    env_dir = tmp_path / ".config" / "watchmen"
    env_dir.mkdir(parents=True)
    (env_dir / ".env").write_text(
        '# managed by watchmen\nOPENROUTER_API_KEY="sk-or-from-file"\nOTHER=ignored\n'
    )
    assert load_api_key() == "sk-or-from-file"


def test_load_api_key_raises_with_actionable_message(monkeypatch, tmp_path):
    """No env, no file → must raise a RuntimeError that names BOTH escape
    hatches (export + `watchmen settings api-key set`). The message is the
    onboarding contract for users who hit this; assert the relevant tokens
    appear."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with pytest.raises(RuntimeError) as exc:
        load_api_key()
    msg = str(exc.value)
    assert "OPENROUTER_API_KEY" in msg
    assert "watchmen settings api-key" in msg


# ─── _backoff_seconds ──────────────────────────────────────────────────────


def test_backoff_grows_with_attempt():
    """Backoff should grow exponentially with attempt index so a transient
    rate-limit cools off properly without retrying instantly. Jitter is
    uniform[0,1) so the worst-case attempt N is always larger than the
    best-case attempt N-1."""
    # 2^0 + jitter(0,1) ≤ 2.0, 2^4 + jitter ≥ 16.0 — no overlap.
    early = _backoff_seconds(attempt=0, retry_after=None)
    late = _backoff_seconds(attempt=4, retry_after=None)
    assert late > early


def test_backoff_honors_retry_after_floor():
    """Retry-After (in seconds) from the server is the minimum we wait —
    ignoring it means watchmen hammers the API faster than the rate
    limiter allows, escalating soft 429s to hard 429s."""
    # base backoff is at most 2.0 for attempt=0; force the floor to 30
    delay = _backoff_seconds(attempt=0, retry_after="30")
    assert delay >= 30.0


def test_backoff_ignores_malformed_retry_after():
    """Servers occasionally send `Retry-After: <HTTP-date>` (we treat as 0)
    or garbage. Either way the backoff must not raise — better to retry
    sooner than to crash the whole agent loop."""
    delay = _backoff_seconds(attempt=2, retry_after="not-a-number")
    assert delay > 0  # backoff still applies


# ─── _turn_cost ────────────────────────────────────────────────────────────


def test_turn_cost_returns_zero_on_missing_data():
    """If the API didn't return usage (some streaming responses don't), the
    budget ceiling must NOT misfire — return 0.0 rather than blow up. Same
    if model is empty."""
    assert _turn_cost("", {"prompt_tokens": 100}) == 0.0
    assert _turn_cost("claude-opus-4-7", {}) == 0.0
    assert _turn_cost("claude-opus-4-7", None) == 0.0


def test_turn_cost_subtracts_cached_tokens_from_prompt(monkeypatch):
    """OpenRouter folds cached tokens INTO prompt_tokens. Without the
    `fresh_input = max(prompt - cache_read, 0)` subtraction, watchmen would
    double-count cached input and over-bill the budget on long sessions."""
    import watchmen.model_prices as mp

    captured = {}
    def fake_cost(model, in_t, cc5, cc1, cr_t, out_t):
        captured.update(in_t=in_t, cr_t=cr_t, out_t=out_t)
        return 0.01

    monkeypatch.setattr(mp, "turn_cost_usd", fake_cost)
    _turn_cost("claude-opus-4-7", {
        "prompt_tokens": 1000,
        "completion_tokens": 200,
        "prompt_tokens_details": {"cached_tokens": 300},
    })
    assert captured["in_t"] == 700, "fresh_input should be prompt - cached"
    assert captured["cr_t"] == 300
    assert captured["out_t"] == 200


# ─── call_openrouter retry logic ───────────────────────────────────────────


def _ok_response(payload=None) -> MagicMock:
    """Build a mock httpx.Response with status 200 + json body."""
    r = MagicMock(spec=httpx.Response)
    r.status_code = 200
    r.json.return_value = payload or {"choices": [{"message": {"content": "ok"}}]}
    r.raise_for_status.return_value = None
    return r


def _err_response(status: int, retry_after: str | None = None) -> MagicMock:
    r = MagicMock(spec=httpx.Response)
    r.status_code = status
    r.headers = {"Retry-After": retry_after} if retry_after else {}
    # raise_for_status only fires for non-2xx, but the retry path inspects
    # status_code first so we don't strictly need to make it raise here.
    if status >= 400:
        r.raise_for_status.side_effect = httpx.HTTPStatusError(
            f"HTTP {status}", request=MagicMock(), response=r
        )
    return r


def test_call_openrouter_succeeds_on_first_try(monkeypatch):
    """Happy path: 200 on first call, no retry, returned dict matches body."""
    monkeypatch.setattr(_agent.time, "sleep", lambda *_: None)  # don't actually wait
    client = MagicMock(spec=httpx.Client)
    client.post.return_value = _ok_response({"choices": [{"message": {"role": "a"}}]})
    out = call_openrouter(client, headers={}, payload={})
    assert out == {"choices": [{"message": {"role": "a"}}]}
    assert client.post.call_count == 1


def test_call_openrouter_retries_on_429_then_succeeds(monkeypatch):
    """429 is the rate-limit signal — must retry, not propagate. The third
    call returns 200 so the function should succeed without raising."""
    sleeps = []
    monkeypatch.setattr(_agent.time, "sleep", lambda s: sleeps.append(s))

    client = MagicMock(spec=httpx.Client)
    client.post.side_effect = [
        _err_response(429, retry_after="1"),
        _err_response(429),
        _ok_response(),
    ]
    out = call_openrouter(client, headers={}, payload={}, max_retries=4)
    assert "choices" in out
    assert client.post.call_count == 3
    assert len(sleeps) == 2, "expected 2 backoff sleeps before the successful third call"


def test_call_openrouter_retries_on_500_then_succeeds(monkeypatch):
    """500/502/503/504/524 are transient gateway/upstream errors — same
    retry policy as 429. Validate one representative."""
    monkeypatch.setattr(_agent.time, "sleep", lambda *_: None)
    client = MagicMock(spec=httpx.Client)
    client.post.side_effect = [_err_response(503), _ok_response()]
    out = call_openrouter(client, headers={}, payload={}, max_retries=3)
    assert "choices" in out
    assert client.post.call_count == 2


def test_call_openrouter_raises_immediately_on_400(monkeypatch):
    """400/401/403/404 are CLIENT errors — retrying won't help, and waiting
    just delays the user's "fix your prompt" feedback. Surface immediately
    via raise_for_status."""
    monkeypatch.setattr(_agent.time, "sleep", lambda *_: None)
    client = MagicMock(spec=httpx.Client)
    client.post.return_value = _err_response(400)
    with pytest.raises(httpx.HTTPStatusError):
        call_openrouter(client, headers={}, payload={})
    assert client.post.call_count == 1, "must not retry on 400"


def test_call_openrouter_retries_request_error_then_raises(monkeypatch):
    """Network failure (httpx.RequestError — connection refused, read
    timeout, etc.) is retryable. After max_retries it must re-raise the
    last exception rather than swallow it."""
    monkeypatch.setattr(_agent.time, "sleep", lambda *_: None)
    client = MagicMock(spec=httpx.Client)
    client.post.side_effect = httpx.ConnectError("connection refused")
    with pytest.raises(httpx.RequestError):
        call_openrouter(client, headers={}, payload={}, max_retries=3)
    assert client.post.call_count == 3


def test_call_openrouter_logger_records_retries(monkeypatch):
    """When a `log` callback is supplied, retry attempts must be recorded so
    operators can see WHY a run took 30s extra. Validate the log strings
    include the status code and attempt number."""
    monkeypatch.setattr(_agent.time, "sleep", lambda *_: None)
    client = MagicMock(spec=httpx.Client)
    client.post.side_effect = [_err_response(429), _ok_response()]
    log_lines = []
    call_openrouter(client, headers={}, payload={}, max_retries=3, log=log_lines.append)
    assert any("429" in line for line in log_lines)
    assert any("retry in" in line for line in log_lines)


# ─── Agent class ───────────────────────────────────────────────────────────


def _build_agent(model="x/y", tools=None, terminal="finish", **kwargs) -> Agent:
    return Agent(
        name="test",
        model=model,
        system_prompt="be brief",
        tool_specs=tools or [{"type": "function", "function": {"name": terminal}}],
        tool_handlers=kwargs.pop("handlers", {}),
        terminal_tool=terminal,
        client=MagicMock(spec=httpx.Client),
        api_key="sk-fake",
        **kwargs,
    )


def test_agent_init_sets_openrouter_attribution_headers():
    """`HTTP-Referer` + `X-Title` are OpenRouter's app-attribution headers.
    We keep `watchmen:<agent-name>` in X-Title so OpenRouter dashboards
    show which sub-agent (analyst, curator, finder, …) was responsible.
    Regression guard against an accidental header strip."""
    a = _build_agent()
    assert a.headers["HTTP-Referer"] == "https://github.com/firstbatchxyz/watchmen"
    assert a.headers["X-Title"] == "watchmen:test"
    assert a.headers["Authorization"] == "Bearer sk-fake"


def test_agent_run_returns_terminal_args_and_exits_loop(monkeypatch):
    """When the model calls the terminal tool, the loop must exit and
    return the parsed arguments. No further OpenRouter calls should happen."""
    a = _build_agent(terminal="finish")
    a.client.post.return_value = _ok_response({
        "choices": [{
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "1",
                    "function": {"name": "finish", "arguments": '{"result": "done"}'},
                }],
            }
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0},
    })
    monkeypatch.setattr(_agent.time, "sleep", lambda *_: None)
    args, messages = a.run("go")
    assert args == {"result": "done"}
    assert a.client.post.call_count == 1


def test_agent_run_dispatches_non_terminal_tool_and_continues(monkeypatch):
    """Non-terminal tool calls go through `tool_handlers`. The result string
    is appended to messages and the loop continues. The second turn calls
    the terminal tool to end."""
    handler_calls = []
    def lookup(query):
        handler_calls.append(query)
        return {"answer": "42"}

    a = _build_agent(handlers={"lookup": lookup}, terminal="finish")
    a.client.post.side_effect = [
        # Turn 1: model calls `lookup`
        _ok_response({
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "checking",
                    "tool_calls": [{
                        "id": "1",
                        "function": {"name": "lookup", "arguments": '{"query": "x"}'},
                    }],
                }
            }],
            "usage": {},
        }),
        # Turn 2: model calls `finish`
        _ok_response({
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": "2",
                        "function": {"name": "finish", "arguments": "{}"},
                    }],
                }
            }],
            "usage": {},
        }),
    ]
    monkeypatch.setattr(_agent.time, "sleep", lambda *_: None)
    args, messages = a.run("go")
    assert handler_calls == ["x"]
    # Messages roughly: system, user, assistant(lookup), tool(result), assistant(finish), tool(ok)
    assert any(m.get("role") == "tool" and "42" in m.get("content", "") for m in messages)


def test_agent_run_aborts_when_cost_ceiling_reached(monkeypatch):
    """`max_cost_usd` is the budget guardrail for scheduled curator runs.
    Once cumulative cost crosses the threshold, the loop must exit before
    burning another expensive turn — so even a runaway model can only
    overshoot by one turn's worth."""
    monkeypatch.setattr(_agent.time, "sleep", lambda *_: None)
    # Force every turn to register a fixed $0.20 cost regardless of usage.
    monkeypatch.setattr(_agent, "_turn_cost", lambda model, usage: 0.20)

    a = _build_agent(max_cost_usd=0.50)
    # Each turn returns a non-terminal tool_call so the loop only exits via budget.
    a.client.post.return_value = _ok_response({
        "choices": [{
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "x",
                    "function": {"name": "noop", "arguments": "{}"},
                }],
            }
        }],
        "usage": {},
    })
    args, _ = a.run("go", max_iter=10)
    # 0.20 * 3 = 0.60 ≥ 0.50 → break on 3rd turn
    assert a.client.post.call_count == 3
    assert args == {}, "terminal never fired — args should be empty"


def test_agent_run_returns_empty_when_max_iter_exhausted(monkeypatch):
    """If the model never calls the terminal tool within max_iter, we return
    empty args rather than loop forever. Caller treats this as "model
    gave up" and falls back to whatever default it expects."""
    monkeypatch.setattr(_agent.time, "sleep", lambda *_: None)
    # No tool_calls at all → loop exits immediately on first turn.
    a = _build_agent()
    a.client.post.return_value = _ok_response({
        "choices": [{
            "message": {"role": "assistant", "content": "I give up"}
        }],
        "usage": {},
    })
    args, _ = a.run("go", max_iter=5)
    assert args == {}
    assert a.client.post.call_count == 1
