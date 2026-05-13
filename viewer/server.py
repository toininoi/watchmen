"""watchmen viewer — local FastAPI dashboard for browsing analyses + skill bundles + CLAUDE.md."""

import shutil
import sqlite3
import subprocess
from pathlib import Path

import markdown as md
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates

ROOT = Path(__file__).parent.parent  # kai-hooks-mvp/
ANALYSES = ROOT / "analyses"
KAI_CLAUDE = ROOT / "kai_claude"
STATE_DB = ROOT / "state.db"
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


MD_EXTENSIONS = ["fenced_code", "tables", "codehilite", "toc", "sane_lists", "nl2br"]
MD_CONFIG = {"codehilite": {"css_class": "codehilite", "guess_lang": True}}


def render_md(text: str) -> str:
    return md.markdown(text, extensions=MD_EXTENSIONS, extension_configs=MD_CONFIG)


def _db():
    if not STATE_DB.exists():
        return None
    c = sqlite3.connect(STATE_DB)
    c.row_factory = sqlite3.Row
    return c


def list_tracked_projects() -> list[dict]:
    c = _db()
    if not c:
        return []
    rows = c.execute("SELECT * FROM projects ORDER BY project_key").fetchall()
    c.close()
    return [dict(r) for r in rows]


def get_project_meta(project_key: str) -> dict | None:
    c = _db()
    if not c:
        return None
    row = c.execute("SELECT * FROM projects WHERE project_key = ?", (project_key,)).fetchone()
    c.close()
    return dict(row) if row else None


def list_skills(project_key: str) -> list[dict]:
    skills_dir = KAI_CLAUDE / project_key / "skills"
    if not skills_dir.exists():
        return []
    out = []
    for d in sorted(skills_dir.iterdir()):
        if not d.is_dir():
            continue
        skill_md_path = d / "SKILL.md"
        description = ""
        if skill_md_path.exists():
            text = skill_md_path.read_text(encoding="utf-8", errors="replace")
            for line in text.splitlines():
                if line.startswith("description:"):
                    description = line.split(":", 1)[1].strip()
                    break
                if line.startswith("# "):
                    description = line[2:].strip()
                    break
        scripts_dir = d / "scripts"
        script_count = sum(1 for f in scripts_dir.glob("*") if f.is_file()) if scripts_dir.exists() else 0
        out.append({
            "slug": d.name,
            "description": description,
            "script_count": script_count,
            "has_skill_md": skill_md_path.exists(),
        })
    return out


def list_thesis_days(project_key: str) -> list[dict]:
    d = ANALYSES / project_key
    if not d.exists():
        return []
    days = []
    for f in sorted(d.glob("20*.md")):
        days.append({
            "day": f.stem,
            "size": f.stat().st_size,
        })
    return days


def list_recent_runs(limit: int = 30) -> list[dict]:
    c = _db()
    if not c:
        return []
    rows = c.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    c.close()
    return [dict(r) for r in rows]


# ─── App ────────────────────────────────────────────────────────────────────

app = FastAPI(title="watchmen viewer")


@app.get("/healthz", response_class=PlainTextResponse)
def healthz() -> str:
    return "ok"


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    projects = list_tracked_projects()
    summaries = []
    for p in projects:
        skills = list_skills(p["project_key"])
        days = list_thesis_days(p["project_key"])
        claude_md = KAI_CLAUDE / p["project_key"] / "CLAUDE.md"
        summaries.append({
            **p,
            "skill_count": len(skills),
            "thesis_day_count": len(days),
            "claude_md_size": claude_md.stat().st_size if claude_md.exists() else 0,
        })
    runs = list_recent_runs(limit=10)

    # Read the latest CHANGELOG.md entry + current version so dashboard.html
    # can render a "What's new in vX.Y" banner. JS dismisses + remembers in
    # localStorage so each new release announces itself exactly once per
    # user-browser. Falls back to no banner when the file is missing.
    changelog_version: str | None = None
    changelog_body_html: str | None = None
    try:
        import sys
        sys.path.insert(0, str(ROOT))
        import cli as _cli  # type: ignore
        changelog_version = _cli._version()
        changelog_path = ROOT / "CHANGELOG.md"
        if changelog_path.exists():
            entries = _cli._parse_changelog(changelog_path.read_text())
            for v, body in entries:
                if v == changelog_version:
                    changelog_body_html = md.markdown(body, extensions=["fenced_code"])
                    break
    except Exception:
        pass

    return TEMPLATES.TemplateResponse(request, "dashboard.html", {
        "projects": summaries,
        "runs": runs,
        "changelog_version": changelog_version,
        "changelog_body_html": changelog_body_html,
    })


