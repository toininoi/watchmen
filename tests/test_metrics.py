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
    using monkeypatch.setattr — and inside `compute_card_stats`, the
    BUNDLES_DIR import is re-resolved against an empty temp dir so the
    mastery axis doesn't read the developer's real bundles directory.
    monkeypatch restores both on teardown."""
    corpus_path = tmp_path / "corpus.db"
    bundles_path = tmp_path / "bundles"
    bundles_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(_metrics, "CORPUS_DB", corpus_path)
    # `compute_card_stats` does `from watchmen.paths import BUNDLES_DIR`
    # at call time, so the patch has to target watchmen.paths rather than
    # the metrics module.
    from watchmen import paths as _paths
    monkeypatch.setattr(_paths, "BUNDLES_DIR", bundles_path)
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


# ─── compute_card_stats ──────────────────────────────────────────────────


def _seed_corpus_with_tools(corpus_path: Path, sessions: list[dict], tool_calls: list[dict]) -> None:
    """Extended fixture: sessions + tool_calls tables. The card metric
    pulls distinct tool names from tool_calls, so we need both."""
    schema = """
    CREATE TABLE sessions (
        session_id TEXT PRIMARY KEY,
        project_dir TEXT NOT NULL,
        started_at TEXT NOT NULL,
        user_prompt_count INTEGER NOT NULL DEFAULT 0,
        tool_use_count INTEGER NOT NULL DEFAULT 0,
        tool_error_count INTEGER NOT NULL DEFAULT 0,
        input_tokens INTEGER NOT NULL DEFAULT 0,
        cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
        cache_read_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        cost_usd REAL NOT NULL DEFAULT 0,
        is_subagent INTEGER NOT NULL DEFAULT 0,
        agent TEXT NOT NULL DEFAULT 'claude_code'
    );
    CREATE TABLE tool_calls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT,
        timestamp TEXT,
        tool_name TEXT,
        is_error INTEGER NOT NULL DEFAULT 0
    );
    """
    with sqlite3.connect(str(corpus_path)) as conn:
        conn.executescript(schema)
        for s in sessions:
            conn.execute(
                "INSERT INTO sessions (session_id, project_dir, started_at, "
                "user_prompt_count, tool_use_count, tool_error_count, "
                "input_tokens, cache_creation_tokens, cache_read_tokens, "
                "output_tokens, cost_usd, is_subagent, agent) VALUES "
                "(:session_id, :project_dir, :started_at, "
                ":user_prompt_count, :tool_use_count, :tool_error_count, "
                ":input_tokens, :cache_creation_tokens, :cache_read_tokens, "
                ":output_tokens, :cost_usd, :is_subagent, :agent)",
                s,
            )
        for t in tool_calls:
            conn.execute(
                "INSERT INTO tool_calls (session_id, timestamp, tool_name, is_error) "
                "VALUES (:session_id, :timestamp, :tool_name, :is_error)",
                t,
            )


def _ses(session_id, *, agent="claude_code", project_dir="/p", days_ago=0,
         prompts=0, tool_uses=0, tool_errors=0, cost=0.0, is_subagent=0):
    return {
        "session_id": session_id,
        "project_dir": project_dir,
        "started_at": (datetime.now() - timedelta(days=days_ago)).isoformat(),
        "user_prompt_count": prompts,
        "tool_use_count": tool_uses,
        "tool_error_count": tool_errors,
        "input_tokens": 0, "cache_creation_tokens": 0,
        "cache_read_tokens": 0, "output_tokens": 0,
        "cost_usd": cost, "is_subagent": is_subagent, "agent": agent,
    }


def _tc(session_id, *, tool, days_ago=0, is_error=0):
    return {
        "session_id": session_id,
        "timestamp": (datetime.now() - timedelta(days=days_ago)).isoformat(),
        "tool_name": tool,
        "is_error": is_error,
    }


def test_card_stats_newcomer_when_corpus_empty(fresh_metrics):
    """No corpus.db / no sessions → Newcomer archetype, rating floor 40,
    every axis at zero. Card still renders so even a fresh install has
    something to look at."""
    s = fresh_metrics.compute_card_stats(days=90)
    assert s["rating"] == 40
    assert s["archetype"][0] == "Newcomer"
    assert all(v == 0.0 for v in s["axes"].values())
    assert s["sessions"] == 0
    assert s["top_agent"] is None and s["top_tool"] is None


def test_card_stats_rating_in_range_and_archetype_set(fresh_metrics, tmp_path):
    """A moderate corpus produces a non-floor rating and picks a real
    archetype (not Newcomer). Specific number is implementation-defined;
    just verify the contract: 40 ≤ rating ≤ 99 and archetype isn't None."""
    _seed_corpus_with_tools(
        tmp_path / "corpus.db",
        sessions=[
            _ses("s1", project_dir="/p1", days_ago=2, prompts=20, tool_uses=40, tool_errors=2, cost=0.50),
            _ses("s2", project_dir="/p2", days_ago=5, prompts=15, tool_uses=30, tool_errors=1, cost=0.30),
            _ses("s3", project_dir="/p3", days_ago=8, prompts=10, tool_uses=20, tool_errors=0, cost=0.20),
        ],
        tool_calls=[
            _tc("s1", tool="Read"), _tc("s1", tool="Edit"), _tc("s1", tool="Bash"),
            _tc("s2", tool="Read"), _tc("s2", tool="Grep"),
            _tc("s3", tool="Write"),
        ],
    )
    s = fresh_metrics.compute_card_stats(days=90)
    assert 40 <= s["rating"] <= 99
    assert s["archetype"][0] != "Newcomer"
    assert s["sessions"] == 3
    assert s["axes_raw"]["curiosity"] == 5  # Read, Edit, Bash, Grep, Write
    assert s["axes_raw"]["range"] == 3      # /p1, /p2, /p3
    assert s["top_tool"][0] == "Read" and s["top_tool"][1] == 2


