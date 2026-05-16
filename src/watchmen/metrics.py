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
# Shown at the top of /metrics. Six axes computed from corpus.db and the
# bundles directory, normalized 0..1 against tunable elite caps, then
# rendered as a Football Manager–style stat card: 3-column color-coded
# attribute grid, hex spider chart with tinted green→yellow→red rings,
# procedural "player traits" derived from the stats.
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


def _kind_from_norm(score: float) -> str:
    """Bucket a 0..1 normalized score into the 3 color tiers used by the
    profile card's stat columns. Tuned so most stats fall into "mid"
    rather than the extremes — green should feel earned."""
    if score >= 0.7:
        return "elite"
    if score >= 0.4:
        return "mid"
    return "low"


def compute_card_stats(days: int = 90) -> dict:
    """Inputs for the Football Manager–style profile card shown on
    /metrics. Pulls from corpus.db over the last `days` days plus the
    BUNDLES_DIR for the always-current mastery axis. Returns a dict
    with:

      - axes:           {axis → 0..1 normalized}
      - axes_raw:       {axis → human-readable raw value}
      - rating:         int 40..99 (FIFA convention; floor so a Newcomer
                        still gets a respectable card)
      - archetype:      (name, flavor) tuple
      - attribute_groups: list of 3 dicts (Volume / Efficiency / Breadth)
                          each with {label, stats: [{label, raw, kind}]}.
                          kind is "elite" / "mid" / "low" / "neutral"; the
                          template just renders, no math. The key name is
                          "stats" rather than "items" because Jinja's
                          `group.items` resolves to dict.items() first.
      - traits:         list of strings — procedural badges like
                        "Codex-first", "Tool collector".
      - sessions, prompts, cost_usd, active_days, tool_calls, tool_errors,
        projects, distinct_tools, distinct_agents, agents (dict),
        cache_hit_ratio, prompts_per_session, cost_per_session
      - first_seen, last_seen, top_agent, top_tool

    Empty corpus → Newcomer card, rating 40, every axis 0.
    """
    from watchmen.paths import BUNDLES_DIR

    out: dict = {
        "axes": {a: 0.0 for a in CARD_AXES},
        "axes_raw": {a: 0.0 for a in CARD_AXES},
        "sessions": 0, "prompts": 0, "cost_usd": 0.0,
        "active_days": 0, "tool_calls": 0, "tool_errors": 0,
        "projects": 0, "distinct_tools": 0, "distinct_agents": 0,
        "input_tokens": 0, "cache_creation_tokens": 0,
        "cache_read_tokens": 0, "output_tokens": 0,
        "agents": {},
        "top_tools": [],
        "first_seen": None, "last_seen": None,
        "top_agent": None, "top_tool": None,
    }

    if CORPUS_DB.exists():
        cutoff = (date.today() - timedelta(days=days - 1)).isoformat()
        with sqlite3.connect(str(CORPUS_DB)) as conn:
            conn.row_factory = sqlite3.Row
            agg = conn.execute(
                """SELECT COUNT(*)                                  AS sessions,
                          COALESCE(SUM(user_prompt_count),0)        AS prompts,
                          COALESCE(SUM(tool_use_count),0)           AS tool_calls,
                          COALESCE(SUM(tool_error_count),0)         AS tool_errors,
                          COALESCE(SUM(cost_usd),0.0)               AS cost_usd,
                          COALESCE(SUM(input_tokens),0)             AS input_tokens,
                          COALESCE(SUM(cache_creation_tokens),0)    AS cache_creation_tokens,
                          COALESCE(SUM(cache_read_tokens),0)        AS cache_read_tokens,
                          COALESCE(SUM(output_tokens),0)            AS output_tokens,
                          COUNT(DISTINCT project_dir)               AS projects,
                          COUNT(DISTINCT date(started_at, 'localtime')) AS active_days,
                          COUNT(DISTINCT agent)                     AS distinct_agents,
                          MIN(date(started_at, 'localtime'))        AS first_seen,
                          MAX(date(started_at, 'localtime'))        AS last_seen
                     FROM sessions
                    WHERE is_subagent = 0
                      AND date(started_at, 'localtime') >= ?""",
                [cutoff],
            ).fetchone()
            for k in ("sessions", "prompts", "tool_calls", "tool_errors",
                      "cost_usd", "input_tokens", "cache_creation_tokens",
                      "cache_read_tokens", "output_tokens", "projects",
                      "active_days", "distinct_agents",
                      "first_seen", "last_seen"):
                out[k] = (agg[k] if agg[k] is not None
                          else (0.0 if k == "cost_usd" else 0))
            out["distinct_tools"] = conn.execute(
                """SELECT COUNT(DISTINCT tc.tool_name) AS n
                     FROM tool_calls tc
                     JOIN sessions s ON s.session_id = tc.session_id
                    WHERE s.is_subagent = 0
                      AND date(s.started_at, 'localtime') >= ?""",
                [cutoff],
            ).fetchone()["n"] or 0
            agent_rows = conn.execute(
                """SELECT agent, COUNT(*) AS n
                     FROM sessions
                    WHERE is_subagent = 0
                      AND date(started_at, 'localtime') >= ?
                 GROUP BY agent ORDER BY n DESC""",
                [cutoff],
            ).fetchall()
            out["agents"] = {r["agent"]: r["n"] for r in agent_rows}
            if agent_rows:
                out["top_agent"] = (agent_rows[0]["agent"], agent_rows[0]["n"])
            tool_rows = conn.execute(
                """SELECT tc.tool_name AS t, COUNT(*) AS n
                     FROM tool_calls tc
                     JOIN sessions s ON s.session_id = tc.session_id
                    WHERE s.is_subagent = 0
                      AND date(s.started_at, 'localtime') >= ?
                 GROUP BY tc.tool_name ORDER BY n DESC LIMIT 8""",
                [cutoff],
            ).fetchall()
            out["top_tools"] = [(r["t"], r["n"]) for r in tool_rows]
            if tool_rows:
                out["top_tool"] = (tool_rows[0]["t"], tool_rows[0]["n"])

    # Mastery from on-disk bundles, independent of the corpus window. A
    # curated skill stays curated even if the source sessions aged out.
    curated_skills = 0
    if BUNDLES_DIR.exists():
        for proj_dir in BUNDLES_DIR.iterdir():
            skills_dir = proj_dir / "skills"
            if skills_dir.is_dir():
                curated_skills += sum(1 for d in skills_dir.iterdir() if d.is_dir())
    out["curated_skills"] = curated_skills

    # Derived stats used by the 3-column attribute grid.
    out["prompts_per_session"] = (out["prompts"] / out["sessions"]) if out["sessions"] else 0.0
    out["cost_per_session"]    = (out["cost_usd"] / out["sessions"]) if out["sessions"] else 0.0
    total_in = out["input_tokens"] + out["cache_creation_tokens"] + out["cache_read_tokens"]
    out["cache_hit_ratio"]     = (out["cache_read_tokens"] / total_in) if total_in else 0.0

    # Raw axis values.
    throughput   = (out["prompts"] / out["active_days"]) if out["active_days"] else 0.0
    cost_per_prm = (out["cost_usd"] / out["prompts"]) if out["prompts"] else 0.0
    reliability  = (1.0 - out["tool_errors"] / out["tool_calls"]) if out["tool_calls"] else 0.0

    out["axes_raw"] = {
        "throughput":  round(throughput, 1),
        "frugality":   round(cost_per_prm, 4),
        "reliability": round(reliability, 3),
        "curiosity":   out["distinct_tools"],
        "range":       out["projects"],
        "mastery":     curated_skills,
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
        "curiosity":   _normalize(out["distinct_tools"], _CARD_CAPS["curiosity"]),
        "range":       _normalize(out["projects"],       _CARD_CAPS["range"]),
        "mastery":     _normalize(curated_skills,        _CARD_CAPS["mastery"]),
    }

    # Rating: weighted average mapped to 40..99 (FIFA convention).
    avg = sum(out["axes"].values()) / len(out["axes"])
    out["rating"] = max(40, min(99, round(40 + 59 * avg)))

    # Archetype: dominant axis if it's clearly ahead, else Generalist.
    if max(out["axes"].values()) < 0.15 and out["sessions"] < 5:
        out["archetype"] = _NEWCOMER
    else:
        top_axis = max(out["axes"], key=out["axes"].get)
        top_val = out["axes"][top_axis]
        runner_up = sorted(out["axes"].values(), reverse=True)[1]
        if top_val >= 0.4 and (runner_up == 0 or top_val / runner_up >= 1.25):
            out["archetype"] = _ARCHETYPES[top_axis]
        else:
            out["archetype"] = _GENERALIST

    out["attribute_groups"] = _compute_attribute_groups(out, days)
    out["traits"] = _compute_traits(out)
    return out