@app.get("/p/{project_key}", response_class=HTMLResponse)
def project_page(request: Request, project_key: str):
    proj = get_project_meta(project_key)
    if not proj:
        raise HTTPException(404, f"project {project_key} not tracked")
    claude_md_path = KAI_CLAUDE / project_key / "CLAUDE.md"
    claude_md_html = render_md(claude_md_path.read_text(encoding="utf-8")) if claude_md_path.exists() else None
    skills = list_skills(project_key)
    days = list_thesis_days(project_key)
    return TEMPLATES.TemplateResponse(request, "project.html", {
        "project": proj,
        "claude_md": claude_md_html,
        "skills": skills,
        "thesis_days": days,
    })


@app.get("/p/{project_key}/skills/{skill_slug}", response_class=HTMLResponse)
def skill_page(request: Request, project_key: str, skill_slug: str):
    skill_dir = KAI_CLAUDE / project_key / "skills" / skill_slug
    if not skill_dir.exists():
        raise HTTPException(404, f"skill {skill_slug} not found")
    skill_md_path = skill_dir / "SKILL.md"
    skill_md_html = render_md(skill_md_path.read_text(encoding="utf-8")) if skill_md_path.exists() else None
    files = []
    for f in sorted(skill_dir.rglob("*")):
        if f.is_file() and f.name != "SKILL.md":
            files.append({
                "rel": str(f.relative_to(skill_dir)),
                "size": f.stat().st_size,
            })
    return TEMPLATES.TemplateResponse(request, "skill.html", {
        "project_key": project_key,
        "skill_slug": skill_slug,
        "skill_md": skill_md_html,
        "files": files,
    })


@app.get("/p/{project_key}/skills/{skill_slug}/files/{file_rel:path}", response_class=HTMLResponse)
def skill_file(request: Request, project_key: str, skill_slug: str, file_rel: str):
    skill_dir = KAI_CLAUDE / project_key / "skills" / skill_slug
    target = (skill_dir / file_rel).resolve()
    try:
        target.relative_to(skill_dir.resolve())
    except ValueError:
        raise HTTPException(400, "path traversal blocked")
    if not target.exists():
        raise HTTPException(404)
    text = target.read_text(encoding="utf-8", errors="replace")
    suffix = target.suffix.lstrip(".")
    lang_map = {"py": "python", "sh": "bash", "md": "markdown", "yml": "yaml", "yaml": "yaml", "json": "json", "js": "javascript", "ts": "typescript", "tsx": "tsx", "toml": "toml"}
    lang = lang_map.get(suffix, "text")
    fenced = f"```{lang}\n{text}\n```"
    rendered = render_md(fenced)
    return TEMPLATES.TemplateResponse(request, "file.html", {
        "project_key": project_key,
        "skill_slug": skill_slug,
        "file_rel": file_rel,
        "file_html": rendered,
        "byte_size": len(text),
    })


@app.get("/p/{project_key}/thesis", response_class=HTMLResponse)
def thesis_index(request: Request, project_key: str):
    days = list_thesis_days(project_key)
    running = ANALYSES / project_key / "_running.md"
    running_html = render_md(running.read_text(encoding="utf-8")) if running.exists() else None
    return TEMPLATES.TemplateResponse(request, "thesis.html", {
        "project_key": project_key,
        "days": days,
        "running_html": running_html,
    })