def test_card_stats_dominant_axis_picks_specific_archetype(fresh_metrics, tmp_path):
    """When one axis dominates by ≥1.25× the runner-up AND ≥0.4 absolute,
    the corresponding archetype label is used. Seed a corpus with the
    range axis maxed (12 projects = cap) and zero tool activity so no
    other axis competes — should land cleanly on Polyglot."""
    sessions = [
        _ses(f"s{i}", project_dir=f"/p{i}", days_ago=i, prompts=1, tool_uses=0)
        for i in range(12)
    ]
    _seed_corpus_with_tools(tmp_path / "corpus.db", sessions=sessions, tool_calls=[])
    s = fresh_metrics.compute_card_stats(days=90)
    assert s["archetype"][0] == "Polyglot", f"got {s['archetype']!r}"
    assert s["axes_raw"]["range"] == 12


def test_card_stats_subagent_sessions_excluded(fresh_metrics, tmp_path):
    """Subagent sessions are curator-internal noise and must not inflate
    user-facing stats. Same exclusion rule as adapter_breakdown_all."""
    _seed_corpus_with_tools(
        tmp_path / "corpus.db",
        sessions=[
            _ses("real", project_dir="/p", days_ago=1, prompts=5),
            _ses("sub", project_dir="/p", days_ago=1, prompts=100, is_subagent=1),
        ],
        tool_calls=[],
    )
    s = fresh_metrics.compute_card_stats(days=90)
    assert s["sessions"] == 1
    assert s["prompts"] == 5


def test_card_svg_renders_axis_labels(fresh_metrics, tmp_path):
    """The SVG is now spider-chart-only (header + footer moved into
    HTML in metrics_all.html). Verify each axis label still appears
    in the rendered string; the polygon + tinted rings have no
    inspectable text but at least we catch the case where a label
    or spoke regresses out of the output."""
    _seed_corpus_with_tools(
        tmp_path / "corpus.db",
        sessions=[_ses("s1", project_dir="/p", days_ago=1, prompts=5, tool_uses=5)],
        tool_calls=[_tc("s1", tool="Read")],
    )
    s = fresh_metrics.compute_card_stats(days=90)
    svg = fresh_metrics.card_svg(s)
    assert svg.startswith("<svg") and svg.endswith("</svg>")
    for axis in ("THROUGHPUT", "FRUGALITY", "RELIABILITY", "CURIOSITY", "RANGE", "MASTERY"):
        assert axis in svg, f"axis label {axis} missing from card SVG"
    # The 4 tinted ring polygons (red → orange → yellow → green).
    assert svg.count("<polygon") >= 5  # 4 rings + 1 user polygon