def _compute_attribute_groups(stats: dict, window_days: int) -> list[dict]:
    """Split derived stats into 3 FM-style columns. Each item gets a
    `kind` ("elite" / "mid" / "low" / "neutral") so the template just
    renders without recomputing thresholds. Thresholds scale with the
    window so a 30-day card and a 365-day card both have meaningful
    color coding."""
    # Window-scaled targets — at the elite tier, the user is "on" most
    # days of the window with substantial volume.
    elite_active   = max(7,   window_days * 0.55)
    elite_sessions = max(20,  window_days * 1.1)
    elite_toolcalls = max(500, window_days * 35)

    def stat(label, raw, score, *, neutral=False):
        return {
            "label": label,
            "raw": raw,
            "kind": "neutral" if neutral else _kind_from_norm(score),
        }

    axes = stats["axes"]
    volume = [
        stat("Throughput",
             f"{stats['axes_raw']['throughput']}/d",  axes["throughput"]),
        stat("Sessions",
             f"{stats['sessions']:,}",
             _normalize(stats["sessions"], elite_sessions)),
        stat("Active days",
             f"{stats['active_days']}",
             _normalize(stats["active_days"], elite_active)),
        stat("Tool calls",
             _format_kn(stats["tool_calls"]),
             _normalize(stats["tool_calls"], elite_toolcalls)),
        stat("Prompts / sess",
             f"{stats['prompts_per_session']:.1f}",
             _normalize(stats["prompts_per_session"], 25.0)),
    ]
    efficiency = [
        stat("Reliability",
             f"{int(stats['axes_raw']['reliability'] * 100)}%" if stats["tool_calls"] else "—",
             axes["reliability"]),
        stat("Frugality",
             f"${stats['axes_raw']['frugality']:.3f}/prm" if stats["prompts"] and stats["cost_usd"] else "—",
             axes["frugality"]),
        stat("Cache hit",
             f"{int(stats['cache_hit_ratio'] * 100)}%" if stats["cache_hit_ratio"] else "—",
             _normalize(stats["cache_hit_ratio"], 0.7)),
        stat("Cost / sess",
             f"${stats['cost_per_session']:.2f}" if stats["sessions"] else "—",
             _normalize(stats["cost_per_session"], 5.0, invert=True) if stats["cost_per_session"] else 0.0),
        stat("Total spend",
             f"${stats['cost_usd']:,.2f}", 0.0, neutral=True),
    ]
    top_agent_label = adapter_label(stats["top_agent"][0]) if stats["top_agent"] else "—"
    breadth = [
        stat("Curiosity",
             f"{stats['axes_raw']['curiosity']} tools", axes["curiosity"]),
        stat("Range",
             f"{stats['axes_raw']['range']} repos",     axes["range"]),
        stat("Mastery",
             f"{stats['axes_raw']['mastery']} skills",  axes["mastery"]),
        stat("Agents",
             f"{stats['distinct_agents']}",
             _normalize(stats["distinct_agents"], 3.0)),
        stat("Top agent",
             top_agent_label, 0.0, neutral=True),
    ]
    return [
        {"label": "Volume",     "stats": volume},
        {"label": "Efficiency", "stats": efficiency},
        {"label": "Breadth",    "stats": breadth},
    ]