@app.get("/p/{project_key}/thesis/{day}", response_class=HTMLResponse)
def thesis_day(request: Request, project_key: str, day: str):
    f = ANALYSES / project_key / f"{day}.md"
    if not f.exists():
        raise HTTPException(404)
    return TEMPLATES.TemplateResponse(request, "thesis_day.html", {
        "project_key": project_key,
        "day": day,
        "html": render_md(f.read_text(encoding="utf-8")),
    })


@app.get("/runs", response_class=HTMLResponse)
def runs_page(request: Request):
    runs = list_recent_runs(limit=200)
    return TEMPLATES.TemplateResponse(request, "runs.html", {
        "runs": runs,
    })


@app.get("/insights", response_class=HTMLResponse)
def insights_page(request: Request):
    """HTML version of `watchmen insights`. Same static aggregation +
    cached deep digest as the CLI, but with richer charts (per-repo
    activity sparklines, top erroring tools, frustration markers,
    aggregate hour-of-day heatmap) that don't fit a terminal."""
    import json as _json
    import sys as _sys
    import metrics as _metrics

    # Reach into cli.py for the friction-signal + adapter helpers + the
    # digest cache reader. Keeps the viewer thin without duplicating those
    # SQL queries / parser helpers.
    _sys.path.insert(0, str(ROOT))
    import cli as _cli  # type: ignore

    state_init = getattr(__import__("state"), "init_db", None)
    if state_init:
        state_init()
    import state as _state

    projects = _state.list_projects()
    base = KAI_CLAUDE

    # Adapter totals across the whole corpus.
    adapter_totals: dict[str, int] = {}
    corpus_db = ROOT / "corpus.db"
    if corpus_db.exists():
        cc = sqlite3.connect(str(corpus_db))
        for agent, n in cc.execute(
            "SELECT agent, COUNT(*) FROM sessions WHERE is_subagent = 0 GROUP BY agent ORDER BY 2 DESC"
        ).fetchall():
            adapter_totals[agent] = n
        cc.close()

    # Per-repo rows with sparklines + friction signals.
    repos: list[dict] = []
    for p in projects:
        key = p["project_key"]
        skills_dir = base / key / "skills"
        skills_n = sum(1 for d in skills_dir.iterdir() if d.is_dir()) if skills_dir.exists() else 0
        pending_dir = base / key / "_pending"
        pending_n = sum(1 for d in pending_dir.iterdir() if d.is_dir()) if pending_dir.exists() else 0
        adapter = _cli._adapter_breakdown(key)
        tool_errors, top_error_tools, frust_count, frust_samples = _cli._repo_friction_signals(key)
        daily = _metrics.daily_metrics(key, days=30) or []
        sess_series = [r["sessions"] for r in reversed(daily)]
        try:
            prog = _state.get_project_progress(key)
            pending_prompts = prog.get("new_prompts_since_last_analysis", 0) or 0
        except Exception:
            pending_prompts = 0
        repos.append({
            "key": key,
            "skills_n": skills_n,
            "pending_n": pending_n,
            "adapter": adapter,
            "tool_errors": tool_errors,
            "top_error_tools": top_error_tools,
            "frust_count": frust_count,
            "frust_samples": frust_samples,
            "sess_spark": _metrics.sparkline_svg(sess_series, color="#4f46e5", width=140, height=30),
            "pending_prompts": pending_prompts,
            "total_sess": sum(adapter.values()),
            "tool_chart": _metrics.hbar_chart_svg(
                [(t, n) for t, n in top_error_tools],
                color="#dc2626", label_width=110, width=340,
            ) if top_error_tools else "",
        })
    repos.sort(key=lambda r: (-r["skills_n"], -r["total_sess"]))

    # Cross-repo candidate-slug overlaps.
    pattern_idx: dict[str, list[tuple[str, str]]] = {}
    for p in projects:
        key = p["project_key"]
        cand_path = base / key / "_candidates.json"
        skills_dir = base / key / "skills"
        existing = {d.name for d in skills_dir.iterdir() if d.is_dir()} if skills_dir.exists() else set()
        if not cand_path.exists():
            continue
        try:
            cands = _json.loads(cand_path.read_text())
        except Exception:
            continue
        for c in cands:
            slug = c.get("slug")
            if not slug:
                continue
            status = "curated" if slug in existing else "candidate"
            pattern_idx.setdefault(slug, []).append((key, status))
    cross = [(slug, hits) for slug, hits in pattern_idx.items() if len(hits) >= 2]
    cross.sort(key=lambda x: (-len(x[1]), x[0]))

    untapped = [(r["key"], r["total_sess"]) for r in repos if r["skills_n"] == 0 and r["total_sess"] > 0]
    untapped.sort(key=lambda x: -x[1])

    # Aggregate per-repo chart for cross-repo comparison (frustration totals).
    frust_chart = _metrics.hbar_chart_svg(
        sorted([(r["key"], r["frust_count"]) for r in repos if r["frust_count"] > 0],
               key=lambda x: -x[1])[:8],
        color="#eab308", label_width=140, width=440,
    )
    errors_chart = _metrics.hbar_chart_svg(
        sorted([(r["key"], r["tool_errors"]) for r in repos if r["tool_errors"] > 0],
               key=lambda x: -x[1])[:8],
        color="#dc2626", label_width=140, width=440,
    )

    # Aggregate metrics (rollup window + heatmap) — reuse what /metrics builds.
    aggregate_rows = _metrics.daily_metrics_all(days=30, tracked_only=False)
    last7 = _metrics.summarize_window(aggregate_rows, 7)
    last30 = _metrics.summarize_window(aggregate_rows, 30)
    series = list(reversed(aggregate_rows))
    sparks = {
        "sessions":    _metrics.sparkline_svg([r["sessions"] for r in series], color="#4f46e5"),
        "prompts":     _metrics.sparkline_svg([r["prompts"] for r in series], color="#0891b2"),
        "tool_errors": _metrics.sparkline_svg([r["tool_errors"] for r in series], color="#dc2626"),
        "cost_usd":    _metrics.sparkline_svg([r["cost_usd"] for r in series], color="#ea580c"),
    }
    hour_dow = _metrics.activity_by_hour_dow_all(days=90, tracked_only=False)
    hour_dow_svg = _metrics.hour_dow_heatmap_svg(hour_dow)

    # Latest cached deep digest from ~/.watchmen/insights/.
    digest_html = None
    digest_meta: dict = {}
    try:
        latest = _cli._latest_digest_path()
        if latest is not None:
            meta, body = _cli._read_digest_metadata(latest)
            digest_meta = meta
            digest_html = render_md(body)
    except Exception:
        digest_html = None

    return TEMPLATES.TemplateResponse(request, "insights.html", {
        "adapter_totals": adapter_totals,
        "total_sessions": sum(adapter_totals.values()),
        "repos": repos,
        "cross": cross,
        "untapped": untapped,
        "frust_chart": frust_chart,
        "errors_chart": errors_chart,
        "last7": last7,
        "last30": last30,
        "sparks": sparks,
        "hour_dow_svg": hour_dow_svg,
        "digest_html": digest_html,
        "digest_meta": digest_meta,
        "curated_count": sum(1 for r in repos if r["skills_n"] > 0),
        "n_projects": len(projects),
        "total_skills": sum(r["skills_n"] for r in repos),
        "total_pending": sum(r["pending_n"] for r in repos),
        "total_errors": sum(r["tool_errors"] for r in repos),
        "total_frustration": sum(r["frust_count"] for r in repos),
    })


