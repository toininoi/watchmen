"""Tests for watchmen.adapters.opencode — the OpenCode SQLite-store parser.

OpenCode 1.x stores sessions in ~/.local/share/opencode/opencode.db (Drizzle/
SQLite), not per-session JSON. These tests build a synthetic fixture DB that
mirrors the real `session` / `message` / `part` schema (columns the adapter
reads) and exercise discover() + scan(): session-level cost/tokens from the
session row, prompts from user text parts, the first-class `skill` tool, and
per-skill cost accrual (#94).
"""

import json
import sqlite3
from pathlib import Path

from watchmen.adapters import opencode

_MS = 1_700_000_000_000  # base epoch-ms


def _make_db(path: Path, *, session: dict, messages: list[dict], parts: list[dict]) -> None:
    """Create a minimal opencode-shaped DB with the columns the adapter reads."""
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE session (
          id TEXT PRIMARY KEY, project_id TEXT, parent_id TEXT, directory TEXT,
          agent TEXT, model TEXT, cost REAL DEFAULT 0,
          tokens_input INTEGER DEFAULT 0, tokens_output INTEGER DEFAULT 0,
          tokens_reasoning INTEGER DEFAULT 0, tokens_cache_read INTEGER DEFAULT 0,
          tokens_cache_write INTEGER DEFAULT 0,
          time_created INTEGER, time_updated INTEGER
        );
        CREATE TABLE message (
          id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER,
          time_updated INTEGER, data TEXT
        );
        CREATE TABLE part (
          id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
          time_created INTEGER, time_updated INTEGER, data TEXT
        );
        """
    )
    s = {"project_id": "p", "parent_id": None, "directory": "/proj", "agent": "build",
         "model": "anthropic/claude-sonnet-4-6", "cost": 0.0, "tokens_input": 0,
         "tokens_output": 0, "tokens_reasoning": 0, "tokens_cache_read": 0,
         "tokens_cache_write": 0, "time_created": _MS, "time_updated": _MS, **session}
    conn.execute(
        "INSERT INTO session (id, project_id, parent_id, directory, agent, model, cost, "
        "tokens_input, tokens_output, tokens_reasoning, tokens_cache_read, tokens_cache_write, "
        "time_created, time_updated) VALUES "
        "(:id,:project_id,:parent_id,:directory,:agent,:model,:cost,:tokens_input,:tokens_output,"
        ":tokens_reasoning,:tokens_cache_read,:tokens_cache_write,:time_created,:time_updated)", s,
    )
    for m in messages:
        conn.execute(
            "INSERT INTO message (id, session_id, time_created, time_updated, data) "
            "VALUES (?,?,?,?,?)",
            (m["id"], m["session_id"], m["time_created"], m["time_created"], json.dumps(m["data"])),
        )
    for i, p in enumerate(parts):
        conn.execute(
            "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) "
            "VALUES (?,?,?,?,?,?)",
            (f"prt_{i}", p["message_id"], p["session_id"], p["time_created"], p["time_created"],
             json.dumps(p["data"])),
        )
    conn.commit()
    conn.close()


def _scan_only(db: Path, sid: str):
    """Run discover() against `db`, then scan the entry for session `sid`."""
    orig = opencode.DB_PATH
    opencode.DB_PATH = db
    try:
        entries = {e["session_id"]: e for e in opencode.discover()}
        return opencode.scan(entries[sid]), entries[sid]
    finally:
        opencode.DB_PATH = orig


def test_discover_skips_when_db_absent(tmp_path):
    orig = opencode.DB_PATH
    opencode.DB_PATH = tmp_path / "nope.db"
    try:
        assert list(opencode.discover()) == []
    finally:
        opencode.DB_PATH = orig


def test_scan_parses_session_with_skill_cost_accrual(tmp_path):
    """Full path: cost/tokens from the session row, the first-class skill tool,
    per-skill cost accrual (a skill opens a span; later messages accrue until
    the next genuine user prompt; the activating message is not charged)."""
    db = tmp_path / "opencode.db"
    sid = "ses1"
    messages = [
        {"id": "m1", "session_id": sid, "time_created": _MS + 1,
         "data": {"role": "user", "time": {"created": _MS + 1}}},
        # activating message: invokes the skill tool (cost NOT charged to deploy)
        {"id": "m2", "session_id": sid, "time_created": _MS + 2,
         "data": {"role": "assistant", "modelID": "anthropic/claude-sonnet-4-6", "cost": 0.01}},
        # in-span working message -> accrues to deploy
        {"id": "m3", "session_id": sid, "time_created": _MS + 3,
         "data": {"role": "assistant", "modelID": "anthropic/claude-sonnet-4-6", "cost": 0.05}},
        # genuine follow-up prompt ends the span
        {"id": "m4", "session_id": sid, "time_created": _MS + 4,
         "data": {"role": "user", "time": {"created": _MS + 4}}},
        # post-span message -> NOT attributed to deploy
        {"id": "m5", "session_id": sid, "time_created": _MS + 5,
         "data": {"role": "assistant", "modelID": "anthropic/claude-sonnet-4-6", "cost": 0.02}},
    ]
    parts = [
        {"message_id": "m1", "session_id": sid, "time_created": _MS + 1,
         "data": {"type": "text", "text": "deploy the thing"}},
        {"message_id": "m2", "session_id": sid, "time_created": _MS + 2,
         "data": {"type": "tool", "tool": "skill", "callID": "c1",
                  "state": {"status": "completed", "input": {"skillId": "deploy"},
                            "output": "<skill_content name=deploy>"}}},
        {"message_id": "m3", "session_id": sid, "time_created": _MS + 3,
         "data": {"type": "text", "text": "doing the deploy"}},
        {"message_id": "m4", "session_id": sid, "time_created": _MS + 4,
         "data": {"type": "text", "text": "now run the tests"}},
        {"message_id": "m5", "session_id": sid, "time_created": _MS + 5,
         "data": {"type": "text", "text": "tests passed"}},
    ]
    _make_db(db, session={"id": sid, "cost": 0.08, "tokens_input": 450,
                          "tokens_output": 820, "tokens_cache_read": 12,
                          "tokens_cache_write": 34, "time_updated": _MS + 5},
             messages=messages, parts=parts)

    (session, prompts, tool_calls), _entry = _scan_only(db, sid)

    # session-level rollups come from the session row (opencode's own numbers)
    assert session["agent"] == "opencode"
    assert session["project_dir"] == "/proj"
    assert session["cost_usd"] == 0.08
    assert session["input_tokens"] == 450
    assert session["output_tokens"] == 820
    assert session["cache_read_tokens"] == 12
    assert session["cache_creation_tokens"] == 34
    assert session["user_prompt_count"] == 2
    assert session["assistant_text_count"] == 2  # m3 + m5 (m2 carries the skill tool, no text)
    assert session["tool_use_count"] == 1
    assert session["duration_seconds"] is not None
    assert prompts[0]["text"] == "deploy the thing"

    skill_rows = [t for t in tool_calls if t.get("skill_name") == "deploy"]
    assert len(skill_rows) == 1
    assert skill_rows[0]["tool_name"] == "skill"
    # only the one in-span working message (0.05), not the activating or post-span
    assert skill_rows[0]["cost_usd"] == 0.05


def test_scan_marks_subagent_and_tool_error(tmp_path):
    """parent_id => is_subagent; a tool part with state.status='error' counts."""
    db = tmp_path / "opencode.db"
    sid = "ses_child"
    messages = [
        {"id": "m1", "session_id": sid, "time_created": _MS + 1,
         "data": {"role": "assistant", "modelID": "x", "cost": 0.0}},
    ]
    parts = [
        {"message_id": "m1", "session_id": sid, "time_created": _MS + 1,
         "data": {"type": "tool", "tool": "bash", "callID": "c1",
                  "state": {"status": "error", "input": {}, "error": "boom"}}},
    ]
    _make_db(db, session={"id": sid, "parent_id": "ses_parent"},
             messages=messages, parts=parts)

    (session, _prompts, tool_calls), _entry = _scan_only(db, sid)
    assert session["is_subagent"] == 1
    assert session["parent_session_id"] == "ses_parent"
    assert session["tool_error_count"] == 1
    assert tool_calls[0]["is_error"] == 1
    assert tool_calls[0]["skill_name"] is None