def _compute_traits(stats: dict) -> list[str]:
    """Procedural one-line badges derived from the stats. Mirrors FM's
    'player traits' field. Order is stable (most distinctive first) and
    duplicates filter out so we never claim two contradicting flavors."""
    out: list[str] = []
    axes = stats["axes"]
    if stats["sessions"] < 5 and max(axes.values()) < 0.15:
        return ["Newcomer"]

    # Agent allegiance — if one agent owns the majority of sessions.
    if stats["top_agent"]:
        slug, n = stats["top_agent"]
        if stats["sessions"] and n / stats["sessions"] >= 0.55:
            out.append(f"{adapter_label(slug)}-first")
    if stats["distinct_agents"] >= 3:
        out.append("Multi-agent")

    if axes["throughput"] >= 0.7:
        out.append("Speedrunner")
    if axes["curiosity"] >= 0.7:
        out.append("Tool collector")
    if axes["range"] >= 0.7:
        out.append("Multi-repo hopper")
    if axes["reliability"] >= 0.95:
        out.append("Reliability master")
    if axes["mastery"] >= 0.5:
        out.append("Curator")
    if stats["cache_hit_ratio"] >= 0.7:
        out.append("Cache wizard")
    if axes["frugality"] > 0 and axes["frugality"] < 0.3:
        out.append("Heavy spender")
    elif axes["frugality"] >= 0.7:
        out.append("Frugal")

    if not out:
        out = ["Generalist"]
    # Cap at 5 so the row doesn't wrap awkwardly.
    return out[:5]