@app.get("/metrics", response_class=HTMLResponse)
def metrics_all(request: Request, tracked: int = 0):
    import metrics as _metrics

    tracked_only = bool(tracked)
    rows = _metrics.daily_metrics_all(days=30, tracked_only=tracked_only)
    last7 = _metrics.summarize_window(rows, 7)
    last30 = _metrics.summarize_window(rows, 30)
    series = list(reversed(rows))
    sparks = {
        "sessions":     _metrics.sparkline_svg([r["sessions"] for r in series], color="#4f46e5"),
        "prompts":      _metrics.sparkline_svg([r["prompts"] for r in series], color="#0891b2"),
        "input_tokens": _metrics.sparkline_svg([r["input_tokens"] for r in series], color="#0891b2"),
        "output_tokens":_metrics.sparkline_svg([r["output_tokens"] for r in series], color="#15803d"),
        "tool_errors":  _metrics.sparkline_svg([r["tool_errors"] for r in series], color="#dc2626"),
        "cost_usd":     _metrics.sparkline_svg([r["cost_usd"] for r in series], color="#ea580c"),
        "suggestions":  _metrics.sparkline_svg([r["suggestions_fired"] for r in series], color="#a855f7"),
    }
    calendar = _metrics.activity_calendar_all(weeks=26, tracked_only=tracked_only)
    hour_dow = _metrics.activity_by_hour_dow_all(days=90, tracked_only=tracked_only)
    calendar_svg = _metrics.calendar_heatmap_svg(calendar, weeks=26)
    hour_dow_svg = _metrics.hour_dow_heatmap_svg(hour_dow)
    peaks = []
    flat = [(dow, hr, hour_dow[dow][hr]) for dow in range(7) for hr in range(24)]
    flat.sort(key=lambda t: t[2], reverse=True)
    if flat and flat[0][2] > 0:
        peak_dow, peak_hr, peak_n = flat[0]
        peaks = [["Sun","Mon","Tue","Wed","Thu","Fri","Sat"][peak_dow], f"{peak_hr:02d}:00", peak_n]
    per_project = _metrics.per_project_totals(days=30)
    tool_usage = _metrics.tool_usage(project_key=None, days=30, tracked_only=tracked_only)
    streak = _metrics.streak_stats(project_key=None, weeks=26, tracked_only=tracked_only)

    return TEMPLATES.TemplateResponse(request, "metrics_all.html", {
        "rows": rows,
        "last7": last7,
        "last30": last30,
        "sparks": sparks,
        "calendar_svg": calendar_svg,
        "hour_dow_svg": hour_dow_svg,
        "peaks": peaks,
        "per_project": per_project,
        "tracked_only": tracked_only,
        "tool_usage": tool_usage,
        "streak": streak,
    })


