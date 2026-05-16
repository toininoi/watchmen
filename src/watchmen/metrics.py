"""Daily efficiency metrics for a tracked project.

Joins three data sources to build per-day rollups for the metrics viewer + CLI:

  - corpus.db.sessions     → sessions, prompts, tool errors, token usage by date
  - ~/.watchmen/suggestions.jsonl → suggestions fired, with session_id
  - prompts table          → uptake detection (did /<skill> appear in a later
                              prompt within the same session within 1 hour?)

Cost is computed from model→price table. Prices are fetched from OpenRouter
API (https://openrouter.ai/api/v1/models) and cached locally.
Falls back to hardcoded defaults if API is unavailable.
"""

import json
import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

from watchmen.paths import CORPUS_DB, STATE_DB
from watchmen.model_prices import price_for_model as price_for_model_from_api, turn_cost_usd as turn_cost_usd_from_api

ROOT = Path(__file__).parent
SUGGESTIONS_LOG = Path.home() / ".watchmen" / "suggestions.jsonl"

# Friendly labels for the `agent` column in corpus.db. The DB stores adapter
# slugs (claude_code, codex, pi); UI surfaces want display names. Fall through
# to the raw slug for unknown adapters so future ones still render.
ADAPTER_LABELS = {
    "claude_code": "Claude Code",
    "codex": "Codex",
    "pi": "pi.dev",
}


def adapter_label(slug: str) -> str:
    return ADAPTER_LABELS.get(slug, slug)

# Default price used when model is unknown (matches sonnet-4.6 hardcoded fallback)
DEFAULT_PRICE = (3.00, 3.75, 6.00, 0.30, 15.00)

# Backward-compatible alias for tests (hardcoded fallback prices)
MODEL_PRICES = {
    "opus-4.7": (5.00, 6.25, 10.00, 0.50, 25.00),
    "opus-4.6": (5.00, 6.25, 10.00, 0.50, 25.00),
    "opus-4.5": (5.00, 6.25, 10.00, 0.50, 25.00),
    "opus-4.1": (15.00, 18.75, 30.00, 1.50, 75.00),
    "opus-4": (15.00, 18.75, 30.00, 1.50, 75.00),
    "sonnet-4.6": (3.00, 3.75, 6.00, 0.30, 15.00),
    "sonnet-4.5": (3.00, 3.75, 6.00, 0.30, 15.00),
    "sonnet-4": (3.00, 3.75, 6.00, 0.30, 15.00),
    "haiku-4.5": (1.00, 1.25, 2.00, 0.10, 5.00),
    "haiku-3.5": (0.80, 1.00, 1.60, 0.08, 4.00),
    "gpt-5.5": (1.25, 1.25, 1.25, 0.125, 10.00),
    "gpt-5.4": (1.25, 1.25, 1.25, 0.125, 10.00),
    "gpt-5-mini": (0.25, 0.25, 0.25, 0.025, 2.00),
    "gpt-5": (1.25, 1.25, 1.25, 0.125, 10.00),
    "gpt-4.1": (2.00, 2.00, 2.00, 0.500, 8.00),
    "gpt-4o": (2.50, 2.50, 2.50, 1.250, 10.00),
    "o3": (2.00, 2.00, 2.00, 0.500, 8.00),
    "o4-mini": (1.10, 1.10, 1.10, 0.275, 4.40),
}


_VERSION_DASH = re.compile(r"(opus|sonnet|haiku)-(\d+)-(\d+)\b")
# GPT model names use dots too (gpt-5.5, gpt-4.1) but API also returns dash forms
# in some cases (gpt-5-5-mini). Same normalizer pattern:
_GPT_DASH = re.compile(r"gpt-(\d+)-(\d+)\b")


def price_for_model(model: str | None) -> tuple[float, float, float, float, float]:
    """Get price for a model. Uses model_prices module which fetches from OpenRouter
    API and caches locally. Falls back to family-based matching if not found.
    Same normalization as before for compatibility."""
    return price_for_model_from_api(model)


def turn_cost_usd(
    model: str | None,
    input_tokens: int,
    cache_creation_5m: int,
    cache_creation_1h: int,
    cache_read: int,
    output_tokens: int,
) -> float:
    """Cost for one assistant turn, in USD. Per-turn attribution means we use
    THIS turn's model — not the session's dominant model — which matters when
    a session spans multiple models (e.g. Opus for planning, Sonnet for grunt
    work)."""
    return turn_cost_usd_from_api(
        model,
        input_tokens,
        cache_creation_5m,
        cache_creation_1h,
        cache_read,
        output_tokens,
    )


def _project_dir_for_key(project_key: str) -> str | None:
    """Map a project_key to its corpus.db project_dir.

    Post-normalization, all adapters store the real cwd (matching state.db's
    source_repo verbatim), so this is just a lookup. Returns None if the
    project isn't tracked or the state DB doesn't exist yet."""
    if not CORPUS_DB.exists():
        return None
    if not STATE_DB.exists():
        return None
    try:
        with sqlite3.connect(str(STATE_DB)) as conn:
            row = conn.execute(
                "SELECT source_repo FROM projects WHERE project_key = ?", (project_key,)
            ).fetchone()
    except sqlite3.Error:
        return None
    if not row or not row[0]:
        return None
    return row[0]


def _local_date(iso_ts: str | None) -> str | None:
    if not iso_ts:
        return None
    try:
        # corpus stores ISO 8601 in UTC (with Z suffix sometimes)
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        return dt.astimezone().date().isoformat()
    except (ValueError, TypeError):
        return None


