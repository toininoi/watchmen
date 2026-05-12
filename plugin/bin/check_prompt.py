#!/usr/bin/env python3
"""UserPromptSubmit hook handler.

Reads the prompt from the hook event stdin, FTS5-matches it against the indexed
`when_to_use` triggers for the project resolved from CWD, and writes a
suggestion file at ~/.watchmen/state/<project>.suggestion.json that the
statusLine reads on its next refresh (after the assistant responds).

If no match passes the threshold, any prior suggestion for this project is
cleared — each prompt either has a current suggestion or none.

Never writes to stdout. The hook wrapper redirects all output so nothing
leaks into the agent's context (this is the "inform, don't manipulate" rule).
"""

import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

WATCHMEN = Path.home() / ".watchmen"
INDEX_DB = WATCHMEN / "skill_index.db"
PROJECTS_INDEX = WATCHMEN / "projects.json"
STATE_DIR = WATCHMEN / "state"

# BM25 returns negative numbers; more negative = more relevant. -0.5 keeps the
# bar fairly high; tune as we observe false positives.
SCORE_THRESHOLD = -0.5

STOP_WORDS = {
    "the", "and", "for", "with", "that", "this", "from", "what", "when", "where",
    "how", "can", "should", "would", "could", "you", "your", "please", "help",
    "need", "want", "make", "just", "any", "all", "let", "into", "onto", "also",
    "but", "not", "get", "got", "have", "has", "are", "was", "were", "will",
    "did", "does", "doing", "done", "use", "using", "used", "try", "tried",
    "give", "gave", "take", "took", "set", "let", "now", "then", "than",
}


def resolve_project_key(cwd: str) -> str | None:
    if not PROJECTS_INDEX.exists() or not cwd:
        return None
    try:
        projects = json.loads(PROJECTS_INDEX.read_text())
        cwd_path = Path(cwd).resolve()
    except (json.JSONDecodeError, OSError):
        return None
    if not cwd_path.exists():
        return None
    best: tuple[int, str] | None = None
    for p in projects:
        repo = p.get("source_repo")
        key = p.get("project_key")
        if not (repo and key):
            continue
        try:
            repo_abs = Path(repo).resolve()
            if not repo_abs.exists():
                continue
        except OSError:
            continue
        for c in [cwd_path, *cwd_path.parents]:
            try:
                if c.samefile(repo_abs):
                    if best is None or len(str(repo_abs)) > best[0]:
                        best = (len(str(repo_abs)), key)
                    break
            except OSError:
                continue
    return best[1] if best else None


def sanitize_fts_query(text: str) -> str:
    """Reduce a prompt to keyword tokens safe for FTS5 MATCH. Returns OR-joined
    tokens — let BM25 rank by how many fire."""
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9]{2,}", text or "")
    keep = [t.lower() for t in tokens if t.lower() not in STOP_WORDS]
    if not keep:
        return ""
    # FTS5 escape: quote each token to disable operator parsing.
    return " OR ".join(f'"{t}"' for t in keep[:20])


def write_suggestion(project_key: str, suggestion: dict | None) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    target = STATE_DIR / f"{project_key}.suggestion.json"
    if suggestion is None:
        target.unlink(missing_ok=True)
        return
    target.write_text(json.dumps(suggestion, indent=2))


def main() -> int:
    raw = sys.stdin.read()
    if not raw:
        return 0
    try:
        evt = json.loads(raw)
    except json.JSONDecodeError:
        return 0
    if evt.get("hook_event_name") != "UserPromptSubmit":
        return 0

    prompt = (evt.get("prompt") or "").strip()
    cwd = evt.get("cwd") or os.getcwd()
    project_key = resolve_project_key(cwd)
    if not project_key:
        return 0

    if not INDEX_DB.exists():
        return 0

    query = sanitize_fts_query(prompt)
    if not query:
        write_suggestion(project_key, None)
        return 0

    try:
        with sqlite3.connect(str(INDEX_DB)) as conn:
            row = conn.execute(
                "SELECT skill_slug, bm25(skill_match) AS score "
                "FROM skill_match "
                "WHERE skill_match MATCH ? AND project_key = ? "
                "ORDER BY score LIMIT 1",
                (query, project_key),
            ).fetchone()
    except sqlite3.Error:
        return 0

    if not row:
        write_suggestion(project_key, None)
        return 0

    skill_slug, score = row
    if score is None or score > SCORE_THRESHOLD:
        write_suggestion(project_key, None)
        return 0

    write_suggestion(project_key, {
        "schema": 1,
        "ts": time.strftime("%Y-%m-%dT%H:%M"),
        "skill_slug": skill_slug,
        "score": round(score, 3),
        "prompt_excerpt": prompt[:140] + ("…" if len(prompt) > 140 else ""),
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