@app.get("/p/{project_key}/metrics", response_class=HTMLResponse)
def project_metrics(request: Request, project_key: str):
    import metrics as _metrics

    rows = _metrics.daily_metrics(project_key, days=30)
    last7 = _metrics.summarize_window(rows, 7)
    last30 = _metrics.summarize_window(rows, 30)
    # Daily series in chronological order for sparklines (rows is newest-first).
    series = list(reversed(rows))
    sparks = {
        "sessions":     _metrics.sparkline_svg([r["sessions"] for r in series], color="#4f46e5"),
        "prompts":      _metrics.sparkline_svg([r["prompts"] for r in series], color="#0891b2"),
        "input_tokens": _metrics.sparkline_svg([r["input_tokens"] for r in series], color="#0891b2"),
        "output_tokens":_metrics.sparkline_svg([r["output_tokens"] for r in series], color="#15803d"),
        "tool_errors":  _metrics.sparkline_svg([r["tool_errors"] for r in series], color="#dc2626"),
        "cost_usd":     _metrics.sparkline_svg([r["cost_usd"] for r in series], color="#ea580c"),
        "suggestions":  _metrics.sparkline_svg([r["suggestions_fired"] for r in series], color="#a855f7"),
    }
    calendar = _metrics.activity_calendar(project_key, weeks=26)
    hour_dow = _metrics.activity_by_hour_dow(project_key, days=90)
    calendar_svg = _metrics.calendar_heatmap_svg(calendar, weeks=26)
    hour_dow_svg = _metrics.hour_dow_heatmap_svg(hour_dow)

    # Peak hour + day for the summary line under the heatmap
    peaks = []
    flat = [(dow, hr, hour_dow[dow][hr]) for dow in range(7) for hr in range(24)]
    flat.sort(key=lambda t: t[2], reverse=True)
    if flat and flat[0][2] > 0:
        peak_dow, peak_hr, peak_n = flat[0]
        peaks = [["Sun","Mon","Tue","Wed","Thu","Fri","Sat"][peak_dow], f"{peak_hr:02d}:00", peak_n]

    tool_usage = _metrics.tool_usage(project_key=project_key, days=30)
    streak = _metrics.streak_stats(project_key=project_key, weeks=26)

    return TEMPLATES.TemplateResponse(request, "metrics.html", {
        "project": get_project_meta(project_key) or {"project_key": project_key},
        "rows": rows,
        "last7": last7,
        "last30": last30,
        "sparks": sparks,
        "calendar_svg": calendar_svg,
        "hour_dow_svg": hour_dow_svg,
        "peaks": peaks,
        "tool_usage": tool_usage,
        "streak": streak,
    })