def daily_metrics(project_key: str, days: int = 30) -> list[dict]:
    """Return list of dicts, one per day in the last `days` days (newest first).
    Days with no activity still appear with zeroed counters."""
    project_dir = _project_dir_for_key(project_key)
    if not project_dir or not CORPUS_DB.exists():
        return []

    # 1. Pull sessions for this project from corpus, group by local date.
    with sqlite3.connect(str(CORPUS_DB)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT session_id, started_at, ended_at, user_prompt_count,
                      tool_error_count, input_tokens, cache_creation_tokens,
                      cache_read_tokens, output_tokens, model_dominant, cost_usd
               FROM sessions
               WHERE project_dir = ? AND is_subagent = 0""",
            (project_dir,),
        ).fetchall()

    by_day: dict[str, dict] = {}
    for r in rows:
        d_str = _local_date(r["started_at"])
        if not d_str:
            continue
        bucket = by_day.setdefault(d_str, _empty_bucket(d_str))
        bucket["sessions"] += 1
        bucket["prompts"] += r["user_prompt_count"] or 0
        bucket["tool_errors"] += r["tool_error_count"] or 0
        bucket["input_tokens"] += r["input_tokens"] or 0
        bucket["cache_creation_tokens"] += r["cache_creation_tokens"] or 0
        bucket["cache_read_tokens"] += r["cache_read_tokens"] or 0
        bucket["output_tokens"] += r["output_tokens"] or 0
        # cost is per-turn-summed at scan time; use the stored value if present.
        bucket["cost_usd"] += (r["cost_usd"] if r["cost_usd"] is not None else 0.0)

    # 2. Pull suggestions for this project from the log, group by local date.
    suggestions = _load_suggestions(project_key)
    for s in suggestions:
        d_str = _local_date(s["ts"])
        if not d_str:
            continue
        bucket = by_day.setdefault(d_str, _empty_bucket(d_str))
        bucket["suggestions_fired"] += 1
        bucket["_suggestion_records"].append(s)

    # 3. Compute uptake per bucket.
    for bucket in by_day.values():
        recs = bucket.pop("_suggestion_records", [])
        if recs:
            bucket["uptake"] = _count_uptake(recs)
            bucket["uptake_rate"] = (
                bucket["uptake"] / bucket["suggestions_fired"]
                if bucket["suggestions_fired"] > 0
                else 0.0
            )

    # 4. Backfill any missing days in the window with zeros so charts look right.
    today = date.today()
    cutoff = today - timedelta(days=days - 1)
    out: list[dict] = []
    for d in (cutoff + timedelta(days=i) for i in range(days)):
        d_str = d.isoformat()
        out.append(by_day.get(d_str, _empty_bucket(d_str)))
    out.sort(key=lambda b: b["date"], reverse=True)
    return out


def _empty_bucket(d_str: str) -> dict:
    return {
        "date": d_str,
        "sessions": 0,
        "prompts": 0,
        "tool_errors": 0,
        "input_tokens": 0,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0.0,
        "suggestions_fired": 0,
        "uptake": 0,
        "uptake_rate": 0.0,
        "_suggestion_records": [],
    }


def _load_suggestions(project_key: str) -> list[dict]:
    if not SUGGESTIONS_LOG.exists():
        return []
    out = []
    with SUGGESTIONS_LOG.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("project_key") == project_key:
                out.append(rec)
    return out


def _count_uptake(suggestions: list[dict]) -> int:
    """For each suggestion, count it as 'taken' if the same skill_slug appears
    as a slash command in any prompt within the same session, after the
    suggestion's timestamp, within 1 hour."""
    if not suggestions or not CORPUS_DB.exists():
        return 0

    session_ids = {s["session_id"] for s in suggestions if s.get("session_id")}
    if not session_ids:
        return 0

    with sqlite3.connect(str(CORPUS_DB)) as conn:
        conn.row_factory = sqlite3.Row
        placeholders = ",".join("?" for _ in session_ids)
        rows = conn.execute(
            f"SELECT session_id, timestamp, text FROM prompts "
            f"WHERE session_id IN ({placeholders}) ORDER BY timestamp ASC",
            tuple(session_ids),
        ).fetchall()
    by_session: dict[str, list[tuple[str, str]]] = {}
    for r in rows:
        by_session.setdefault(r["session_id"], []).append((r["timestamp"], r["text"] or ""))

    taken = 0
    for s in suggestions:
        sid = s.get("session_id")
        slug = s.get("skill_slug")
        ts_str = s.get("ts")
        if not (sid and slug and ts_str):
            continue
        try:
            ts = datetime.fromisoformat(ts_str)
        except ValueError:
            continue
        prompts = by_session.get(sid, [])
        # Find later prompts in the same session
        pattern = re.compile(rf"/(?:watchmen:)?{re.escape(slug)}\b")
        for p_ts_str, p_text in prompts:
            p_ts = _parse_any_ts(p_ts_str)
            if not p_ts or p_ts <= ts:
                continue
            if (p_ts - ts).total_seconds() > 3600:
                continue
            if pattern.search(p_text):
                taken += 1
                break
    return taken


def _parse_any_ts(s: str | None):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, TypeError):
        return None


def summarize_window(rows: list[dict], days: int) -> dict:
    """Roll up a slice of the daily rows into an N-day total."""
    slice_ = rows[:days]
    return {
        "days": days,
        "sessions": sum(r["sessions"] for r in slice_),
        "prompts": sum(r["prompts"] for r in slice_),
        "tool_errors": sum(r["tool_errors"] for r in slice_),
        "input_tokens": sum(r["input_tokens"] for r in slice_),
        "cache_creation_tokens": sum(r["cache_creation_tokens"] for r in slice_),
        "cache_read_tokens": sum(r["cache_read_tokens"] for r in slice_),
        "output_tokens": sum(r["output_tokens"] for r in slice_),
        "cost_usd": sum(r["cost_usd"] for r in slice_),
        "suggestions_fired": sum(r["suggestions_fired"] for r in slice_),
        "uptake": sum(r["uptake"] for r in slice_),
    }


def _tracked_project_dirs() -> list[str]:
    """Real-path project_dirs for every tracked project. Used to filter the
    aggregated views down to 'tracked only' when the toggle is on. Matches
    the post-normalization adapter convention (real cwd, not encoded)."""
    if not STATE_DB.exists():
        return []
    try:
        with sqlite3.connect(str(STATE_DB)) as conn:
            rows = conn.execute(
                "SELECT source_repo FROM projects WHERE source_repo IS NOT NULL"
            ).fetchall()
    except sqlite3.Error:
        return []
    return [r[0] for r in rows if r[0]]