def test_card_attribute_groups_and_traits_populate(fresh_metrics, tmp_path):
    """`compute_card_stats` returns the data the FM-style profile card
    needs: three column groups (Volume / Efficiency / Breadth) with
    color-coded stat items, and a non-empty traits list. This is the
    contract the metrics_all.html profile section depends on."""
    _seed_corpus_with_tools(
        tmp_path / "corpus.db",
        sessions=[
            _ses("s1", project_dir="/p1", days_ago=2, prompts=20, tool_uses=40, cost=1.0),
            _ses("s2", project_dir="/p2", days_ago=4, prompts=10, tool_uses=20, cost=0.5),
            _ses("s3", project_dir="/p3", days_ago=6, prompts=5, tool_uses=10),
        ],
        tool_calls=[_tc("s1", tool="Read"), _tc("s2", tool="Edit"), _tc("s3", tool="Bash")],
    )
    s = fresh_metrics.compute_card_stats(days=90)
    groups = s["attribute_groups"]
    assert [g["label"] for g in groups] == ["Volume", "Efficiency", "Breadth"]
    # Every stat item is a dict with the contract the template iterates.
    for g in groups:
        assert g["stats"], f"empty {g['label']!r} column"
        for item in g["stats"]:
            assert set(item.keys()) == {"label", "raw", "kind"}
            assert item["kind"] in {"elite", "mid", "low", "neutral"}
    # Traits is a list of strings, at least one trait present.
    assert isinstance(s["traits"], list) and s["traits"]
    assert all(isinstance(t, str) for t in s["traits"])


def test_card_tier_colors_match_rating_bands(fresh_metrics):
    """card_tier_colors picks gold/silver/bronze/indigo from rating.
    Smoke-tests the band edges (89 → silver, 90 → gold, etc.)."""
    assert fresh_metrics.card_tier_colors(95)["name"] == "gold"
    assert fresh_metrics.card_tier_colors(90)["name"] == "gold"
    assert fresh_metrics.card_tier_colors(89)["name"] == "silver"
    assert fresh_metrics.card_tier_colors(80)["name"] == "silver"
    assert fresh_metrics.card_tier_colors(79)["name"] == "bronze"
    assert fresh_metrics.card_tier_colors(70)["name"] == "bronze"
    assert fresh_metrics.card_tier_colors(40)["name"] == "indigo"


def test_agent_donut_svg_segments_and_empty(fresh_metrics):
    """agent_donut_svg renders one <path> per non-zero agent and a muted
    fallback when the input is empty. Center text shows the total +
    'sessions' label so the chart reads without a separate caption."""
    svg = fresh_metrics.agent_donut_svg({"claude_code": 6, "codex": 4, "pi": 0})
    assert svg.count("<path") == 2, "zero-count agents should be skipped"
    assert "10" in svg or "SESSIONS" in svg, "total + label missing from center"

    empty = fresh_metrics.agent_donut_svg({})
    assert "no data" in empty
    assert "<path" not in empty


def test_agent_donut_legend_orders_by_share_and_assigns_colors(fresh_metrics):
    """Legend rows come back sorted by count desc, with friendly labels
    and a stable color per known adapter. Unknown adapters cycle through
    a fallback palette so future agents render without a code change."""
    rows = fresh_metrics.agent_donut_legend({"claude_code": 5, "codex": 12, "pi": 3})
    assert [r["slug"] for r in rows] == ["codex", "claude_code", "pi"]
    assert rows[0]["label"] == "Codex"
    assert rows[0]["share"] == pytest.approx(12 / 20)
    # Known-agent colors are fixed.
    by_slug = {r["slug"]: r["color"] for r in rows}
    assert by_slug["claude_code"] == "#6366f1"
    assert by_slug["codex"] == "#0891b2"
    assert by_slug["pi"] == "#a855f7"
