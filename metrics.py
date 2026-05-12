"""Daily efficiency metrics for a tracked project.

Joins three data sources to build per-day rollups for the metrics viewer + CLI:

  - corpus.db.sessions     → sessions, prompts, tool errors, token usage by date
  - ~/.watchmen/suggestions.jsonl → suggestions fired, with session_id
  - prompts table          → uptake detection (did /<skill> appear in a later
                              prompt within the same session within 1 hour?)

Cost is computed from a model→price table. Prices are public Anthropic
list prices per million tokens; update as they change.
"""

import json
import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).parent
CORPUS_DB = ROOT / "corpus.db"
SUGGESTIONS_LOG = Path.home() / ".watchmen" / "suggestions.jsonl"

# Per 1M tokens. Source: https://platform.claude.com/docs/en/about-claude/pricing
# Tuple shape: (input, cache_write_5m, cache_write_1h, cache_read, output).
# Last verified: 2026-05-12.
MODEL_PRICES: dict[str, tuple[float, float, float, float, float]] = {
    # Opus 4.5/4.6/4.7 share the new (cheaper) pricing.
    "opus-4.7":  (5.00,  6.25,  10.00, 0.50, 25.00),
    "opus-4.6":  (5.00,  6.25,  10.00, 0.50, 25.00),
    "opus-4.5":  (5.00,  6.25,  10.00, 0.50, 25.00),
    # Older Opus 4 / 4.1 keep the previous higher rates.
    "opus-4.1":  (15.00, 18.75, 30.00, 1.50, 75.00),
    "opus-4":    (15.00, 18.75, 30.00, 1.50, 75.00),
    # Sonnet family (4 / 4.5 / 4.6 all identical).
    "sonnet-4.6":(3.00,  3.75,  6.00,  0.30, 15.00),
    "sonnet-4.5":(3.00,  3.75,  6.00,  0.30, 15.00),
    "sonnet-4":  (3.00,  3.75,  6.00,  0.30, 15.00),
    # Haiku family — 4.5 jumped from older 3.5 pricing.
    "haiku-4.5": (1.00,  1.25,  2.00,  0.10, 5.00),
    "haiku-3.5": (0.80,  1.00,  1.60,  0.08, 4.00),
    # ── OpenAI (Codex CLI). Source: https://openai.com/api/pricing
    # OpenAI doesn't bill cache writes separately, so 5m/1h slots = input rate
    # (won't be charged — codex adapter sets cc_5m/cc_1h to 0). Cache-read column
    # used for cached_input_tokens, which OpenAI prices at ~10% of input.
    # Last verified: 2026-05-12.
    "gpt-5.5":     (1.25, 1.25, 1.25, 0.125, 10.00),
    "gpt-5.4":     (1.25, 1.25, 1.25, 0.125, 10.00),
    "gpt-5-mini":  (0.25, 0.25, 0.25, 0.025, 2.00),
    "gpt-5":       (1.25, 1.25, 1.25, 0.125, 10.00),
    "gpt-4.1":     (2.00, 2.00, 2.00, 0.500, 8.00),
    "gpt-4o":      (2.50, 2.50, 2.50, 1.250, 10.00),
    "o3":          (2.00, 2.00, 2.00, 0.500, 8.00),
    "o4-mini":     (1.10, 1.10, 1.10, 0.275, 4.40),
}
DEFAULT_PRICE = MODEL_PRICES["sonnet-4.6"]


_VERSION_DASH = re.compile(r"(opus|sonnet|haiku)-(\d+)-(\d+)\b")
# GPT model names use dots too (gpt-5.5, gpt-4.1) but API also returns dash forms
# in some cases (gpt-5-5-mini). Same normalizer pattern:
_GPT_DASH = re.compile(r"gpt-(\d+)-(\d+)\b")


def price_for_model(model: str | None) -> tuple[float, float, float, float, float]:
    """Match by lowercase substring. Longest key wins, so 'opus-4.7' matches
    before 'opus-4'. Anthropic API model names use dashes between version
    components ('claude-opus-4-7'), so we first normalize 'X-major-minor' to
    'X-major.minor' to match our dot-separated keys. Falls back to family
    default, then sonnet."""
    if not model:
        return DEFAULT_PRICE
    m = model.lower()
    m = _VERSION_DASH.sub(lambda x: f"{x.group(1)}-{x.group(2)}.{x.group(3)}", m)
    m = _GPT_DASH.sub(lambda x: f"gpt-{x.group(1)}.{x.group(2)}", m)
    for key in sorted(MODEL_PRICES.keys(), key=len, reverse=True):
        if key in m:
            return MODEL_PRICES[key]
    if "opus" in m:
        return MODEL_PRICES["opus-4.7"]
    if "sonnet" in m:
        return MODEL_PRICES["sonnet-4.6"]
    if "haiku" in m:
        return MODEL_PRICES["haiku-4.5"]
    if "gpt-5" in m or m.startswith("o4"):
        return MODEL_PRICES["gpt-5"]
    if "gpt-4" in m or m.startswith("o3"):
        return MODEL_PRICES["gpt-4.1"]
    return DEFAULT_PRICE


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
    p_in, p_5m, p_1h, p_cr, p_out = price_for_model(model)
    return (
        input_tokens * p_in
        + cache_creation_5m * p_5m
        + cache_creation_1h * p_1h
        + cache_read * p_cr
        + output_tokens * p_out
    ) / 1_000_000


def _project_dir_for_key(project_key: str) -> str | None:
    """Map a project_key to its corpus.db project_dir.

    Post-normalization, all adapters store the real cwd (matching state.db's
    source_repo verbatim), so this is just a lookup. Returns None if the
    project isn't tracked or the state DB doesn't exist yet."""
    if not CORPUS_DB.exists():
        return None
    state_db = ROOT / "state.db"
    if not state_db.exists():
        return None
    try:
        with sqlite3.connect(str(state_db)) as conn:
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
    state_db = ROOT / "state.db"
    if not state_db.exists():
        return []
    try:
        with sqlite3.connect(str(state_db)) as conn:
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
    import state as _state
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
    days = weeks * 7
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


if __name__ == "__main__":
    # Quick CLI for dev: python metrics.py <project_key>
    import sys
    if len(sys.argv) < 2:
        print("usage: python metrics.py <project_key>")
        sys.exit(1)
    rows = daily_metrics(sys.argv[1], days=30)
    for r in rows[:10]:
        print(f"  {r['date']}  sessions={r['sessions']:>2}  prompts={r['prompts']:>3}  errors={r['tool_errors']:>2}  ${r['cost_usd']:>5.2f}  suggestions={r['suggestions_fired']:>2}  uptake={r['uptake']}")