def _project_git_dir(project_key: str) -> Path | None:
    pdir = KAI_CLAUDE / project_key
    if not pdir.exists() or not (pdir / ".git").exists():
        return None
    return pdir


@app.get("/p/{project_key}/runs", response_class=HTMLResponse)
def project_runs(request: Request, project_key: str):
    pdir = _project_git_dir(project_key)
    if pdir is None:
        raise HTTPException(404, detail="no run history yet — curator hasn't committed anything for this project")
    if not shutil.which("git"):
        raise HTTPException(500, detail="git not available")
    r = subprocess.run(
        ["git", "-C", str(pdir), "log", "--pretty=format:%H%x09%ai%x09%s", "-n", "50"],
        capture_output=True, text=True,
    )
    runs = []
    for line in (r.stdout or "").strip().split("\n"):
        if not line:
            continue
        sha, ai_ts, subject = (line.split("\t", 2) + ["", ""])[:3]
        runs.append({"sha": sha, "short": sha[:8], "ts": ai_ts, "subject": subject})
    return TEMPLATES.TemplateResponse(request, "project_runs.html", {
        "project": get_project_meta(project_key) or {"project_key": project_key},
        "runs": runs,
    })


@app.get("/p/{project_key}/diff/{sha}", response_class=HTMLResponse)
def project_diff(request: Request, project_key: str, sha: str):
    pdir = _project_git_dir(project_key)
    if pdir is None:
        raise HTTPException(404, detail="no run history for this project")
    if not shutil.which("git"):
        raise HTTPException(500, detail="git not available")

    # Commit metadata (subject + body)
    r = subprocess.run(
        ["git", "-C", str(pdir), "log", "-1", "--pretty=format:%H%n%ai%n%s%n--BODY--%n%b", sha],
        capture_output=True, text=True,
    )
    if r.returncode != 0 or not r.stdout.strip():
        raise HTTPException(404, detail=f"commit {sha} not found")
    parts = r.stdout.split("\n--BODY--\n", 1)
    head = parts[0].split("\n", 2)
    sha_full = head[0]
    ai_ts = head[1] if len(head) > 1 else ""
    subject = head[2] if len(head) > 2 else ""
    body = parts[1] if len(parts) > 1 else ""

    # Raw diff — let diff2html (client-side) render it as side-by-side or unified.
    r = subprocess.run(
        ["git", "-C", str(pdir), "show", "--pretty=", "--no-color", sha_full],
        capture_output=True, text=True,
    )
    diff_text = r.stdout or ""

    # Neighbors for prev navigation; absence indicates this is the initial commit.
    r_prev = subprocess.run(
        ["git", "-C", str(pdir), "rev-parse", f"{sha_full}^"],
        capture_output=True, text=True,
    )
    prev_sha = r_prev.stdout.strip() if r_prev.returncode == 0 else None
    is_initial = prev_sha is None

    return TEMPLATES.TemplateResponse(request, "diff.html", {
        "project": get_project_meta(project_key) or {"project_key": project_key},
        "commit": {
            "sha": sha_full,
            "short": sha_full[:8],
            "ts": ai_ts,
            "subject": subject,
            "body": body,
        },
        "diff_text": diff_text,
        "prev_sha": prev_sha,
        "is_initial": is_initial,
    })


def serve(host: str | None = None, port: int | None = None):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import config
    import uvicorn
    host = host or config.VIEWER_DEFAULT_HOST
    port = port if port is not None else config.viewer_port()
    print(f"\n  🌐 watchmen viewer running at http://{host}:{port}\n", flush=True)
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    serve()
