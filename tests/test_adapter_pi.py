"""Tests for watchmen.adapters.pi — the pi.dev (Pi Coding Agent) parser.

Focused on real-data quirks validated against a v0.74.0 session: pi carries
the tool-error flag at the MESSAGE level, not inside content blocks.
"""

import json
import tempfile
from pathlib import Path

from watchmen.adapters import pi


def _write(path: Path, lines: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")


def _entry(path: Path) -> dict:
    return {
        "path": path,
        "project_dir": None,
        "is_subagent": False,
        "parent_session_id": None,
    }


def test_scan_uses_native_cost_total_over_price_table():
    """pi's `usage.cost.total` is authoritative (e.g. OpenRouter/`:free` models
    our price table doesn't know). The adapter must use it, not recompute. Uses
    an unpriced model so a price-table recompute would be 0/wrong, and asserts
    the native total surfaces as the session cost."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "s.jsonl"
        _write(p, [
            {"type": "session", "version": 3, "id": "s1",
             "timestamp": "2026-05-15T10:00:00.000Z", "cwd": "/p"},
            {"type": "message", "id": "u1", "parentId": "s1",
             "timestamp": "2026-05-15T10:00:01.000Z",
             "message": {"role": "user", "content": "go"}},
            {"type": "message", "id": "a1", "parentId": "u1",
             "timestamp": "2026-05-15T10:00:02.000Z",
             "message": {"role": "assistant", "model": "tencent/hy3-preview:free",
                         "content": [{"type": "text", "text": "ok"}],
                         "usage": {"input": 1000, "output": 1000,
                                   "cost": {"input": 0.1, "output": 0.3, "total": 0.42}}}},
        ])
        session, _, _ = pi.scan(_entry(p))
        assert session["cost_usd"] == 0.42, session["cost_usd"]


def test_scan_native_cost_zero_is_respected_not_recomputed():
    """A native total of 0.0 (free models) is valid and must be used as-is,
    not treated as 'missing' and recomputed."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "s.jsonl"
        _write(p, [
            {"type": "session", "version": 3, "id": "s1",
             "timestamp": "2026-05-15T10:00:00.000Z", "cwd": "/p"},
            {"type": "message", "id": "a1", "parentId": "s1",
             "timestamp": "2026-05-15T10:00:02.000Z",
             "message": {"role": "assistant", "model": "claude-3-5-sonnet",
                         "content": [{"type": "text", "text": "ok"}],
                         "usage": {"input": 1000, "output": 1000, "cost": {"total": 0.0}}}},
        ])
        session, _, _ = pi.scan(_entry(p))
        assert session["cost_usd"] == 0.0, session["cost_usd"]


def test_scan_falls_back_to_price_table_without_native_cost():
    """When `usage` has no `cost` object, recompute via the price table."""
    from watchmen.metrics import turn_cost_usd
    expect = turn_cost_usd("claude-3-5-sonnet", 1000, 0, 0, 0, 1000)
    assert expect > 0
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "s.jsonl"
        _write(p, [
            {"type": "session", "version": 3, "id": "s1",
             "timestamp": "2026-05-15T10:00:00.000Z", "cwd": "/p"},
            {"type": "message", "id": "a1", "parentId": "s1",
             "timestamp": "2026-05-15T10:00:02.000Z",
             "message": {"role": "assistant", "model": "claude-3-5-sonnet",
                         "content": [{"type": "text", "text": "ok"}],
                         "usage": {"input": 1000, "output": 1000}}},
        ])
        session, _, _ = pi.scan(_entry(p))
        assert session["cost_usd"] == expect, session["cost_usd"]


def test_scan_counts_toolresult_error_at_message_level():
    """pi marks a failed tool call with `message.isError: true` on the
    toolResult message itself — NOT via an `isError` block inside content.
    The previous block-scan never matched, so every pi tool error went
    uncounted. Build a session whose only toolResult is a message-level
    error and assert it is counted once."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "sess.jsonl"
        _write(p, [
            {"type": "session", "version": 3, "id": "s1",
             "timestamp": "2026-05-15T10:00:00.000Z", "cwd": "/proj"},
            {"type": "message", "id": "u1", "parentId": "s1",
             "timestamp": "2026-05-15T10:00:01.000Z",
             "message": {"role": "user", "content": "read a dir"}},
            {"type": "message", "id": "a1", "parentId": "u1",
             "timestamp": "2026-05-15T10:00:02.000Z",
             "message": {"role": "assistant", "model": "m",
                         "content": [{"type": "toolCall", "name": "read",
                                      "arguments": {"path": "/proj"}}],
                         "usage": {"input": 1, "output": 1}}},
            # error flag is message-level, content blocks carry no flag
            {"type": "message", "id": "t1", "parentId": "a1",
             "timestamp": "2026-05-15T10:00:03.000Z",
             "message": {"role": "toolResult", "toolName": "read",
                         "content": [{"type": "text", "text": "EISDIR: is a directory"}],
                         "isError": True}},
        ])
        session, _, _ = pi.scan(_entry(p))
        assert session["tool_error_count"] == 1, session["tool_error_count"]


def test_scan_does_not_count_successful_toolresult():
    """A toolResult with no error flag (message- or block-level) must not
    inflate tool_error_count."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "sess.jsonl"
        _write(p, [
            {"type": "session", "version": 3, "id": "s1",
             "timestamp": "2026-05-15T10:00:00.000Z", "cwd": "/proj"},
            {"type": "message", "id": "u1", "parentId": "s1",
             "timestamp": "2026-05-15T10:00:01.000Z",
             "message": {"role": "user", "content": "read a file"}},
            {"type": "message", "id": "a1", "parentId": "u1",
             "timestamp": "2026-05-15T10:00:02.000Z",
             "message": {"role": "assistant", "model": "m",
                         "content": [{"type": "toolCall", "name": "read",
                                      "arguments": {"path": "/proj/x"}}],
                         "usage": {"input": 1, "output": 1}}},
            {"type": "message", "id": "t1", "parentId": "a1",
             "timestamp": "2026-05-15T10:00:03.000Z",
             "message": {"role": "toolResult", "toolName": "read",
                         "content": [{"type": "text", "text": "ok"}]}},
        ])
        session, _, _ = pi.scan(_entry(p))
        assert session["tool_error_count"] == 0
