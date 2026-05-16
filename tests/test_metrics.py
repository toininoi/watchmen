"""Tests for watchmen.metrics aggregation helpers.

Focused on `adapter_breakdown_all` — the cross-project per-agent rollup
that powers the viewer's "By coding agent" section and the CLI's per-agent
table. Builds a fixture corpus.db with sessions across three agents and
asserts the returned shape matches.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from watchmen import metrics as _metrics


@pytest.fixture
def fresh_metrics(tmp_path: Path, monkeypatch):
    """Redirect the metrics module's CORPUS_DB constant to a temp file
    using monkeypatch.setattr. This avoids the sys.modules reloading dance
    (and the cross-test contamination it caused) — when the test ends,
    monkeypatch restores the real CORPUS_DB automatically."""
    corpus_path = tmp_path / "corpus.db"
    monkeypatch.setattr(_metrics, "CORPUS_DB", corpus_path)
    return _metrics


def _seed_corpus(corpus_path: Path, rows: list[dict]) -> None:
    """Write a minimal corpus.db sessions table with just the columns
    `adapter_breakdown_all` reads. Keeps the fixture tiny."""
    schema = """
    CREATE TABLE sessions (
        session_id TEXT PRIMARY KEY,
        project_dir TEXT NOT NULL,
        started_at TEXT NOT NULL,
        user_prompt_count INTEGER NOT NULL DEFAULT 0,
        tool_error_count INTEGER NOT NULL DEFAULT 0,
        input_tokens INTEGER NOT NULL DEFAULT 0,
        cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
        cache_read_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        cost_usd REAL NOT NULL DEFAULT 0,
        is_subagent INTEGER NOT NULL DEFAULT 0,
        agent TEXT NOT NULL DEFAULT 'claude_code'
    );
    """
    cols = (
        "session_id, project_dir, started_at, user_prompt_count, "
        "tool_error_count, input_tokens, cache_creation_tokens, "
        "cache_read_tokens, output_tokens, cost_usd, is_subagent, agent"
    )
    with sqlite3.connect(str(corpus_path)) as conn:
        conn.executescript(schema)
        for r in rows:
            conn.execute(
                f"INSERT INTO sessions ({cols}) VALUES "
                f"(:session_id, :project_dir, :started_at, "
                f":user_prompt_count, :tool_error_count, "
                f":input_tokens, :cache_creation_tokens, :cache_read_tokens, "
                f":output_tokens, :cost_usd, :is_subagent, :agent)",
                r,
            )


def _row(session_id, *, agent, project_dir, days_ago=0, prompts=0,
         tool_errors=0, cost=0.0, is_subagent=0):
    started = (datetime.now() - timedelta(days=days_ago)).isoformat()
    return {
        "session_id": session_id,
        "project_dir": project_dir,
        "started_at": started,
        "user_prompt_count": prompts,
        "tool_error_count": tool_errors,
        "input_tokens": 0,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
        "output_tokens": 0,
        "cost_usd": cost,
        "is_subagent": is_subagent,
        "agent": agent,
    }


def test_no_corpus_db_returns_empty(fresh_metrics):
    """No corpus.db on disk → empty list, not an exception."""
    assert fresh_metrics.adapter_breakdown_all() == []


def test_per_agent_rollup_orders_by_session_count(fresh_metrics, tmp_path):
    _seed_corpus(tmp_path / "corpus.db", [
        _row("s1", agent="claude_code", project_dir="/p1", prompts=10, tool_errors=2, cost=1.50),
        _row("s2", agent="claude_code", project_dir="/p2", prompts=4, cost=0.50),
        _row("s3", agent="codex", project_dir="/p1", prompts=3, tool_errors=1),
        _row("s4", agent="pi", project_dir="/p3", prompts=2),
        _row("s5", agent="pi", project_dir="/p3", prompts=1),
    ])
    rows = fresh_metrics.adapter_breakdown_all(days=30)

    assert [r["agent"] for r in rows] == ["claude_code", "pi", "codex"]
    cc = rows[0]
    assert cc["label"] == "Claude Code"
    assert cc["sessions"] == 2
    assert cc["projects"] == 2
    assert cc["prompts"] == 14
    assert cc["tool_errors"] == 2
    assert cc["cost_usd"] == pytest.approx(2.00)

    pi = rows[1]
    assert pi["label"] == "pi.dev"
    assert pi["sessions"] == 2
    assert pi["projects"] == 1


def test_subagent_sessions_excluded(fresh_metrics, tmp_path):
    """is_subagent=1 rows are noise from the curator's tool-calling agents,
    not user-driven sessions. They must not appear in the rollup."""
    _seed_corpus(tmp_path / "corpus.db", [
        _row("s1", agent="claude_code", project_dir="/p1"),
        _row("sub1", agent="claude_code", project_dir="/p1", is_subagent=1),
        _row("sub2", agent="claude_code", project_dir="/p1", is_subagent=1),
    ])
    rows = fresh_metrics.adapter_breakdown_all(days=30)
    assert len(rows) == 1
    assert rows[0]["sessions"] == 1


def test_window_cutoff_excludes_old_sessions(fresh_metrics, tmp_path):
    """Sessions older than `days` should not be counted. Default window is
    30 days; anything beyond drops out."""
    _seed_corpus(tmp_path / "corpus.db", [
        _row("recent", agent="claude_code", project_dir="/p1", days_ago=5),
        _row("ancient", agent="codex", project_dir="/p1", days_ago=120),
    ])
    rows = fresh_metrics.adapter_breakdown_all(days=30)
    assert [r["agent"] for r in rows] == ["claude_code"]


def test_unknown_agent_falls_through_to_raw_slug(fresh_metrics, tmp_path):
    """A future adapter slug not in ADAPTER_LABELS should still render — we
    just show the slug verbatim instead of crashing."""
    _seed_corpus(tmp_path / "corpus.db", [
        _row("s1", agent="cursor", project_dir="/p1"),
    ])
    rows = fresh_metrics.adapter_breakdown_all(days=30)
    assert rows[0]["label"] == "cursor"
