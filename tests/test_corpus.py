"""Tests for the deterministic core of watchmen.corpus: the idempotent column
migrations and `_replace_session` (the UPSERT that every adapter scan funnels
through). These are the parts that, if they regress, silently corrupt or fail
to load the corpus — and they have no LLM in the loop, so they're cheap to pin
down exactly.
"""

from __future__ import annotations

import sqlite3

import pytest

from watchmen import corpus


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


# ─── tool_calls column migration (skill_name, cost_usd) ─────────────────────


def test_migrate_tool_calls_adds_missing_columns_to_legacy_db():
    conn = sqlite3.connect(":memory:")
    # A pre-v0.6.1 tool_calls table: no skill_name, no cost_usd.
    conn.executescript(
        "CREATE TABLE tool_calls (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "session_id TEXT, timestamp TEXT, tool_name TEXT, "
        "is_error INTEGER NOT NULL DEFAULT 0)"
    )
    assert "skill_name" not in _cols(conn, "tool_calls")
    corpus._migrate_tool_calls_columns(conn)
    cols = _cols(conn, "tool_calls")
    assert "skill_name" in cols and "cost_usd" in cols
    # Idempotent: a second pass on the now-current schema is a no-op.
    corpus._migrate_tool_calls_columns(conn)
    assert _cols(conn, "tool_calls") == cols


# ─── sessions column migration (file_mtime, agent) ──────────────────────────


def test_migrate_sessions_adds_columns_and_defaults_agent():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE sessions (session_id TEXT PRIMARY KEY, project_dir TEXT)"
    )
    conn.execute("INSERT INTO sessions (session_id, project_dir) VALUES ('s1', '/p')")
    corpus._migrate_sessions_columns(conn)
    cols = _cols(conn, "sessions")
    assert "file_mtime" in cols and "agent" in cols
    # Existing rows are tagged as claude_code (true for any pre-adapter corpus).
    assert conn.execute("SELECT agent FROM sessions WHERE session_id='s1'").fetchone()[0] == "claude_code"
    corpus._migrate_sessions_columns(conn)  # idempotent
    assert _cols(conn, "sessions") == cols


# ─── goals CHECK-constraint rebuild ─────────────────────────────────────────


def test_migrate_goals_rebuilds_when_check_is_stale():
    conn = sqlite3.connect(":memory:")
    # Old 4-status enum, missing 'blocked' and 'usage_limited'.
    conn.executescript(
        "CREATE TABLE goals (goal_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, "
        "project_dir TEXT, objective TEXT NOT NULL, "
        "status TEXT NOT NULL CHECK(status IN ('active','paused','budget_limited','complete')), "
        "token_budget INTEGER, tokens_used INTEGER NOT NULL DEFAULT 0, "
        "time_used_seconds INTEGER NOT NULL DEFAULT 0, created_at TEXT, updated_at TEXT, "
        "agent TEXT NOT NULL DEFAULT 'codex')"
    )
    conn.execute(
        "INSERT INTO goals (goal_id, thread_id, objective, status) VALUES ('g1','t1','do it','active')"
    )
    # The new status would be rejected by the old CHECK.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO goals (goal_id, thread_id, objective, status) VALUES ('g2','t2','x','blocked')"
        )

    corpus._migrate_goals_check_constraint(conn)

    # Rebuilt table accepts the new statuses and preserved the old row.
    conn.execute(
        "INSERT INTO goals (goal_id, thread_id, objective, status) VALUES ('g3','t3','x','usage_limited')"
    )
    assert conn.execute("SELECT objective FROM goals WHERE goal_id='g1'").fetchone()[0] == "do it"


def test_migrate_goals_noop_when_constraint_current():
    conn = sqlite3.connect(":memory:")
    conn.executescript(corpus._ENSURE_GOALS_TABLE)
    before = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='goals'"
    ).fetchone()[0]
    corpus._migrate_goals_check_constraint(conn)
    after = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='goals'"
    ).fetchone()[0]
    assert before == after  # untouched


# ─── _replace_session ───────────────────────────────────────────────────────


def _full_schema_conn() -> sqlite3.Connection:
    """A corpus connection with the canonical tables + the column migrations
    applied — exactly the shape init_db produces, but in memory."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(corpus._CREATE_TABLES)
    corpus._migrate_tool_calls_columns(conn)  # add cost_usd (not in the base CREATE)
    return conn


def _session(sid: str, **over) -> dict:
    base = {
        "session_id": sid, "project_dir": "/p", "transcript_path": f"/t/{sid}.jsonl",
        "file_mtime": 1.0, "started_at": "2026-05-01T10:00:00Z", "ended_at": None,
        "duration_seconds": None, "is_subagent": 0, "parent_session_id": None,
        "message_count": 0, "user_prompt_count": 0, "assistant_text_count": 0,
        "assistant_thinking_count": 0, "tool_use_count": 0, "tool_error_count": 0,
        "models": None, "input_tokens": 0, "cache_creation_tokens": 0,
        "cache_read_tokens": 0, "output_tokens": 0, "model_dominant": None,
        "cost_usd": 0.0, "agent": "claude_code",
    }
    base.update(over)
    return base


def _prompt(sid: str, text: str) -> dict:
    return {"session_id": sid, "timestamp": "2026-05-01T10:00:01Z", "text": text,
            "word_count": len(text.split()), "char_count": len(text), "is_first_in_session": 1}


def test_replace_session_inserts_session_prompts_and_tools():
    conn = _full_schema_conn()
    # A tool row WITHOUT cost_usd — _replace_session must default it to NULL,
    # not blow up on the missing named param.
    tools = [{"session_id": "s1", "timestamp": "2026-05-01T10:00:02Z",
              "tool_name": "Bash", "is_error": 0, "skill_name": None}]
    corpus._replace_session(conn, _session("s1"), [_prompt("s1", "hello world")], tools)

    assert conn.execute("SELECT COUNT(*) FROM sessions WHERE session_id='s1'").fetchone()[0] == 1
    assert conn.execute("SELECT text FROM prompts WHERE session_id='s1'").fetchone()[0] == "hello world"
    row = conn.execute("SELECT tool_name, cost_usd FROM tool_calls WHERE session_id='s1'").fetchone()
    assert row[0] == "Bash" and row[1] is None


def test_replace_session_replaces_children_on_reparse():
    conn = _full_schema_conn()
    corpus._replace_session(
        conn, _session("s1"), [_prompt("s1", "old prompt")],
        [{"session_id": "s1", "timestamp": "t", "tool_name": "Read", "is_error": 0, "skill_name": None}],
    )
    # Re-parse the same session with different content — old children must go.
    corpus._replace_session(
        conn, _session("s1", cost_usd=2.5), [_prompt("s1", "new prompt")],
        [{"session_id": "s1", "timestamp": "t", "tool_name": "Edit", "is_error": 1,
          "skill_name": "deploy", "cost_usd": 0.5}],
    )

    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    assert conn.execute("SELECT cost_usd FROM sessions WHERE session_id='s1'").fetchone()[0] == 2.5
    prompts = [r[0] for r in conn.execute("SELECT text FROM prompts WHERE session_id='s1'")]
    assert prompts == ["new prompt"]  # old prompt gone, not duplicated
    tools = conn.execute("SELECT tool_name, is_error, skill_name, cost_usd FROM tool_calls").fetchall()
    assert tools == [("Edit", 1, "deploy", 0.5)]


def test_replace_session_with_no_children_is_clean():
    conn = _full_schema_conn()
    corpus._replace_session(conn, _session("s1"), [], [])
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM prompts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0] == 0
