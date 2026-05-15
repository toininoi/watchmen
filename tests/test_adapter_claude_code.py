"""Tests for watchmen.adapters.claude_code — the Claude Code transcript parser.

Smoke had zero coverage on this adapter even though it powers the majority
of watchmen's corpus. We exercise the public `scan()` surface with synthetic
JSONL transcripts and validate the session/prompt/tool_call shapes flowing
into corpus.db.
"""

import json
import tempfile
from pathlib import Path

from watchmen.adapters import claude_code


# ─── _parse_iso ────────────────────────────────────────────────────────────


def test_parse_iso_handles_z_suffix():
    """`_parse_iso` must normalize Anthropic's `…Z` ISO timestamps. Without
    the Z→+00:00 fix, datetime.fromisoformat raises and duration_seconds
    silently stays None for every session."""
    dt = claude_code._parse_iso("2026-05-15T10:30:00.123Z")
    assert dt is not None
    assert dt.year == 2026 and dt.month == 5 and dt.day == 15
    assert dt.tzinfo is not None, "Z must produce a tz-aware datetime"


def test_parse_iso_returns_none_on_empty_or_invalid():
    """Empty/None/garbage strings must round-trip to None rather than crash
    the scanner — corpus.db tolerates duration_seconds=NULL but a TypeError
    here aborts the whole project ingest."""
    assert claude_code._parse_iso(None) is None
    assert claude_code._parse_iso("") is None
    assert claude_code._parse_iso("not-a-timestamp") is None


# ─── _resolve cache ────────────────────────────────────────────────────────


def test_resolve_caches_decoded_paths(monkeypatch):
    """`_resolve` is called once per encoded dir during discover() — the
    cache prevents repeated FS walks. Validate the second call short-circuits."""
    calls = []
    def fake_decode(encoded):
        calls.append(encoded)
        return f"/decoded/{encoded}"

    monkeypatch.setattr(claude_code, "decode_project_dir", fake_decode)
    monkeypatch.setattr(claude_code, "_DECODE_CACHE", {})

    a = claude_code._resolve("encoded-1")
    b = claude_code._resolve("encoded-1")
    assert a == b == "/decoded/encoded-1"
    assert calls == ["encoded-1"], f"expected single decode call, got {calls}"


# ─── scan() ────────────────────────────────────────────────────────────────


def _write_transcript(path: Path, lines: list[dict]) -> None:
    """Write a list of dicts as JSONL — mirrors Claude Code's transcript shape."""
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")


def _entry(path: Path) -> dict:
    """Build the `entry` dict that `discover()` would yield for `path`."""
    return {
        "path": path,
        "project_dir": "/test/project",
        "is_subagent": False,
        "parent_session_id": None,
    }


def test_scan_parses_minimal_user_assistant_pair():
    """A single user prompt + assistant response should produce: 1 prompt,
    correct user_prompt_count + assistant_text_count, models populated,
    started_at/ended_at + duration computed from the timestamps."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "session-abc.jsonl"
        _write_transcript(p, [
            {
                "timestamp": "2026-05-15T10:00:00Z",
                "type": "user",
                "message": {"content": "hello world"},
            },
            {
                "timestamp": "2026-05-15T10:00:30Z",
                "type": "assistant",
                "message": {
                    "model": "claude-opus-4-7",
                    "content": [{"type": "text", "text": "hi back"}],
                    "usage": {"input_tokens": 100, "output_tokens": 50},
                },
            },
        ])
        session, prompts, tool_calls = claude_code.scan(_entry(p))

        assert session["session_id"] == "session-abc"
        assert session["project_dir"] == "/test/project"
        assert session["user_prompt_count"] == 1
        assert session["assistant_text_count"] == 1
        assert session["agent"] == "claude_code"
        assert session["duration_seconds"] == 30.0
        assert session["input_tokens"] == 100
        assert session["output_tokens"] == 50
        assert session["model_dominant"] == "claude-opus-4-7"
        assert "claude-opus-4-7" in session["models"]

        assert len(prompts) == 1
        assert prompts[0]["text"] == "hello world"
        assert prompts[0]["word_count"] == 2
        assert prompts[0]["is_first_in_session"] == 1

        assert tool_calls == []


def test_scan_extracts_text_blocks_skips_tool_result_only_messages():
    """User messages can carry either a string OR a list of content blocks. A
    list with only tool_result blocks is NOT a real user prompt — it's the
    transcript echoing tool output back. Skipping these prevents corpus.db
    from being polluted with auto-echoed tool results as 'user prompts'."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "session.jsonl"
        _write_transcript(p, [
            # Real prompt with text blocks → counts as a user prompt.
            {
                "timestamp": "2026-05-15T10:00:00Z",
                "type": "user",
                "message": {"content": [
                    {"type": "text", "text": "please run ls"},
                ]},
            },
            # Tool-result-only message → does NOT count as a prompt.
            {
                "timestamp": "2026-05-15T10:00:10Z",
                "type": "user",
                "message": {"content": [
                    {"type": "tool_result", "tool_use_id": "x", "content": "file1\nfile2"},
                ]},
            },
        ])
        session, prompts, _ = claude_code.scan(_entry(p))

        assert session["user_prompt_count"] == 1
        assert len(prompts) == 1
        assert prompts[0]["text"] == "please run ls"


