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
    assert by_slug["claude_code"] == "#3b82f6"  # blue-500
    assert by_slug["codex"] == "#06b6d4"        # cyan-500
    assert by_slug["pi"] == "#14b8a6"           # teal-500


# ─── agent_comparison_facts ──────────────────────────────────────────────


def _seed_comparison_corpus(
    corpus_path: Path,
    sessions: list[dict],
    tool_calls: list[dict] | None = None,
) -> None:
    """Schema variant for cross-agent comparison tests: adds the columns
    `agent_comparison_facts` reads beyond `_seed_corpus_with_tools` —
    duration_seconds and model_dominant — plus the tool_calls table."""
    schema = """
    CREATE TABLE sessions (
        session_id TEXT PRIMARY KEY,
        project_dir TEXT NOT NULL,
        started_at TEXT NOT NULL,
        duration_seconds REAL,
        user_prompt_count INTEGER NOT NULL DEFAULT 0,
        tool_use_count INTEGER NOT NULL DEFAULT 0,
        tool_error_count INTEGER NOT NULL DEFAULT 0,
        input_tokens INTEGER NOT NULL DEFAULT 0,
        cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
        cache_read_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        cost_usd REAL NOT NULL DEFAULT 0,
        is_subagent INTEGER NOT NULL DEFAULT 0,
        agent TEXT NOT NULL DEFAULT 'claude_code',
        model_dominant TEXT
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
        cols = (
            "session_id, project_dir, started_at, duration_seconds, "
            "user_prompt_count, tool_use_count, tool_error_count, "
            "input_tokens, cache_creation_tokens, cache_read_tokens, "
            "output_tokens, cost_usd, is_subagent, agent, model_dominant"
        )
        for s in sessions:
            row = {
                "duration_seconds": None,
                "tool_use_count": 0,
                "model_dominant": None,
                **s,
            }
            placeholders = ", ".join(f":{c.strip()}" for c in cols.split(","))
            conn.execute(
                f"INSERT INTO sessions ({cols}) VALUES ({placeholders})",
                row,
            )
        for t in tool_calls or []:
            conn.execute(
                "INSERT INTO tool_calls (session_id, tool_name) VALUES "
                "(:session_id, :tool_name)",
                t,
            )


def test_agent_comparison_facts_empty_when_no_corpus(fresh_metrics):
    """Missing corpus.db → empty shape, not an exception. The viewer
    template guards on `cmp_facts.adapters|length >= 2` so an empty
    dict here naturally hides the panel."""
    out = fresh_metrics.agent_comparison_facts(days=30)
    assert out["window_days"] == 30
    assert out["adapters"] == []


def test_agent_comparison_facts_derives_ratios_and_top_lists(fresh_metrics, tmp_path):
    """End-to-end: two adapters in window, each with sessions, tool
    calls, costs. Verify every derived ratio and that top_tools /
    top_projects are sorted desc within each adapter."""
    started_recent = (datetime.now() - timedelta(days=5)).isoformat()
    sessions = [
        # Claude Code: 2 sessions, 10 prompts, 100 tool calls, $5.
        {"session_id": "cc1", "project_dir": "/home/user/kai", "started_at": started_recent,
         "duration_seconds": 600.0, "user_prompt_count": 6, "tool_use_count": 70,
         "tool_error_count": 2, "input_tokens": 1000, "cache_creation_tokens": 0,
         "cache_read_tokens": 9000, "output_tokens": 500, "cost_usd": 3.0,
         "is_subagent": 0, "agent": "claude_code", "model_dominant": "claude-opus-4-7"},
        {"session_id": "cc2", "project_dir": "/home/user/kai", "started_at": started_recent,
         "duration_seconds": 1200.0, "user_prompt_count": 4, "tool_use_count": 30,
         "tool_error_count": 1, "input_tokens": 500, "cache_creation_tokens": 0,
         "cache_read_tokens": 4500, "output_tokens": 300, "cost_usd": 2.0,
         "is_subagent": 0, "agent": "claude_code", "model_dominant": "claude-opus-4-7"},
        # Codex: 1 session, 20 prompts, 50 tool calls, $0.40 — cheaper but more prompts.
        {"session_id": "cx1", "project_dir": "/home/user/other-repo", "started_at": started_recent,
         "duration_seconds": 1800.0, "user_prompt_count": 20, "tool_use_count": 50,
         "tool_error_count": 0, "input_tokens": 8000, "cache_creation_tokens": 0,
         "cache_read_tokens": 12000, "output_tokens": 2000, "cost_usd": 0.40,
         "is_subagent": 0, "agent": "codex", "model_dominant": "gpt-5.5"},
        # Subagent — must be excluded from the rollup.
        {"session_id": "sub1", "project_dir": "/home/user/kai", "started_at": started_recent,
         "duration_seconds": 60.0, "user_prompt_count": 99, "tool_use_count": 9999,
         "tool_error_count": 0, "input_tokens": 0, "cache_creation_tokens": 0,
         "cache_read_tokens": 0, "output_tokens": 0, "cost_usd": 99.0,
         "is_subagent": 1, "agent": "claude_code", "model_dominant": "claude-opus-4-7"},
    ]
    tool_calls = (
        [{"session_id": "cc1", "tool_name": "Bash"}] * 40 +
        [{"session_id": "cc1", "tool_name": "Read"}] * 20 +
        [{"session_id": "cc2", "tool_name": "Edit"}] * 25 +
        [{"session_id": "cx1", "tool_name": "exec_command"}] * 40 +
        [{"session_id": "cx1", "tool_name": "apply_patch"}] * 10
    )
    _seed_comparison_corpus(tmp_path / "corpus.db", sessions, tool_calls)

    out = fresh_metrics.agent_comparison_facts(days=30)
    adapters = {a["agent"]: a for a in out["adapters"]}
    assert set(adapters.keys()) == {"claude_code", "codex"}, \
        "subagent session leaked into the rollup or an adapter went missing"
    cc = adapters["claude_code"]
    cx = adapters["codex"]
    # Sessions / prompts / costs aggregate across both rows for cc.
    assert cc["sessions"] == 2
    assert cc["prompts"] == 10
    assert cc["tool_calls"] == 100
    assert cc["cost_usd"] == pytest.approx(5.0)
    # Derived: $/prompt = 5 / 10 = 0.5; tool_error_rate = 3 / 100 = 0.03;
    # cache hit = 13500 / (1500 + 13500) = 0.9.
    assert cc["cost_per_prompt"] == pytest.approx(0.5)
    assert cc["prompts_per_session"] == pytest.approx(5.0)
    assert cc["tool_calls_per_session"] == pytest.approx(50.0)
    assert cc["tool_error_rate"] == pytest.approx(0.03)
    assert cc["cache_hit_rate"] == pytest.approx(0.9)
    assert cc["dominant_model"] == "claude-opus-4-7"
    # Top tools ordered by count desc: Bash (40) > Edit (25) > Read (20).
    assert [t for t, _ in cc["top_tools"][:3]] == ["Bash", "Edit", "Read"]
    # Codex side: 20 prompts, 50 tool calls, cheap.
    assert cx["sessions"] == 1
    assert cx["cost_per_prompt"] == pytest.approx(0.40 / 20)
    assert cx["dominant_model"] == "gpt-5.5"
    assert cx["top_tools"][0] == ("exec_command", 40)


def test_agent_comparison_facts_window_excludes_old(fresh_metrics, tmp_path):
    """Sessions outside the requested window must drop out of the rollup
    — the LLM narrative is window-scoped and shouldn't be influenced by
    sessions older than the user asked for."""
    recent = (datetime.now() - timedelta(days=10)).isoformat()
    ancient = (datetime.now() - timedelta(days=200)).isoformat()
    _seed_comparison_corpus(tmp_path / "corpus.db", [
        {"session_id": "r1", "project_dir": "/p", "started_at": recent,
         "duration_seconds": 60.0, "user_prompt_count": 5, "tool_use_count": 1,
         "tool_error_count": 0, "input_tokens": 0, "cache_creation_tokens": 0,
         "cache_read_tokens": 0, "output_tokens": 0, "cost_usd": 1.0,
         "is_subagent": 0, "agent": "claude_code", "model_dominant": None},
        {"session_id": "a1", "project_dir": "/p", "started_at": ancient,
         "duration_seconds": 60.0, "user_prompt_count": 999, "tool_use_count": 0,
         "tool_error_count": 0, "input_tokens": 0, "cache_creation_tokens": 0,
         "cache_read_tokens": 0, "output_tokens": 0, "cost_usd": 999.0,
         "is_subagent": 0, "agent": "claude_code", "model_dominant": None},
    ])
    out = fresh_metrics.agent_comparison_facts(days=30)
    assert len(out["adapters"]) == 1
    assert out["adapters"][0]["prompts"] == 5
    assert out["adapters"][0]["cost_usd"] == pytest.approx(1.0)


def test_cross_agent_narrative_skips_when_lt_two_active_adapters():
    """The LLM narrative is None when fewer than 2 adapters have at
    least 10 prompts each — comparing one agent in isolation is what
    the profile card already does, so the narrative would be redundant
    AND wastes an LLM call."""
    from watchmen.commands.insights import _cross_agent_narrative
    # Single adapter — skip.
    facts_one = {"window_days": 90, "adapters": [
        {"agent": "claude_code", "label": "Claude Code", "prompts": 500,
         "sessions": 10, "active_days": 5, "projects": 2, "tool_calls": 100,
         "tool_errors": 1, "cost_usd": 10.0, "cost_per_prompt": 0.02,
         "cost_per_session": 1.0, "prompts_per_session": 50.0,
         "tool_calls_per_session": 10.0, "tool_error_rate": 0.01,
         "cache_hit_rate": 0.5, "avg_session_seconds": 600.0,
         "top_tools": [("Bash", 50)], "top_projects": [("/p", 10)],
         "dominant_model": "claude-opus-4-7", "suggestions_fired": 0},
    ]}
    assert _cross_agent_narrative(facts_one, model="ignored") is None
    # Two adapters but one has <10 prompts — skip (not enough to compare).
    facts_thin = {"window_days": 90, "adapters": facts_one["adapters"] + [
        {"agent": "codex", "label": "Codex", "prompts": 3,
         "sessions": 1, "active_days": 1, "projects": 1, "tool_calls": 5,
         "tool_errors": 0, "cost_usd": 0.05, "cost_per_prompt": 0.017,
         "cost_per_session": 0.05, "prompts_per_session": 3.0,
         "tool_calls_per_session": 5.0, "tool_error_rate": 0.0,
         "cache_hit_rate": 0.0, "avg_session_seconds": 60.0,
         "top_tools": [("exec_command", 5)], "top_projects": [("/q", 1)],
         "dominant_model": "gpt-5.5", "suggestions_fired": 0},
    ]}
    assert _cross_agent_narrative(facts_thin, model="ignored") is None


# ── _count_uptake: slash-command + auto-fire channels ──────────────────────

def _seed_corpus_for_uptake(corpus_path: Path, prompts: list[dict], tool_calls: list[dict]) -> None:
    """Minimal corpus.db with the two tables `_count_uptake` reads: `prompts`
    (slash-command channel) and `tool_calls` carrying `skill_name` (auto-fire
    channel)."""
    schema = """
    CREATE TABLE prompts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT,
        timestamp TEXT,
        text TEXT
    );
    CREATE TABLE tool_calls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT,
        timestamp TEXT,
        tool_name TEXT,
        is_error INTEGER NOT NULL DEFAULT 0,
        skill_name TEXT
    );
    """
    with sqlite3.connect(str(corpus_path)) as conn:
        conn.executescript(schema)
        for p in prompts:
            conn.execute(
                "INSERT INTO prompts (session_id, timestamp, text) VALUES (?, ?, ?)",
                (p["session_id"], p["timestamp"], p["text"]),
            )
        for t in tool_calls:
            conn.execute(
                "INSERT INTO tool_calls (session_id, timestamp, tool_name, skill_name) "
                "VALUES (?, ?, ?, ?)",
                (t["session_id"], t["timestamp"], t.get("tool_name", "Skill"), t.get("skill_name")),
            )


def _sugg(session_id: str, slug: str, ts: str) -> dict:
    return {"session_id": session_id, "skill_slug": slug, "ts": ts}


def test_uptake_slash_command_channel(fresh_metrics):
    """A later /<slug> prompt in the same session within an hour counts."""
    _seed_corpus_for_uptake(
        fresh_metrics.CORPUS_DB,
        prompts=[{"session_id": "s1", "timestamp": "2026-05-20T12:30:00.000Z",
                  "text": "let's run /portless-dev-server now"}],
        tool_calls=[],
    )
    n = fresh_metrics._count_uptake([_sugg("s1", "portless-dev-server", "2026-05-20T12:00:00")])
    assert n == 1


def test_uptake_autofire_channel(fresh_metrics):
    """A Skill tool call (auto-fire, no slash command) counts as uptake."""
    _seed_corpus_for_uptake(
        fresh_metrics.CORPUS_DB,
        prompts=[],
        tool_calls=[{"session_id": "s1", "timestamp": "2026-05-20T12:30:00.000Z",
                     "skill_name": "portless-dev-server"}],
    )
    n = fresh_metrics._count_uptake([_sugg("s1", "portless-dev-server", "2026-05-20T12:00:00")])
    assert n == 1


def test_uptake_autofire_namespace_stripped(fresh_metrics):
    """`watchmen:brief` auto-fire matches a `brief` suggestion."""
    _seed_corpus_for_uptake(
        fresh_metrics.CORPUS_DB,
        prompts=[],
        tool_calls=[{"session_id": "s1", "timestamp": "2026-05-20T12:10:00.000Z",
                     "skill_name": "watchmen:brief"}],
    )
    n = fresh_metrics._count_uptake([_sugg("s1", "brief", "2026-05-20T12:00:00")])
    assert n == 1


def test_uptake_autofire_outside_window_not_counted(fresh_metrics):
    """A Skill call more than an hour after the suggestion doesn't count."""
    _seed_corpus_for_uptake(
        fresh_metrics.CORPUS_DB,
        prompts=[],
        tool_calls=[{"session_id": "s1", "timestamp": "2026-05-20T13:30:00.000Z",
                     "skill_name": "portless-dev-server"}],
    )
    n = fresh_metrics._count_uptake([_sugg("s1", "portless-dev-server", "2026-05-20T12:00:00")])
    assert n == 0