def _format_kn(n: int) -> str:
    """Compact integer formatter: 1500 → 1.5k, 1500000 → 1.5M."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n / 1_000:.0f}k"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return f"{n:,}"


def agent_donut_svg(agent_counts: dict, *, size: int = 220, total_label: str = "sessions") -> str:
    """Donut chart of per-agent session shares. Each agent is a colored
    segment, center shows the total + label. Empty input renders a
    muted "no data" donut so the layout doesn't collapse on empty
    corpora."""
    import math
    cx = cy = size / 2
    r_outer = size * 0.42
    r_inner = size * 0.28

    colors = {
        "claude_code": "#6366f1",   # indigo
        "codex":       "#0891b2",   # cyan
        "pi":          "#a855f7",   # purple
    }
    fallback_palette = ["#22c55e", "#f59e0b", "#ef4444", "#14b8a6", "#ec4899"]

    total = sum(agent_counts.values())
    if total == 0:
        return (
            f'<svg viewBox="0 0 {size} {size}" xmlns="http://www.w3.org/2000/svg" role="img">'
            f'<circle cx="{cx}" cy="{cy}" r="{r_outer}" fill="hsl(var(--muted))"/>'
            f'<circle cx="{cx}" cy="{cy}" r="{r_inner}" fill="white"/>'
            f'<text x="{cx}" y="{cy + 4}" text-anchor="middle" font-size="11" '
            f'fill="hsl(var(--muted-foreground))">no data</text>'
            f'</svg>'
        )

    segs: list[str] = []
    angle = -math.pi / 2
    fallback_i = 0
    sorted_agents = sorted(agent_counts.items(), key=lambda kv: kv[1], reverse=True)
    for slug, n in sorted_agents:
        if not n:
            continue
        frac = n / total
        next_angle = angle + 2 * math.pi * frac
        # Avoid a single-segment "Z" arc rendering as nothing — when one
        # agent owns 100%, force a tiny gap so the path is well-formed.
        if frac >= 0.9999:
            next_angle = angle + 2 * math.pi * 0.9999
        large_arc = 1 if frac > 0.5 else 0
        x1 = cx + r_outer * math.cos(angle);    y1 = cy + r_outer * math.sin(angle)
        x2 = cx + r_outer * math.cos(next_angle); y2 = cy + r_outer * math.sin(next_angle)
        ix1 = cx + r_inner * math.cos(next_angle); iy1 = cy + r_inner * math.sin(next_angle)
        ix2 = cx + r_inner * math.cos(angle);    iy2 = cy + r_inner * math.sin(angle)
        color = colors.get(slug)
        if not color:
            color = fallback_palette[fallback_i % len(fallback_palette)]
            fallback_i += 1
        path = (
            f"M {x1:.1f},{y1:.1f} "
            f"A {r_outer:.1f},{r_outer:.1f} 0 {large_arc} 1 {x2:.1f},{y2:.1f} "
            f"L {ix1:.1f},{iy1:.1f} "
            f"A {r_inner:.1f},{r_inner:.1f} 0 {large_arc} 0 {ix2:.1f},{iy2:.1f} Z"
        )
        segs.append(f'<path d="{path}" fill="{color}" stroke="white" stroke-width="2"/>')
        angle = next_angle

    total_str = _format_kn(total)
    return (
        f'<svg viewBox="0 0 {size} {size}" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="agent mix donut">'
        f'{"".join(segs)}'
        f'<text x="{cx}" y="{cy - 4}" text-anchor="middle" font-size="24" font-weight="700" '
        f'fill="hsl(220 9% 18%)" font-variant-numeric="tabular-nums">{total_str}</text>'
        f'<text x="{cx}" y="{cy + 14}" text-anchor="middle" font-size="9" font-weight="600" '
        f'fill="hsl(220 9% 46%)" letter-spacing="0.14em">{total_label.upper()}</text>'
        f'</svg>'
    )


def agent_donut_legend(agent_counts: dict) -> list[dict]:
    """Companion data for the donut: list of dicts with slug, label,
    count, share (0..1), color. Template renders the legend with the
    same color order the donut uses."""
    total = sum(agent_counts.values()) or 1
    colors = {"claude_code": "#6366f1", "codex": "#0891b2", "pi": "#a855f7"}
    fallback = ["#22c55e", "#f59e0b", "#ef4444", "#14b8a6", "#ec4899"]
    out, fb_i = [], 0
    for slug, n in sorted(agent_counts.items(), key=lambda kv: kv[1], reverse=True):
        if not n:
            continue
        if slug in colors:
            color = colors[slug]
        else:
            color = fallback[fb_i % len(fallback)]
            fb_i += 1
        out.append({
            "slug": slug, "label": adapter_label(slug),
            "count": n, "share": n / total, "color": color,
        })
    return out


def card_svg(stats: dict, *, size: int = 440) -> str:
    """Hex spider chart with FM-style tinted concentric rings.

    Red core (0.0–0.25) → orange (0.25–0.5) → yellow (0.5–0.75) →
    green outer ring (0.75–1.0). The user's polygon is drawn on top in
    white with an indigo border so it pops against the tinted rings.

    No header / footer / rating in the SVG — those moved into HTML on
    the profile section of /metrics so labels stay copy-pasteable and
    the SVG fits any column width.
    """
    import math

    cx, cy = size / 2, size / 2
    r = size * 0.42  # outer ring radius
    n = len(CARD_AXES)
    angles = [(-math.pi / 2) + (2 * math.pi * i / n) for i in range(n)]

    def _ring_points(scale: float) -> str:
        return " ".join(
            f"{cx + r * scale * math.cos(a):.1f},{cy + r * scale * math.sin(a):.1f}"
            for a in angles
        )

    # Tinted ring colors, outer → inner. Each band is the area between
    # one ring and the next; we draw the OUTER one first as a green
    # background, then layer smaller polygons of warmer colors on top.
    # That gives the FM "elite zone is green, weak zone is red" effect.
    ring_polys = [
        f'<polygon points="{_ring_points(1.00)}" fill="hsl(142 60% 86%)" stroke="hsl(220 13% 86%)" stroke-width="1"/>',
        f'<polygon points="{_ring_points(0.75)}" fill="hsl(45  90% 90%)" stroke="hsl(220 13% 86%)" stroke-width="1"/>',
        f'<polygon points="{_ring_points(0.50)}" fill="hsl(25  95% 90%)" stroke="hsl(220 13% 86%)" stroke-width="1"/>',
        f'<polygon points="{_ring_points(0.25)}" fill="hsl(0   75% 92%)" stroke="hsl(220 13% 86%)" stroke-width="1"/>',
    ]

    # Spokes from center to outer ring.
    spokes: list[str] = []
    labels: list[str] = []
    for i, a in enumerate(angles):
        x, y = cx + r * math.cos(a), cy + r * math.sin(a)
        spokes.append(
            f'<line x1="{cx:.1f}" y1="{cy:.1f}" x2="{x:.1f}" y2="{y:.1f}" '
            f'stroke="white" stroke-width="1.4" stroke-opacity="0.8"/>'
        )
        # Label sits just past the outer ring.
        lx, ly = cx + (r + 18) * math.cos(a), cy + (r + 18) * math.sin(a)
        if abs(math.cos(a)) < 0.1:
            anchor = "middle"
        elif math.cos(a) > 0:
            anchor = "start"
        else:
            anchor = "end"
        labels.append(
            f'<text x="{lx:.1f}" y="{ly + 4:.1f}" text-anchor="{anchor}" '
            f'font-size="10" font-weight="700" fill="hsl(220 9% 25%)" '
            f'letter-spacing="0.08em">{CARD_AXES[i].upper()}</text>'
        )

    # The user's polygon, on top of the rings.
    user_pts = []
    for i, a in enumerate(angles):
        v = max(0.02, stats["axes"][CARD_AXES[i]])
        x = cx + r * v * math.cos(a)
        y = cy + r * v * math.sin(a)
        user_pts.append(f"{x:.1f},{y:.1f}")
    user_poly = " ".join(user_pts)

    return f'''<svg viewBox="0 0 {size} {size + 36}" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="watchmen profile spider chart">
  {''.join(ring_polys)}
  {''.join(spokes)}
  <polygon points="{user_poly}"
           fill="hsl(243 75% 59% / 0.65)"
           stroke="hsl(243 75% 45%)" stroke-width="2.5" stroke-linejoin="round"/>
  {''.join(f'<circle cx="{cx + r * max(0.02, stats["axes"][CARD_AXES[i]]) * math.cos(a):.1f}" cy="{cy + r * max(0.02, stats["axes"][CARD_AXES[i]]) * math.sin(a):.1f}" r="3" fill="white" stroke="hsl(243 75% 45%)" stroke-width="2"/>' for i, a in enumerate(angles))}
  {''.join(labels)}
</svg>'''


def card_tier_colors(rating: int) -> dict:
    """Return CSS-ready color tokens for the rating tier so the template
    can render the header strip + rating number with matching tints.
    Mirrors FIFA: gold / silver / bronze / indigo (default)."""
    if rating >= 90:
        return {"name": "gold", "from": "#fef3c7", "to": "#f59e0b",
                "text": "#78350f", "border": "#d97706"}
    if rating >= 80:
        return {"name": "silver", "from": "#f3f4f6", "to": "#9ca3af",
                "text": "#1f2937", "border": "#6b7280"}
    if rating >= 70:
        return {"name": "bronze", "from": "#fed7aa", "to": "#c2410c",
                "text": "#7c2d12", "border": "#9a3412"}
    return {"name": "indigo", "from": "#e0e7ff", "to": "#6366f1",
            "text": "#3730a3", "border": "#4f46e5"}


if __name__ == "__main__":
    # Quick CLI for dev: python metrics.py <project_key>
    import sys
    if len(sys.argv) < 2:
        print("usage: python metrics.py <project_key>")
        sys.exit(1)
    rows = daily_metrics(sys.argv[1], days=30)
    for r in rows[:10]:
        print(f"  {r['date']}  sessions={r['sessions']:>2}  prompts={r['prompts']:>3}  errors={r['tool_errors']:>2}  ${r['cost_usd']:>5.2f}  suggestions={r['suggestions_fired']:>2}  uptake={r['uptake']}")