def test_scan_counts_tool_errors_via_is_error_flag():
    """Tool errors surface in tool_result blocks via `is_error: true`. The
    adapter counts them into `tool_error_count` — that field is what powers
    the friction-signal chart in the viewer and the doctor's "session
    quality" verdict."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "session.jsonl"
        _write_transcript(p, [
            {
                "timestamp": "2026-05-15T10:00:00Z",
                "type": "user",
                "message": {"content": "do thing"},
            },
            {
                "timestamp": "2026-05-15T10:00:01Z",
                "type": "user",
                "message": {"content": [
                    {"type": "tool_result", "is_error": True, "content": "oops"},
                    {"type": "tool_result", "is_error": False, "content": "ok"},
                    {"type": "tool_result", "is_error": True, "content": "boom"},
                ]},
            },
        ])
        session, _, _ = claude_code.scan(_entry(p))
        assert session["tool_error_count"] == 2


def test_scan_records_tool_use_in_assistant_messages():
    """Assistant tool_use blocks bump `tool_use_count` AND emit per-call rows
    into `tool_calls` for downstream attribution. Without these, the viewer's
    "tools used" column is always zero even for tool-heavy sessions."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "session.jsonl"
        _write_transcript(p, [
            {
                "timestamp": "2026-05-15T10:00:00Z",
                "type": "user",
                "message": {"content": "list files"},
            },
            {
                "timestamp": "2026-05-15T10:00:01Z",
                "type": "assistant",
                "message": {
                    "model": "claude-sonnet-4-6",
                    "content": [
                        {"type": "text", "text": "Let me check."},
                        {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
                        {"type": "tool_use", "name": "Read", "input": {"file_path": "/x"}},
                    ],
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            },
        ])
        session, _, tool_calls = claude_code.scan(_entry(p))

        assert session["tool_use_count"] == 2
        assert session["assistant_text_count"] == 1
        assert {tc["tool_name"] for tc in tool_calls} == {"Bash", "Read"}
        for tc in tool_calls:
            assert tc["session_id"] == "session"
            assert tc["is_error"] == 0


def test_scan_dominant_model_picks_highest_output_tokens():
    """When a session crosses models mid-conversation, `model_dominant`
    should pick the one with the most assistant output tokens — that's the
    one users will recognize as "the model that wrote my code"."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "session.jsonl"
        _write_transcript(p, [
            {
                "timestamp": "2026-05-15T10:00:00Z",
                "type": "assistant",
                "message": {
                    "model": "claude-sonnet-4-6",
                    "content": [{"type": "text", "text": "small response"}],
                    "usage": {"input_tokens": 0, "output_tokens": 100},
                },
            },
            {
                "timestamp": "2026-05-15T10:00:01Z",
                "type": "assistant",
                "message": {
                    "model": "claude-opus-4-7",
                    "content": [{"type": "text", "text": "big response"}],
                    "usage": {"input_tokens": 0, "output_tokens": 5000},
                },
            },
        ])
        session, _, _ = claude_code.scan(_entry(p))
        assert session["model_dominant"] == "claude-opus-4-7"
        models = json.loads(session["models"])
        assert "claude-opus-4-7" in models
        assert "claude-sonnet-4-6" in models


def test_scan_tolerates_malformed_jsonl_lines():
    """One bad line in a transcript must not abort the whole session — the
    user has thousands of these and a partial parse is better than a
    midnight-daemon crash. Bad lines get silently skipped."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "session.jsonl"
        p.write_text(
            json.dumps({
                "timestamp": "2026-05-15T10:00:00Z",
                "type": "user",
                "message": {"content": "valid"},
            }) + "\n"
            "this-is-not-json{{{\n"
            "\n"  # blank line
            + json.dumps({
                "timestamp": "2026-05-15T10:00:01Z",
                "type": "assistant",
                "message": {
                    "model": "x",
                    "content": [{"type": "text", "text": "also valid"}],
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            }) + "\n"
        )
        session, prompts, _ = claude_code.scan(_entry(p))
        assert session["user_prompt_count"] == 1
        assert session["assistant_text_count"] == 1
        assert len(prompts) == 1


def test_scan_handles_cache_creation_buckets():
    """Anthropic splits cache_creation_input_tokens by TTL bucket
    (ephemeral_5m vs ephemeral_1h). The adapter must read both and combine
    them into cache_creation_tokens — otherwise the cost math under-counts
    on long-running sessions that mix both bucket types."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "session.jsonl"
        _write_transcript(p, [
            {
                "timestamp": "2026-05-15T10:00:00Z",
                "type": "assistant",
                "message": {
                    "model": "claude-sonnet-4-6",
                    "content": [{"type": "text", "text": "."}],
                    "usage": {
                        "input_tokens": 0,
                        "cache_creation_input_tokens": 1000,
                        "cache_creation": {
                            "ephemeral_5m_input_tokens": 600,
                            "ephemeral_1h_input_tokens": 400,
                        },
                        "output_tokens": 50,
                    },
                },
            },
        ])
        session, _, _ = claude_code.scan(_entry(p))
        assert session["cache_creation_tokens"] == 1000


def test_scan_subagent_entry_threads_parent_session_id():
    """Subagent transcripts under <parent>/subagents/ must carry their parent
    session id forward — that's how the viewer's session graph attributes
    child invocations back to the main conversation."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "sub.jsonl"
        _write_transcript(p, [{"timestamp": "2026-05-15T10:00:00Z", "type": "user", "message": {"content": "x"}}])
        entry = {
            "path": p,
            "project_dir": "/test/project",
            "is_subagent": True,
            "parent_session_id": "parent-abc",
        }
        session, _, _ = claude_code.scan(entry)
        assert session["is_subagent"] == 1
        assert session["parent_session_id"] == "parent-abc"
