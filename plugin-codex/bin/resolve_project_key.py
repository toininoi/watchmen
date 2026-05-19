#!/usr/bin/env python3
"""Resolve a CWD to a watchmen project_key by reading ~/.watchmen/projects.json.

The watchmen engine writes that index whenever a project is tracked or a curator
run completes, so the plugin never needs to know where the engine is installed.

Uses Path.samefile() to compare cwd against each tracked source_repo so case
differences on case-insensitive filesystems (macOS APFS default) and symlinks
don't cause false negatives.

Prints the project_key to stdout (followed by a newline), or nothing if no match.
Always exits 0 — callers treat empty output as "no project here".
"""

import json
import os
import sys
from pathlib import Path

INDEX = Path.home() / ".watchmen" / "projects.json"


def main() -> int:
    raw_cwd = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    try:
        cwd = Path(raw_cwd).resolve()
    except OSError:
        return 0
    if not cwd.exists() or not INDEX.exists():
        return 0
    try:
        projects = json.loads(INDEX.read_text())
    except (json.JSONDecodeError, OSError):
        return 0

    # Candidates: cwd itself + every ancestor. We match cwd against tracked
    # source_repo paths; longest match wins so nested tracked repos still
    # resolve to the most specific one.
    candidates = [cwd, *cwd.parents]

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
        for c in candidates:
            try:
                if c.samefile(repo_abs):
                    match_len = len(str(repo_abs))
                    if best is None or match_len > best[0]:
                        best = (match_len, key)
                    break
            except OSError:
                continue
    if best:
        print(best[1])
    return 0


if __name__ == "__main__":
    sys.exit(main())
