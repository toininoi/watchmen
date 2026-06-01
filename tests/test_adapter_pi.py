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
