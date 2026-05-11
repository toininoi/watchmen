"""SessionStart brief — reads hook stdin, surfaces a macOS notification with what
changed in kai_claude/<project>/_changelog.md since this user last saw it.

Per the design: inform the user, do NOT inject context into the agent. Nothing is
written to stdout. All output goes to osascript Notification Center.

Hook script invokes this in the background and exits 0 immediately, so any work
here happens off the <200ms blocking budget.
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LAST_SEEN_FILE = Path.home() / ".watchmen" / "last_seen.json"
TS_FMT = "%Y-%m-%dT%H:%M"
ENTRY_HEADER = re.compile(
    r"^##\s+(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})\s*[—-]?\s*(.*)$",
    re.MULTILINE,
)


def project_key_from_cwd(cwd: str) -> str | None:
    """Resolve cwd → project_key via state.db.projects.source_repo. Returns the
    longest matching prefix so nested repos (rare) still resolve correctly."""
    db = ROOT / "state.db"
    if not db.exists() or not cwd:
        return None
    cwd_abs = str(Path(cwd).resolve())
    try:
        with sqlite3.connect(str(db)) as conn:
            rows = conn.execute("SELECT project_key, source_repo FROM projects").fetchall()
    except sqlite3.Error:
        return None
    best: tuple[int, str] | None = None
    for key, repo in rows:
        if not repo:
            continue
        repo_abs = str(Path(repo).resolve())
        if cwd_abs == repo_abs or cwd_abs.startswith(repo_abs + os.sep):
            if best is None or len(repo_abs) > best[0]:
                best = (len(repo_abs), key)
    return best[1] if best else None


def parse_entries(text: str) -> list[tuple[datetime, str, str]]:
    """Return [(timestamp, kind, body)] in file order (newest-first, as writer prepends)."""
    matches = list(ENTRY_HEADER.finditer(text))
    out: list[tuple[datetime, str, str]] = []
    for i, m in enumerate(matches):
        try:
            ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        kind = m.group(2).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out.append((ts, kind, text[start:end].strip()))
    return out


def load_last_seen() -> dict:
    if not LAST_SEEN_FILE.exists():
        return {}
    try:
        return json.loads(LAST_SEEN_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_last_seen(d: dict) -> None:
    LAST_SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    LAST_SEEN_FILE.write_text(json.dumps(d, indent=2, sort_keys=True))


def summarize(new_entries: list[tuple[datetime, str, str]]) -> str:
    """Build a single-line summary from the union of all new entries."""
    added: set[str] = set()
    updated: set[str] = set()
    removed: set[str] = set()
    section_re = re.compile(r"^\*\*(Added|Updated|Removed):\*\*\s*$")
    bucket_map = {"Added": added, "Updated": updated, "Removed": removed}
    for _ts, _kind, body in new_entries:
        current_bucket: set[str] | None = None
        for raw in body.splitlines():
            line = raw.strip()
            sec = section_re.match(line)
            if sec:
                current_bucket = bucket_map.get(sec.group(1))
                continue
            if current_bucket is not None and line.startswith("- "):
                current_bucket.add(line[2:].strip())
            elif not line:
                current_bucket = None

    parts: list[str] = []
    if added:
        sample = sorted(added)[0]
        parts.append(f"+{len(added)} ({sample})" if len(added) > 1 else f"new: {sample}")
    if updated:
        sample = sorted(updated)[0]
        parts.append(f"~{len(updated)} ({sample})" if len(updated) > 1 else f"updated: {sample}")
    if removed:
        parts.append(f"-{len(removed)}")
    return " · ".join(parts) or f"{len(new_entries)} curator run(s)"


def notify(title: str, body: str) -> None:
    title_q = title.replace("\\", "\\\\").replace('"', '\\"')
    body_q = body.replace("\\", "\\\\").replace('"', '\\"')
    script = f'display notification "{body_q}" with title "{title_q}" sound name "Pop"'
    try:
        subprocess.run(["osascript", "-e", script], check=False, timeout=3)
    except (subprocess.SubprocessError, FileNotFoundError):
        pass


def main() -> int:
    raw = sys.stdin.read()
    if not raw:
        return 0
    try:
        evt = json.loads(raw)
    except json.JSONDecodeError:
        return 0

    if evt.get("hook_event_name") != "SessionStart":
        return 0

    cwd = evt.get("cwd") or os.getcwd()
    project_key = project_key_from_cwd(cwd)
    if not project_key:
        return 0

    changelog = ROOT / "kai_claude" / project_key / "_changelog.md"
    if not changelog.exists():
        return 0

    last_seen = load_last_seen()
    last_str = last_seen.get(project_key)
    last_ts: datetime | None = None
    if last_str:
        try:
            last_ts = datetime.strptime(last_str, TS_FMT)
        except ValueError:
            last_ts = None

    entries = parse_entries(changelog.read_text())
    new_entries = [e for e in entries if last_ts is None or e[0] > last_ts]
    if not new_entries:
        return 0

    body = summarize(new_entries)
    notify(f"watchmen · {project_key}", body)

    last_seen[project_key] = datetime.now().strftime(TS_FMT)
    save_last_seen(last_seen)
    return 0


if __name__ == "__main__":
    sys.exit(main())
