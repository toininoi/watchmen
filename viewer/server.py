"""watchmen viewer — local FastAPI dashboard for browsing analyses + skill bundles + CLAUDE.md."""

import shutil
import sqlite3
import subprocess
from pathlib import Path

import markdown as md
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import DiffLexer

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
    return TEMPLATES.TemplateResponse(request, "dashboard.html", {
        "projects": summaries,
        "runs": runs,
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

    # Diff (excludes the commit header — we render that ourselves)
    r = subprocess.run(
        ["git", "-C", str(pdir), "show", "--pretty=", "--no-color", sha_full],
        capture_output=True, text=True,
    )
    diff_text = r.stdout or "(no diff — likely the initial commit)"
    diff_html = highlight(diff_text, DiffLexer(), HtmlFormatter(cssclass="codehilite", nowrap=False))

    # Neighbors for prev/next navigation
    r_prev = subprocess.run(
        ["git", "-C", str(pdir), "rev-parse", f"{sha_full}^"],
        capture_output=True, text=True,
    )
    prev_sha = r_prev.stdout.strip() if r_prev.returncode == 0 else None

    return TEMPLATES.TemplateResponse(request, "diff.html", {
        "project": get_project_meta(project_key) or {"project_key": project_key},
        "commit": {
            "sha": sha_full,
            "short": sha_full[:8],
            "ts": ai_ts,
            "subject": subject,
            "body": body,
        },
        "diff_html": diff_html,
        "prev_sha": prev_sha,
    })


def serve(host: str = "127.0.0.1", port: int = 8888):
    import uvicorn
    print(f"\n  🌐 watchmen viewer running at http://{host}:{port}\n", flush=True)
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    serve()
