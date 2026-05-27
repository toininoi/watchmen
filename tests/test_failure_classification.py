"""Tests for run-failure classification (util.classify_failure / classify_run_failure)."""

from __future__ import annotations

from pathlib import Path

import pytest

from watchmen import util


# ─── classify_failure (pure) ────────────────────────────────────────────────

def test_rate_limit_from_retry_log_label():
    out = "[14:14:50] chatgpt: HTTP 429, retry in 1.6s (attempt 1/7)"
    assert util.classify_failure(out, 1) == "rate_limit: chatgpt"


def test_rate_limit_from_httpx_error_url():
    out = "httpx.HTTPStatusError: Client error '429 Too Many Requests' for url 'https://chatgpt.com/backend-api/codex/responses'"
    assert util.classify_failure(out, 1) == "rate_limit: chatgpt"


def test_rate_limit_provider_from_anthropic_url():
    out = "Client error '429 Too Many Requests' for url 'https://api.anthropic.com/v1/messages'"
    assert util.classify_failure(out, 1) == "rate_limit: claude-pro"


def test_rate_limit_unknown_provider_falls_back():
    out = "429 too many requests"  # no label, no known host
    assert util.classify_failure(out, 1) == "rate_limit: provider"


def test_auth_from_oauth_expiry():
    out = "RuntimeError: Claude Pro OAuth token expired — run `claude login`"
    assert util.classify_failure(out, 1) == "auth: claude-pro"


def test_auth_from_401():
    out = "openrouter: HTTP 401, Client error '401 Unauthorized'"
    assert util.classify_failure(out, 1) == "auth: openrouter"


def test_plain_exit_when_no_signature():
    out = "Traceback: KeyError 'foo'\nValueError: bad thing"
    assert util.classify_failure(out, 2) == "exit 2"


def test_empty_output_is_plain_exit():
    assert util.classify_failure("", 1) == "exit 1"


# ─── classify_run_failure (reads bundle _run.log) ───────────────────────────

@pytest.fixture
def bundles(tmp_path: Path, monkeypatch):
    root = tmp_path / "bundles"
    root.mkdir()
    monkeypatch.setattr(util, "BUNDLES_DIR", root)
    return root


def test_run_failure_reads_run_log_tail(bundles):
    proj = bundles / "kai-frontend"
    proj.mkdir()
    (proj / "_run.log").write_text(
        "[03:01:38] curator started\n[03:01:40] chatgpt: HTTP 429, retry in 4.7s\n",
        encoding="utf-8",
    )
    # No captured output, but the log carries the signal.
    assert util.classify_run_failure("kai-frontend", 1) == "rate_limit: chatgpt"


def test_run_failure_extra_output_takes_part(bundles):
    proj = bundles / "p"
    proj.mkdir()
    (proj / "_run.log").write_text("nothing interesting here\n", encoding="utf-8")
    note = util.classify_run_failure(
        "p", 1, "Client error '429 Too Many Requests' for url 'https://chatgpt.com/x'"
    )
    assert note == "rate_limit: chatgpt"


def test_run_failure_no_log_no_signal_is_plain_exit(bundles):
    assert util.classify_run_failure("missing", 3) == "exit 3"
