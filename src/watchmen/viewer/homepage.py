"""Aggregations powering the mission-control homepage.

Three buckets of data, all read-only against corpus.db + state.db:

  - `impact_strip()`  — three KPI cards (skill calls, tool errors/sess,
                        active repos) with this-7d vs prior-7d deltas.
  - `skill_leaderboard()` — top repos by `Skill` tool invocations in
                            window, with curated-skill count alongside.
  - `weekly_sparkline_data()` — last 12 weeks of skill calls + tool
                                errors-per-session for the trend strip.
  - `status_tiles()` — per-tracked-project health (healthy / stale /
                       uncurated), counts of skills + pending + last
                       activity. Drives the status grid.

Everything degrades to empty/None when corpus.db is absent — the
homepage is still useful on a fresh install with no captured sessions.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from watchmen.paths import BUNDLES_DIR, CORPUS_DB
from watchmen import state


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _conn_ro() -> sqlite3.Connection | None:
    """Open corpus.db read-only. Returns None when the file is missing."""
    if not CORPUS_DB.exists():
        return None
    c = sqlite3.connect(f"file:{CORPUS_DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def _pct_delta(this: float, prior: float) -> float | None:
    """Percent change, or None when the prior bucket is empty."""
    if not prior:
        return None
    return (this - prior) / prior * 100.0


# ─── 1. Impact strip — 3 KPI cards with week-over-week delta ────────────────

def impact_strip() -> dict:
    """Three this-7d vs prior-7d cards.

    Returns a dict with keys `skill_calls`, `tool_errors`, `active_repos`,
    each having {this, prior, delta_pct, fmt}.  `fmt` is a hint for the
    template — int for counts, float-1 for per-session means.

    All values fall back to zero when corpus.db is missing.  delta_pct
    is None when the prior week was zero (avoid divide-by-zero, render
    as a dash).
    """
    out = {
        "skill_calls":  {"this": 0,   "prior": 0,   "delta_pct": None, "fmt": "int"},
        "tool_errors":  {"this": 0.0, "prior": 0.0, "delta_pct": None, "fmt": "float1"},
        "active_repos": {"this": 0,   "prior": 0,   "delta_pct": None, "fmt": "int"},
    }
    cc = _conn_ro()
    if cc is None:
        return out
    try:
        now = _now()
        cutoff_7  = (now - timedelta(days=7)).isoformat()
        cutoff_14 = (now - timedelta(days=14)).isoformat()

        # Skill calls
        row = cc.execute(
            "SELECT "
            " SUM(CASE WHEN timestamp >= ? THEN 1 ELSE 0 END) AS this_, "
            " SUM(CASE WHEN timestamp >= ? AND timestamp < ? THEN 1 ELSE 0 END) AS prior_ "
            "FROM tool_calls WHERE tool_name = 'Skill' AND timestamp >= ?",
            (cutoff_7, cutoff_14, cutoff_7, cutoff_14),
        ).fetchone()
        out["skill_calls"]["this"]  = row["this_"]  or 0
        out["skill_calls"]["prior"] = row["prior_"] or 0
        out["skill_calls"]["delta_pct"] = _pct_delta(out["skill_calls"]["this"], out["skill_calls"]["prior"])

        # Tool errors per session (mean), over the same windows
        for label, start, end in [("this", cutoff_7, None), ("prior", cutoff_14, cutoff_7)]:
            sql = (
                "SELECT AVG(CAST(tool_error_count AS FLOAT)) AS m "
                "FROM sessions WHERE is_subagent = 0 AND started_at >= ?"
            )
            args: tuple = (start,)
            if end:
                sql += " AND started_at < ?"
                args = (start, end)
            v = cc.execute(sql, args).fetchone()["m"]
            out["tool_errors"][label] = float(v or 0.0)
        out["tool_errors"]["delta_pct"] = _pct_delta(out["tool_errors"]["this"], out["tool_errors"]["prior"])

        # Active repos (distinct project_dir with ≥1 session)
        for label, start, end in [("this", cutoff_7, None), ("prior", cutoff_14, cutoff_7)]:
            sql = (
                "SELECT COUNT(DISTINCT project_dir) AS n "
                "FROM sessions WHERE is_subagent = 0 AND started_at >= ?"
            )
            args = (start,)
            if end:
                sql += " AND started_at < ?"
                args = (start, end)
            out["active_repos"][label] = cc.execute(sql, args).fetchone()["n"] or 0
        out["active_repos"]["delta_pct"] = _pct_delta(out["active_repos"]["this"], out["active_repos"]["prior"])
    finally:
        cc.close()
    return out


# ─── 2. Skill leaderboard — top repos by Skill calls this week ──────────────

def skill_leaderboard(window_days: int = 7, limit: int = 8) -> list[dict]:
    """Top projects by `Skill` tool invocations in the last N days.

    Returns a list of {project_dir, project_name, project_key, count,
    skills_curated, max_count} sorted desc by count.  `max_count` is the
    leader's count, used by the template to render proportional bars
    without computing it in Jinja.
    """
    cc = _conn_ro()
    if cc is None:
        return []
    try:
        cutoff = (_now() - timedelta(days=window_days)).isoformat()
        rows = cc.execute(
            """
            SELECT s.project_dir AS project_dir, COUNT(*) AS n
            FROM tool_calls t
            JOIN sessions s ON s.session_id = t.session_id
            WHERE t.tool_name = 'Skill' AND t.timestamp >= ? AND s.is_subagent = 0
            GROUP BY s.project_dir ORDER BY n DESC LIMIT ?
            """,
            (cutoff, limit),
        ).fetchall()
    finally:
        cc.close()
    if not rows:
        return []

    # Map project_dir → curated-skills count by walking state.db once.
    state.init_db()
    projects = {p["source_repo"]: p for p in state.list_projects()}

    max_count = rows[0]["n"]
    out: list[dict] = []
    for r in rows:
        path = r["project_dir"] or ""
        proj = projects.get(path)
        name = proj["project_key"] if proj else (Path(path).name or "(unknown)")
        # Count curated skills via BUNDLES_DIR (watchmen's view).  Same
        # accounting as actions.py: a sub-directory under
        # bundles/<key>/skills/ counts as one curated skill.
        skills_n = 0
        if proj:
            skills_root = BUNDLES_DIR / proj["project_key"] / "skills"
            if skills_root.exists():
                skills_n = sum(1 for x in skills_root.iterdir() if x.is_dir())
        out.append({
            "project_dir": path,
            "project_name": name,
            "project_key": proj["project_key"] if proj else None,
            "count": r["n"],
            "skills_curated": skills_n,
            "max_count": max_count,
        })
    return out


# ─── 3. Weekly sparklines — last 12 weeks of skill calls + tool errors ──────

def weekly_sparkline_data(weeks: int = 12) -> dict:
    """Last N completed-or-current ISO weeks of two metrics.

    Returns
      { "skill_calls": [{date, value}, …],
        "tool_errors": [{date, value}, …] }
    The `date` for a week is the Monday of that ISO week, so the chart
    renders along a real time axis (not just labels).  Empty list when
    corpus.db is missing.
    """
    cc = _conn_ro()
    if cc is None:
        return {"skill_calls": [], "tool_errors": []}
    try:
        now = _now()
        # Anchor at the Monday of (now - weeks weeks). All buckets aligned to ISO weeks.
        anchor = now - timedelta(weeks=weeks)
        anchor_iso = anchor.isoformat()
        # Skill calls per week
        skill_rows = cc.execute(
            """
            SELECT strftime('%Y-%W', timestamp) AS yw, MIN(timestamp) AS first_ts, COUNT(*) AS n
            FROM tool_calls WHERE tool_name = 'Skill' AND timestamp >= ?
            GROUP BY yw ORDER BY yw
            """,
            (anchor_iso,),
        ).fetchall()
        # Tool errors per session, weekly mean
        err_rows = cc.execute(
            """
            SELECT strftime('%Y-%W', started_at) AS yw, MIN(started_at) AS first_ts,
                   AVG(CAST(tool_error_count AS FLOAT)) AS m
            FROM sessions WHERE is_subagent = 0 AND started_at >= ?
            GROUP BY yw ORDER BY yw
            """,
            (anchor_iso,),
        ).fetchall()
    finally:
        cc.close()

    def to_series(rs, key) -> list[dict]:
        # Use the first timestamp in the bucket as the label so the chart
        # has a real ISO date to plot against, not just "YYYY-WW".
        out = []
        for r in rs:
            ts = r["first_ts"] or ""
            iso_date = ts[:10] if ts else ""
            v = r[key] or 0
            out.append({"date": iso_date, "value": float(v) if isinstance(v, float) else int(v)})
        return out

    return {
        "skill_calls": to_series(skill_rows, "n"),
        "tool_errors": to_series(err_rows, "m"),
    }


# ─── 4. Status tiles — per-project health glance ────────────────────────────

def status_tiles() -> list[dict]:
    """Compact per-tracked-project status for the grid.

    Each tile carries:
      - project_key, source_repo
      - skills_n (curated SKILL.md files on disk)
      - pending_n (from state.skill_candidates)
      - sessions_7d (corpus.db count)
      - last_curator_run (state.projects.last_curator_run, ISO string or None)
      - status: 'healthy' | 'stale' | 'uncurated'
        * uncurated → 0 skills curated yet
        * stale     → last curator run > 30 days ago
        * healthy   → has skills and curator ran recently
    """
    state.init_db()
    projects = state.list_projects()
    cc = _conn_ro()
    sessions_by_path: dict[str, int] = {}
    if cc is not None:
        try:
            cutoff = (_now() - timedelta(days=7)).isoformat()
            rows = cc.execute(
                "SELECT project_dir, COUNT(*) AS n FROM sessions "
                "WHERE is_subagent = 0 AND started_at >= ? GROUP BY project_dir",
                (cutoff,),
            ).fetchall()
            sessions_by_path = {r["project_dir"]: r["n"] for r in rows if r["project_dir"]}
        finally:
            cc.close()

    out: list[dict] = []
    for p in projects:
        # Same accounting as actions._skills_count / _pending_count: count
        # sub-dirs under BUNDLES_DIR/<key>/{skills, _pending}.
        skills_root = BUNDLES_DIR / p["project_key"] / "skills"
        skills_n = sum(1 for x in skills_root.iterdir() if x.is_dir()) if skills_root.exists() else 0
        pending_root = BUNDLES_DIR / p["project_key"] / "_pending"
        pending_n = sum(1 for x in pending_root.iterdir() if x.is_dir()) if pending_root.exists() else 0

        last_run = p.get("last_curator_run")
        days_since_curator: int | None = None
        if last_run:
            try:
                ts = datetime.fromisoformat(last_run.replace("Z", "+00:00"))
                days_since_curator = (_now() - ts).days
            except ValueError:
                pass

        if skills_n == 0:
            status = "uncurated"
        elif days_since_curator is not None and days_since_curator > 30:
            status = "stale"
        else:
            status = "healthy"

        out.append({
            "project_key": p["project_key"],
            "source_repo": p["source_repo"],
            "skills_n": skills_n,
            "pending_n": pending_n,
            "sessions_7d": sessions_by_path.get(p["source_repo"], 0),
            "last_curator_run": last_run,
            "days_since_curator": days_since_curator,
            "status": status,
        })
    return out