def test_uptake_autofire_before_suggestion_not_counted(fresh_metrics):
    """A Skill call before the suggestion timestamp doesn't count."""
    _seed_corpus_for_uptake(
        fresh_metrics.CORPUS_DB,
        prompts=[],
        tool_calls=[{"session_id": "s1", "timestamp": "2026-05-20T11:30:00.000Z",
                     "skill_name": "portless-dev-server"}],
    )
    n = fresh_metrics._count_uptake([_sugg("s1", "portless-dev-server", "2026-05-20T12:00:00")])
    assert n == 0


def test_uptake_no_matching_channel_not_counted(fresh_metrics):
    """Neither a /<slug> prompt nor a matching Skill call → not taken."""
    _seed_corpus_for_uptake(
        fresh_metrics.CORPUS_DB,
        prompts=[{"session_id": "s1", "timestamp": "2026-05-20T12:30:00.000Z",
                  "text": "unrelated prompt"}],
        tool_calls=[{"session_id": "s1", "timestamp": "2026-05-20T12:30:00.000Z",
                     "skill_name": "some-other-skill"}],
    )
    n = fresh_metrics._count_uptake([_sugg("s1", "portless-dev-server", "2026-05-20T12:00:00")])
    assert n == 0


def test_uptake_both_channels_counts_suggestion_once(fresh_metrics):
    """When both channels fire for one suggestion, it still counts once."""
    _seed_corpus_for_uptake(
        fresh_metrics.CORPUS_DB,
        prompts=[{"session_id": "s1", "timestamp": "2026-05-20T12:20:00.000Z",
                  "text": "/portless-dev-server"}],
        tool_calls=[{"session_id": "s1", "timestamp": "2026-05-20T12:30:00.000Z",
                     "skill_name": "portless-dev-server"}],
    )
    n = fresh_metrics._count_uptake([_sugg("s1", "portless-dev-server", "2026-05-20T12:00:00")])
    assert n == 1