def daily_metrics_all(days: int = 30, tracked_only: bool = False) -> list[dict]:
    """Aggregated daily metrics across every is_subagent=0 session in the corpus.
    Set tracked_only=True to restrict to projects in state.db. Suggestions come
    from the log (which only has tracked projects, so they're unchanged either way)."""
    today = date.today()
    cutoff = today - timedelta(days=days - 1)
    by_day: dict[str, dict] = {}
    for i in range(days):
        d_str = (cutoff + timedelta(days=i)).isoformat()
        by_day[d_str] = _empty_bucket(d_str)

    if CORPUS_DB.exists():
        with sqlite3.connect(str(CORPUS_DB)) as conn:
            conn.row_factory = sqlite3.Row
            base_sql = """SELECT session_id, started_at, user_prompt_count, tool_error_count,
                                  input_tokens, cache_creation_tokens, cache_read_tokens,
                                  output_tokens, model_dominant, cost_usd
                           FROM sessions
                           WHERE is_subagent = 0
                             AND date(started_at, 'localtime') >= ?"""
            params: list = [cutoff.isoformat()]
            if tracked_only:
                tracked_dirs = _tracked_project_dirs()
                if not tracked_dirs:
                    rows = []
                else:
                    placeholders = ",".join("?" for _ in tracked_dirs)
                    base_sql += f" AND project_dir IN ({placeholders})"
                    params.extend(tracked_dirs)
                    rows = conn.execute(base_sql, params).fetchall()
            else:
                rows = conn.execute(base_sql, params).fetchall()
        for r in rows:
            d_str = _local_date(r["started_at"])
            if not d_str or d_str not in by_day:
                continue
            b = by_day[d_str]
            b["sessions"] += 1
            b["prompts"] += r["user_prompt_count"] or 0
            b["tool_errors"] += r["tool_error_count"] or 0
            b["input_tokens"] += r["input_tokens"] or 0
            b["cache_creation_tokens"] += r["cache_creation_tokens"] or 0
            b["cache_read_tokens"] += r["cache_read_tokens"] or 0
            b["output_tokens"] += r["output_tokens"] or 0
            b["cost_usd"] += (r["cost_usd"] if r["cost_usd"] is not None else 0.0)

    # Suggestions log — already partitioned by project_key, but we sum all.
    if SUGGESTIONS_LOG.exists():
        with SUGGESTIONS_LOG.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                d_str = _local_date(rec.get("ts"))
                if not d_str or d_str not in by_day:
                    continue
                by_day[d_str]["suggestions_fired"] += 1
                by_day[d_str]["_suggestion_records"].append(rec)
        for b in by_day.values():
            recs = b.pop("_suggestion_records", [])
            if recs:
                b["uptake"] = _count_uptake(recs)
                b["uptake_rate"] = (
                    b["uptake"] / b["suggestions_fired"] if b["suggestions_fired"] > 0 else 0.0
                )

    for b in by_day.values():
        b.pop("_suggestion_records", None)

    out = list(by_day.values())
    out.sort(key=lambda b: b["date"], reverse=True)
    return out


def adapter_breakdown_all(days: int = 30, tracked_only: bool = False) -> list[dict]:
    """Per-coding-agent rollup over the last `days` calendar days.

    Returns one row per `agent` value in corpus.db.sessions, ordered by
    session count desc. Surfaces are the viewer's /metrics page and the
    CLI's `By coding agent` table.

    Note: distinct from `util.adapter_breakdown(project_key)`, which does a
    per-project quick lookup. This is the global cross-project rollup with
    cost / errors / projects-touched alongside session counts.

    Empty list when corpus.db doesn't exist yet or no sessions in window.
    """
    if not CORPUS_DB.exists():
        return []
    cutoff = date.today() - timedelta(days=days - 1)
    base_sql = """
        SELECT agent,
               COUNT(*)                       AS sessions,
               COUNT(DISTINCT project_dir)    AS projects,
               COALESCE(SUM(user_prompt_count), 0)     AS prompts,
               COALESCE(SUM(tool_error_count), 0)      AS tool_errors,
               COALESCE(SUM(input_tokens), 0)          AS input_tokens,
               COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens,
               COALESCE(SUM(cache_read_tokens), 0)     AS cache_read_tokens,
               COALESCE(SUM(output_tokens), 0)         AS output_tokens,
               COALESCE(SUM(cost_usd), 0.0)            AS cost_usd
          FROM sessions
         WHERE is_subagent = 0
           AND date(started_at, 'localtime') >= ?
    """
    params: list = [cutoff.isoformat()]
    if tracked_only:
        tracked_dirs = _tracked_project_dirs()
        if not tracked_dirs:
            return []
        placeholders = ",".join("?" for _ in tracked_dirs)
        base_sql += f" AND project_dir IN ({placeholders})"
        params.extend(tracked_dirs)
    base_sql += " GROUP BY agent ORDER BY sessions DESC, agent ASC"
    out: list[dict] = []
    with sqlite3.connect(str(CORPUS_DB)) as conn:
        conn.row_factory = sqlite3.Row
        for r in conn.execute(base_sql, params).fetchall():
            out.append({
                "agent": r["agent"],
                "label": adapter_label(r["agent"]),
                "sessions": r["sessions"],
                "projects": r["projects"],
                "prompts": r["prompts"],
                "tool_errors": r["tool_errors"],
                "input_tokens": r["input_tokens"],
                "cache_creation_tokens": r["cache_creation_tokens"],
                "cache_read_tokens": r["cache_read_tokens"],
                "output_tokens": r["output_tokens"],
                "cost_usd": r["cost_usd"],
            })
    return out


def activity_calendar_all(weeks: int = 26, tracked_only: bool = False) -> list[tuple[str, int]]:
    """Calendar across all activity, optionally restricted to tracked projects."""
    if not CORPUS_DB.exists():
        return []
    today = date.today()
    to_sunday = (today.weekday() + 1) % 7
    end = today
    start = today - timedelta(days=(weeks * 7 - 1 - to_sunday))

    sql = """SELECT date(p.timestamp, 'localtime') AS d, COUNT(*) AS n
             FROM prompts p JOIN sessions s ON p.session_id = s.session_id
             WHERE s.is_subagent = 0
               AND date(p.timestamp, 'localtime') >= ?
               AND date(p.timestamp, 'localtime') <= ?"""
    params: list = [start.isoformat(), end.isoformat()]
    if tracked_only:
        tracked_dirs = _tracked_project_dirs()
        if not tracked_dirs:
            return [((start + timedelta(days=i)).isoformat(), 0) for i in range(weeks * 7) if start + timedelta(days=i) <= end]
        placeholders = ",".join("?" for _ in tracked_dirs)
        sql += f" AND s.project_dir IN ({placeholders})"
        params.extend(tracked_dirs)
    sql += " GROUP BY date(p.timestamp, 'localtime')"

    with sqlite3.connect(str(CORPUS_DB)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
    counts = {r["d"]: r["n"] for r in rows}
    out = []
    for i in range(weeks * 7):
        d = start + timedelta(days=i)
        if d > end:
            break
        out.append((d.isoformat(), counts.get(d.isoformat(), 0)))
    return out


def activity_by_hour_dow_all(days: int = 90, tracked_only: bool = False) -> list[list[int]]:
    """Hour-of-day × day-of-week heatmap, optionally restricted to tracked projects."""
    if not CORPUS_DB.exists():
        return [[0] * 24 for _ in range(7)]
    cutoff = (date.today() - timedelta(days=days)).isoformat()

    sql = """SELECT CAST(strftime('%w', p.timestamp, 'localtime') AS INT) AS dow,
                    CAST(strftime('%H', p.timestamp, 'localtime') AS INT) AS hr,
                    COUNT(*) AS n
             FROM prompts p JOIN sessions s ON p.session_id = s.session_id
             WHERE s.is_subagent = 0
               AND date(p.timestamp, 'localtime') >= ?"""
    params: list = [cutoff]
    if tracked_only:
        tracked_dirs = _tracked_project_dirs()
        if not tracked_dirs:
            return [[0] * 24 for _ in range(7)]
        placeholders = ",".join("?" for _ in tracked_dirs)
        sql += f" AND s.project_dir IN ({placeholders})"
        params.extend(tracked_dirs)
    sql += " GROUP BY dow, hr"

    with sqlite3.connect(str(CORPUS_DB)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
    m = [[0] * 24 for _ in range(7)]
    for r in rows:
        if r["dow"] is None or r["hr"] is None:
            continue
        m[r["dow"]][r["hr"]] = r["n"]
    return m


def per_project_totals(days: int = 30) -> list[dict]:
    """Per-project rollup over the window, sorted by cost descending.
    Used in the aggregated metrics page to show which projects drive the totals."""
    from watchmen import state as _state
    out = []
    for p in _state.list_projects():
        rows = daily_metrics(p["project_key"], days=days)
        total = summarize_window(rows, days)
        total["project_key"] = p["project_key"]
        out.append(total)
    out.sort(key=lambda r: r["cost_usd"], reverse=True)
    return out


def activity_calendar(project_key: str, weeks: int = 26) -> list[tuple[str, int]]:
    """Per-day prompt counts for the last `weeks` weeks (Sunday-aligned).
    Returns [(date_iso, prompt_count)] in chronological order."""
    project_dir = _project_dir_for_key(project_key)
    if not project_dir or not CORPUS_DB.exists():
        return []
    today = date.today()
    # Sunday-align: roll back to the Sunday of this week, then go N-1 weeks back.
    # weekday(): Mon=0..Sun=6 → days to subtract to reach Sunday
    to_sunday = (today.weekday() + 1) % 7
    end = today  # inclusive
    start = today - timedelta(days=(weeks * 7 - 1 - to_sunday))

    with sqlite3.connect(str(CORPUS_DB)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT date(p.timestamp, 'localtime') AS d, COUNT(*) AS n
               FROM prompts p JOIN sessions s ON p.session_id = s.session_id
               WHERE s.project_dir = ? AND s.is_subagent = 0
                 AND date(p.timestamp, 'localtime') >= ?
                 AND date(p.timestamp, 'localtime') <= ?
               GROUP BY date(p.timestamp, 'localtime')""",
            (project_dir, start.isoformat(), end.isoformat()),
        ).fetchall()
    counts = {r["d"]: r["n"] for r in rows}
    out = []
    for i in range(weeks * 7):
        d = start + timedelta(days=i)
        if d > end:
            break
        out.append((d.isoformat(), counts.get(d.isoformat(), 0)))
    return out


def activity_by_hour_dow(project_key: str, days: int = 90) -> list[list[int]]:
    """7×24 matrix of prompt counts. Row 0 = Sunday, col 0 = midnight (local).
    Returns matrix[dow][hour] = count."""
    project_dir = _project_dir_for_key(project_key)
    if not project_dir or not CORPUS_DB.exists():
        return [[0] * 24 for _ in range(7)]
    cutoff = (date.today() - timedelta(days=days)).isoformat()

    with sqlite3.connect(str(CORPUS_DB)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT CAST(strftime('%w', p.timestamp, 'localtime') AS INT) AS dow,
                      CAST(strftime('%H', p.timestamp, 'localtime') AS INT) AS hr,
                      COUNT(*) AS n
               FROM prompts p JOIN sessions s ON p.session_id = s.session_id
               WHERE s.project_dir = ? AND s.is_subagent = 0
                 AND date(p.timestamp, 'localtime') >= ?
               GROUP BY dow, hr""",
            (project_dir, cutoff),
        ).fetchall()
    m = [[0] * 24 for _ in range(7)]
    for r in rows:
        if r["dow"] is None or r["hr"] is None:
            continue
        m[r["dow"]][r["hr"]] = r["n"]
    return m


def _heatmap_color(n: int, hi: int, palette: list[str]) -> str:
    if n <= 0 or hi <= 0:
        return palette[0]
    # 4 active buckets above the empty bucket.
    bucket = min(4, max(1, int((n / hi) * 4) + (1 if n > 0 else 0)))
    return palette[bucket]


def calendar_heatmap_svg(daily: list[tuple[str, int]], weeks: int = 26) -> str:
    """GitHub-style contribution grid. 7 rows (Sun→Sat), N cols (weeks)."""
    cell = 12
    gap = 3
    pad_left = 28
    pad_top = 18
    width = pad_left + weeks * (cell + gap)
    height = pad_top + 7 * (cell + gap) + 12
    palette = ["#f3f4f6", "#dbeafe", "#93c5fd", "#3b82f6", "#1e40af"]
    counts = [c for _, c in daily]
    hi = max(counts) if counts else 0

    rects = []
    if daily:
        first_d = date.fromisoformat(daily[0][0])
        for idx, (d_str, n) in enumerate(daily):
            d = date.fromisoformat(d_str)
            col = (d - first_d).days // 7
            row = (d.weekday() + 1) % 7  # Sun=0
            x = pad_left + col * (cell + gap)
            y = pad_top + row * (cell + gap)
            fill = _heatmap_color(n, hi, palette)
            rects.append(
                f'<rect x="{x}" y="{y}" width="{cell}" height="{cell}" rx="2" fill="{fill}">'
                f'<title>{d_str}: {n} prompt{"s" if n != 1 else ""}</title></rect>'
            )

    # Day labels (Sun, Mon...)
    day_labels = ""
    for i, name in enumerate(["Sun", "", "Tue", "", "Thu", "", "Sat"]):
        if name:
            y = pad_top + i * (cell + gap) + cell - 2
            day_labels += f'<text x="0" y="{y}" font-size="10" fill="#6b7280">{name}</text>'

    legend = ""
    lg_x = width - 5 * (cell + gap) - 60
    legend += f'<text x="{lg_x}" y="{height - 4}" font-size="10" fill="#6b7280">less</text>'
    for i, c in enumerate(palette):
        x = lg_x + 28 + i * (cell + 2)
        legend += f'<rect x="{x}" y="{height - 14}" width="{cell}" height="{cell}" rx="2" fill="{c}"/>'
    legend += f'<text x="{lg_x + 28 + 5 * (cell + 2) + 4}" y="{height - 4}" font-size="10" fill="#6b7280">more</text>'

    return (
        f'<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg">'
        f'{day_labels}{"".join(rects)}{legend}</svg>'
    )


def hour_dow_heatmap_svg(matrix: list[list[int]]) -> str:
    """7-row × 24-col heatmap. Row 0 = Sunday."""
    cell_w = 22
    cell_h = 22
    gap = 3
    pad_left = 36
    pad_top = 22
    width = pad_left + 24 * (cell_w + gap)
    height = pad_top + 7 * (cell_h + gap) + 10
    palette = ["#f3f4f6", "#fce7f3", "#f9a8d4", "#ec4899", "#9d174d"]
    hi = max((max(row) for row in matrix), default=0)

    cells = ""
    for dow in range(7):
        for hr in range(24):
            n = matrix[dow][hr]
            fill = _heatmap_color(n, hi, palette)
            x = pad_left + hr * (cell_w + gap)
            y = pad_top + dow * (cell_h + gap)
            day_name = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][dow]
            cells += (
                f'<rect x="{x}" y="{y}" width="{cell_w}" height="{cell_h}" rx="3" fill="{fill}">'
                f'<title>{day_name} {hr:02d}:00 — {n} prompt{"s" if n != 1 else ""}</title></rect>'
            )

    # Hour labels along the top (every 3 hours)
    hour_labels = ""
    for hr in range(0, 24, 3):
        x = pad_left + hr * (cell_w + gap) + cell_w / 2
        hour_labels += f'<text x="{x}" y="14" font-size="10" fill="#6b7280" text-anchor="middle">{hr:02d}</text>'

    # Day labels down the left
    day_labels = ""
    for i, name in enumerate(["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]):
        y = pad_top + i * (cell_h + gap) + cell_h - 6
        day_labels += f'<text x="0" y="{y}" font-size="11" fill="#6b7280">{name}</text>'

    return (
        f'<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg">'
        f'{hour_labels}{day_labels}{cells}</svg>'
    )


def tool_usage(
    project_key: str | None = None,
    days: int = 30,
    limit: int = 12,
    tracked_only: bool = False,
) -> list[dict]:
    """Top N tools by call count over the window, plus error counts.

    project_key=None aggregates across all sessions; pair with tracked_only=True
    to restrict to tracked projects only."""
    if not CORPUS_DB.exists():
        return []
    cutoff = (date.today() - timedelta(days=days)).isoformat()

    sql = """SELECT t.tool_name, COUNT(*) AS n, SUM(t.is_error) AS errors
             FROM tool_calls t JOIN sessions s ON t.session_id = s.session_id
             WHERE s.is_subagent = 0
               AND date(t.timestamp, 'localtime') >= ?"""
    params: list = [cutoff]
    if project_key:
        proj_dir = _project_dir_for_key(project_key)
        if not proj_dir:
            return []
        sql += " AND s.project_dir = ?"
        params.append(proj_dir)
    elif tracked_only:
        dirs = _tracked_project_dirs()
        if not dirs:
            return []
        placeholders = ",".join("?" for _ in dirs)
        sql += f" AND s.project_dir IN ({placeholders})"
        params.extend(dirs)
    sql += f" GROUP BY t.tool_name ORDER BY n DESC LIMIT {limit}"

    with sqlite3.connect(str(CORPUS_DB)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
    return [{"tool": r["tool_name"], "count": r["n"], "errors": r["errors"] or 0} for r in rows]


def streak_stats(
    project_key: str | None = None,
    weeks: int = 26,
    tracked_only: bool = False,
) -> dict:
    """Current and longest consecutive-active-day streak in the calendar window.
    'Active' = at least one prompt that day."""
    if project_key:
        daily = activity_calendar(project_key, weeks=weeks)
    else:
        daily = activity_calendar_all(weeks=weeks, tracked_only=tracked_only)
    if not daily:
        return {"current": 0, "longest": 0, "longest_end": None}

    today_str = date.today().isoformat()

    # Current streak: count consecutive active days ending at today (or last activity).
    current = 0
    for d, n in reversed(daily):
        if d > today_str:
            continue
        if n > 0:
            current += 1
        else:
            break

    # Longest streak in the window.
    longest = 0
    longest_end = None
    run = 0
    run_end = None
    for d, n in daily:
        if n > 0:
            run += 1
            run_end = d
            if run > longest:
                longest = run
                longest_end = run_end
        else:
            run = 0
            run_end = None
    return {"current": current, "longest": longest, "longest_end": longest_end}


def hbar_chart_svg(
    rows: list[tuple[str, float]],
    width: int = 360,
    bar_height: int = 18,
    gap: int = 4,
    color: str = "#4f46e5",
    label_width: int = 110,
    value_fmt: str = "{:,.0f}",
) -> str:
    """Inline-SVG horizontal bar chart. Each row is (label, value), top-down
    in the order given. Bar widths are scaled to the row with the largest
    value. Empty input renders an empty SVG so the template can plug it in
    without branching. Used for top-tools-per-repo, frustration-per-repo,
    and similar low-cardinality categorical charts that read better as
    horizontal bars than as a vertical chart."""
    if not rows:
        return f'<svg width="{width}" height="{bar_height + gap}"></svg>'
    height = (bar_height + gap) * len(rows)
    bars_width = width - label_width - 60  # leave room for value text on the right
    max_v = max((v for _, v in rows), default=0) or 1
    parts = [
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif">'
    ]
    for i, (label, value) in enumerate(rows):
        y = i * (bar_height + gap)
        bar_w = max(2, (value / max_v) * bars_width) if value > 0 else 0
        # Truncate long labels so the chart never overflows.
        label_text = (label[: label_width // 7] + "…") if len(label) > label_width // 7 else label
        parts.append(
            f'<text x="0" y="{y + bar_height * 0.72:.1f}" font-size="11" fill="#374151">{_xml_escape(label_text)}</text>'
            f'<rect x="{label_width}" y="{y}" width="{bar_w:.1f}" height="{bar_height}" rx="2" ry="2" fill="{color}" fill-opacity="0.85"/>'
            f'<text x="{label_width + bar_w + 4:.1f}" y="{y + bar_height * 0.72:.1f}" font-size="11" fill="#6b7280" font-variant-numeric="tabular-nums">{_xml_escape(value_fmt.format(value))}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _xml_escape(s) -> str:
    """Minimal escaper for label/value text inside SVG <text> nodes. Avoids
    a stdlib `html` import; only need <, >, &, ", '."""
    s = str(s)
    return (
        s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;").replace("'", "&#39;")
    )


def sparkline_svg(values: Iterable[float], width: int = 220, height: int = 40, color: str = "#4f46e5") -> str:
    """Tiny inline-SVG line/area sparkline. Zero-deps, renders in any browser."""
    vals = list(values)
    if not vals:
        return f'<svg width="{width}" height="{height}"></svg>'
    if max(vals) == 0 and min(vals) == 0:
        return f'<svg width="{width}" height="{height}"><line x1="0" y1="{height/2}" x2="{width}" y2="{height/2}" stroke="#e5e7eb" stroke-width="1"/></svg>'
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1
    n = len(vals)
    dx = width / max(n - 1, 1)
    points = []
    for i, v in enumerate(vals):
        x = i * dx
        y = height - ((v - lo) / rng) * (height - 4) - 2
        points.append(f"{x:.1f},{y:.1f}")
    pts_str = " ".join(points)
    area_pts = f"0,{height} {pts_str} {width},{height}"
    return (
        f'<svg width="{width}" height="{height}" preserveAspectRatio="none" viewBox="0 0 {width} {height}">'
        f'<polygon points="{area_pts}" fill="{color}" fill-opacity="0.12"/>'
        f'<polyline points="{pts_str}" fill="none" stroke="{color}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>'
        f'</svg>'
    )


# ─── Profile card (per-user spider stats) ────────────────────────────────
#
# The fun tab at /card. Six axes computed from corpus.db and the
# bundles directory, normalized 0..1 against tunable elite caps, then
# rendered as a FIFA-style SVG card with a rating + archetype.
#
# The caps are deliberately reachable — fresh users still get a card
# (Newcomer archetype, rating 40), and a power user with months of
# tracked sessions ends up in the 80s/90s.

CARD_AXES = ("throughput", "frugality", "reliability", "curiosity", "range", "mastery")

# Elite caps for normalization. A user hitting all six caps lands at 99.
# Tuned against my own ~3-month corpus to put a heavy user in the 85-95 band.
_CARD_CAPS = {
    "throughput":    40.0,   # prompts per active day
    "frugality":     0.04,   # USD per prompt — LOWER is better (inverted below)
    "reliability":   1.0,    # 1 - error rate; already 0..1
    "curiosity":     30.0,   # distinct tool names used
    "range":         12.0,   # distinct project dirs touched
    "mastery":       25.0,   # curated skills across all bundles
}

# Archetype name + 1-line flavor, keyed by the dominant axis.
_ARCHETYPES = {
    "throughput":   ("Speedrunner",   "Volume player. Ships in bursts, iterates fast."),
    "frugality":    ("Minimalist",    "Wastes no tokens. Surgical prompts."),
    "reliability":  ("Perfectionist", "Tool calls land. Errors are rare."),
    "curiosity":    ("Explorer",      "Reaches for new tools instead of the same hammer."),
    "range":        ("Polyglot",      "Hops repos without losing context."),
    "mastery":      ("Curator",       "Turns sessions into reusable skill bundles."),
}

# Returned when every axis is below 0.15 — no data yet to characterize.
_NEWCOMER = ("Newcomer", "Brand new corpus. Stats sharpen as you use it.")
_GENERALIST = ("Generalist", "Balanced across the board. No single dominant strength.")


def _normalize(value: float, cap: float, *, invert: bool = False) -> float:
    """Map a raw axis value into 0..1, capped. `invert=True` is for axes
    where lower is better (cost-per-prompt → frugality)."""
    if cap <= 0:
        return 0.0
    if invert:
        if value <= 0:
            return 1.0
        return max(0.0, min(1.0, cap / value))
    return max(0.0, min(1.0, value / cap))


def compute_card_stats(days: int = 90) -> dict:
    """Spider-chart inputs for the user's profile card at /card.

    Pulls from corpus.db (sessions, tool_calls) over the last `days` days
    plus the BUNDLES_DIR for the mastery axis. Returns a dict with:

      - axes:       dict {name → 0..1 normalized}
      - axes_raw:   dict {name → raw value, for the right-side stat list}
      - rating:     int 40..99 (FIFA-style; never 0 so empty corpora still
                    get a respectable card)
      - archetype:  (name, flavor) tuple
      - sessions, prompts, cost_usd, active_days, tool_calls, tool_errors
      - first_seen, last_seen: ISO dates (or None for empty corpus)
      - top_agent:  most-active agent slug + count
      - top_tool:   most-used tool name + count

    Empty corpus → Newcomer card, rating 40, every axis 0.
    """
    from watchmen.paths import BUNDLES_DIR

    out: dict = {
        "axes": {a: 0.0 for a in CARD_AXES},
        "axes_raw": {a: 0.0 for a in CARD_AXES},
        "sessions": 0, "prompts": 0, "cost_usd": 0.0,
        "active_days": 0, "tool_calls": 0, "tool_errors": 0,
        "first_seen": None, "last_seen": None,
        "top_agent": None, "top_tool": None,
    }

    if CORPUS_DB.exists():
        cutoff = (date.today() - timedelta(days=days - 1)).isoformat()
        with sqlite3.connect(str(CORPUS_DB)) as conn:
            conn.row_factory = sqlite3.Row
            agg = conn.execute(
                """SELECT COUNT(*)                            AS sessions,
                          COALESCE(SUM(user_prompt_count),0)  AS prompts,
                          COALESCE(SUM(tool_use_count),0)     AS tool_calls,
                          COALESCE(SUM(tool_error_count),0)   AS tool_errors,
                          COALESCE(SUM(cost_usd),0.0)         AS cost_usd,
                          COUNT(DISTINCT project_dir)         AS projects,
                          COUNT(DISTINCT date(started_at, 'localtime')) AS active_days,
                          MIN(date(started_at, 'localtime'))  AS first_seen,
                          MAX(date(started_at, 'localtime'))  AS last_seen
                     FROM sessions
                    WHERE is_subagent = 0
                      AND date(started_at, 'localtime') >= ?""",
                [cutoff],
            ).fetchone()
            out.update({
                "sessions":    agg["sessions"] or 0,
                "prompts":     agg["prompts"] or 0,
                "tool_calls":  agg["tool_calls"] or 0,
                "tool_errors": agg["tool_errors"] or 0,
                "cost_usd":    agg["cost_usd"] or 0.0,
                "active_days": agg["active_days"] or 0,
                "first_seen":  agg["first_seen"],
                "last_seen":   agg["last_seen"],
                "projects":    agg["projects"] or 0,
            })
            distinct_tools = conn.execute(
                """SELECT COUNT(DISTINCT tc.tool_name) AS n
                     FROM tool_calls tc
                     JOIN sessions s ON s.session_id = tc.session_id
                    WHERE s.is_subagent = 0
                      AND date(s.started_at, 'localtime') >= ?""",
                [cutoff],
            ).fetchone()["n"] or 0
            top_agent_row = conn.execute(
                """SELECT agent, COUNT(*) AS n
                     FROM sessions
                    WHERE is_subagent = 0
                      AND date(started_at, 'localtime') >= ?
                 GROUP BY agent ORDER BY n DESC LIMIT 1""",
                [cutoff],
            ).fetchone()
            if top_agent_row:
                out["top_agent"] = (top_agent_row["agent"], top_agent_row["n"])
            top_tool_row = conn.execute(
                """SELECT tc.tool_name AS t, COUNT(*) AS n
                     FROM tool_calls tc
                     JOIN sessions s ON s.session_id = tc.session_id
                    WHERE s.is_subagent = 0
                      AND date(s.started_at, 'localtime') >= ?
                 GROUP BY tc.tool_name ORDER BY n DESC LIMIT 1""",
                [cutoff],
            ).fetchone()
            if top_tool_row:
                out["top_tool"] = (top_tool_row["t"], top_tool_row["n"])
    else:
        distinct_tools = 0

    # Mastery from on-disk bundles, independent of the corpus window. A
    # curated skill stays curated even if the source sessions aged out.
    curated_skills = 0
    if BUNDLES_DIR.exists():
        for proj_dir in BUNDLES_DIR.iterdir():
            skills_dir = proj_dir / "skills"
            if skills_dir.is_dir():
                curated_skills += sum(1 for d in skills_dir.iterdir() if d.is_dir())

    # Raw axis values.
    throughput   = (out["prompts"] / out["active_days"]) if out["active_days"] else 0.0
    cost_per_prm = (out["cost_usd"] / out["prompts"]) if out["prompts"] else 0.0
    reliability  = (1.0 - out["tool_errors"] / out["tool_calls"]) if out["tool_calls"] else 0.0
    curiosity    = float(distinct_tools)
    proj_range   = float(out.get("projects") or 0)
    mastery      = float(curated_skills)

    out["axes_raw"] = {
        "throughput":  round(throughput, 1),
        "frugality":   round(cost_per_prm, 4),  # show raw $/prompt
        "reliability": round(reliability, 3),
        "curiosity":   int(curiosity),
        "range":       int(proj_range),
        "mastery":     int(mastery),
    }
    # Frugality + reliability only register when there's source data. A
    # corpus with zero spend or zero tool calls is missing-data, not
    # perfect frugality / perfect reliability — otherwise Newcomers
    # would falsely score 1.0 on both axes from a divide-by-zero.
    frugality_axis = (
        _normalize(cost_per_prm, _CARD_CAPS["frugality"], invert=True)
        if out["prompts"] > 0 and out["cost_usd"] > 0 else 0.0
    )
    reliability_axis = (
        _normalize(reliability, _CARD_CAPS["reliability"])
        if out["tool_calls"] > 0 else 0.0
    )
    out["axes"] = {
        "throughput":  _normalize(throughput,  _CARD_CAPS["throughput"]),
        "frugality":   frugality_axis,
        "reliability": reliability_axis,
        "curiosity":   _normalize(curiosity,   _CARD_CAPS["curiosity"]),
        "range":       _normalize(proj_range,  _CARD_CAPS["range"]),
        "mastery":     _normalize(mastery,     _CARD_CAPS["mastery"]),
    }

    # Rating: weighted average mapped to 40..99 so even a Newcomer gets a
    # card, and the elite ceiling stays sub-100 (FIFA convention).
    avg = sum(out["axes"].values()) / len(out["axes"])
    out["rating"] = max(40, min(99, round(40 + 59 * avg)))

    # Archetype: dominant axis if it's clearly ahead, else Generalist.
    # Newcomer overrides everything when there's basically no data yet.
    if max(out["axes"].values()) < 0.15 and out["sessions"] < 5:
        out["archetype"] = _NEWCOMER
    else:
        top_axis = max(out["axes"], key=out["axes"].get)
        top_val = out["axes"][top_axis]
        runner_up = sorted(out["axes"].values(), reverse=True)[1]
        # "Clearly ahead" = at least 1.25× the runner-up AND ≥ 0.4 absolute.
        if top_val >= 0.4 and (runner_up == 0 or top_val / runner_up >= 1.25):
            out["archetype"] = _ARCHETYPES[top_axis]
        else:
            out["archetype"] = _GENERALIST
    return out


def card_svg(stats: dict, *, width: int = 640, height: int = 880) -> str:
    """FIFA-style card with a 6-axis spider chart, rating, and archetype.

    Pure SVG so it renders identically without JS, copies cleanly into
    screenshots, and inherits the page's typography stack. Polygons are
    pre-computed in Python; the template just embeds the returned blob.
    """
    import math

    rating = stats["rating"]
    arch_name, arch_flavor = stats["archetype"]
    axes = stats["axes"]
    raw = stats["axes_raw"]

    # Card tier colors. Mirrors FIFA but tuned for the watchmen palette.
    if rating >= 90:
        tier_grad = ("#fde68a", "#f59e0b")  # gold
        tier_text = "#78350f"
    elif rating >= 80:
        tier_grad = ("#e5e7eb", "#9ca3af")  # silver
        tier_text = "#374151"
    elif rating >= 70:
        tier_grad = ("#fed7aa", "#c2410c")  # bronze
        tier_text = "#7c2d12"
    else:
        tier_grad = ("#e0e7ff", "#6366f1")  # indigo — Newcomer / Generalist
        tier_text = "#3730a3"

    # Spider geometry: hexagon, top vertex straight up.
    cx, cy, r = width / 2, 470, 175
    n = len(CARD_AXES)
    angles = [(-math.pi / 2) + (2 * math.pi * i / n) for i in range(n)]
    rings = [0.25, 0.5, 0.75, 1.0]
    grid_polys: list[str] = []
    for ring in rings:
        pts = [
            f"{cx + r * ring * math.cos(a):.1f},{cy + r * ring * math.sin(a):.1f}"
            for a in angles
        ]
        grid_polys.append(
            f'<polygon points="{" ".join(pts)}" fill="none" stroke="#e5e7eb" stroke-width="1"/>'
        )
    axis_lines: list[str] = []
    label_marks: list[str] = []
    for i, a in enumerate(angles):
        x, y = cx + r * math.cos(a), cy + r * math.sin(a)
        axis_lines.append(
            f'<line x1="{cx:.1f}" y1="{cy:.1f}" x2="{x:.1f}" y2="{y:.1f}" '
            f'stroke="#e5e7eb" stroke-width="1"/>'
        )
        # Labels sit just outside the outer ring; anchor varies by quadrant
        # so text doesn't overlap the polygon.
        lx, ly = cx + (r + 22) * math.cos(a), cy + (r + 22) * math.sin(a)
        if abs(math.cos(a)) < 0.1:
            anchor = "middle"
        elif math.cos(a) > 0:
            anchor = "start"
        else:
            anchor = "end"
        name = CARD_AXES[i]
        label_marks.append(
            f'<text x="{lx:.1f}" y="{ly + 4:.1f}" text-anchor="{anchor}" '
            f'font-size="11" font-weight="600" fill="#374151" '
            f'letter-spacing="0.05em">{name.upper()}</text>'
        )
        # Numeric value just below the label.
        if name == "frugality":
            num = f"${raw[name]:.3f}/prm" if raw[name] else "—"
        elif name == "reliability":
            num = f"{int(raw[name] * 100)}%"
        else:
            num = str(raw[name])
        label_marks.append(
            f'<text x="{lx:.1f}" y="{ly + 18:.1f}" text-anchor="{anchor}" '
            f'font-size="11" fill="#6b7280">{num}</text>'
        )

    # The user's actual spider polygon.
    user_pts = []
    for i, a in enumerate(angles):
        val = max(0.02, axes[CARD_AXES[i]])  # floor so axis is visible
        x = cx + r * val * math.cos(a)
        y = cy + r * val * math.sin(a)
        user_pts.append(f"{x:.1f},{y:.1f}")
    user_poly_pts = " ".join(user_pts)

    # Bottom strip: total sessions / cost / top agent / favorite tool.
    sessions_str = f"{stats['sessions']:,}"
    cost_str = f"${stats['cost_usd']:,.2f}"
    agent_str = adapter_label(stats["top_agent"][0]) if stats["top_agent"] else "—"
    tool_str = stats["top_tool"][0] if stats["top_tool"] else "—"
    first = stats["first_seen"] or "—"
    last = stats["last_seen"] or "—"

    arch_name_x = _xml_escape(arch_name)
    arch_flavor_x = _xml_escape(arch_flavor)
    agent_x = _xml_escape(agent_str)
    tool_x = _xml_escape(tool_str)

    return f'''<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="watchmen profile card">
  <defs>
    <linearGradient id="card-tier" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="{tier_grad[0]}"/>
      <stop offset="100%" stop-color="{tier_grad[1]}"/>
    </linearGradient>
    <linearGradient id="user-fill" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#6366f1" stop-opacity="0.45"/>
      <stop offset="100%" stop-color="#4f46e5" stop-opacity="0.20"/>
    </linearGradient>
  </defs>

  <!-- Card body -->
  <rect x="20" y="20" width="{width-40}" height="{height-40}" rx="24" ry="24"
        fill="white" stroke="url(#card-tier)" stroke-width="6"/>

  <!-- Header strip -->
  <rect x="32" y="32" width="{width-64}" height="120" rx="14" ry="14"
        fill="url(#card-tier)" opacity="0.18"/>

  <!-- Rating (large, top-left) -->
  <text x="80" y="120" font-size="86" font-weight="800" fill="{tier_text}"
        letter-spacing="-0.04em" font-variant-numeric="tabular-nums">{rating}</text>
  <text x="80" y="146" font-size="11" font-weight="700" fill="{tier_text}"
        letter-spacing="0.18em">OVR</text>

  <!-- Archetype -->
  <text x="225" y="86" font-size="26" font-weight="700" fill="#111827"
        letter-spacing="-0.01em">{arch_name_x}</text>
  <text x="225" y="112" font-size="13" fill="#4b5563">{arch_flavor_x}</text>
  <text x="225" y="138" font-size="11" fill="#6b7280" letter-spacing="0.05em">
    {sessions_str} SESSIONS  ·  {first} → {last}
  </text>

  <!-- Spider chart -->
  {''.join(grid_polys)}
  {''.join(axis_lines)}
  <polygon points="{user_poly_pts}"
           fill="url(#user-fill)" stroke="#4f46e5" stroke-width="2.5"
           stroke-linejoin="round"/>
  {''.join(label_marks)}

  <!-- Footer strip -->
  <line x1="56" y1="745" x2="{width-56}" y2="745" stroke="#e5e7eb" stroke-width="1"/>

  <text x="80" y="785" font-size="11" font-weight="600" fill="#6b7280" letter-spacing="0.1em">TOTAL SPEND</text>
  <text x="80" y="815" font-size="24" font-weight="700" fill="#111827" font-variant-numeric="tabular-nums">{cost_str}</text>

  <text x="240" y="785" font-size="11" font-weight="600" fill="#6b7280" letter-spacing="0.1em">TOP AGENT</text>
  <text x="240" y="815" font-size="20" font-weight="700" fill="#111827">{agent_x}</text>

  <text x="430" y="785" font-size="11" font-weight="600" fill="#6b7280" letter-spacing="0.1em">FAV TOOL</text>
  <text x="430" y="815" font-size="20" font-weight="700" fill="#111827">{tool_x}</text>
</svg>'''


if __name__ == "__main__":
    # Quick CLI for dev: python metrics.py <project_key>
    import sys
    if len(sys.argv) < 2:
        print("usage: python metrics.py <project_key>")
        sys.exit(1)
    rows = daily_metrics(sys.argv[1], days=30)
    for r in rows[:10]:
        print(f"  {r['date']}  sessions={r['sessions']:>2}  prompts={r['prompts']:>3}  errors={r['tool_errors']:>2}  ${r['cost_usd']:>5.2f}  suggestions={r['suggestions_fired']:>2}  uptake={r['uptake']}")
