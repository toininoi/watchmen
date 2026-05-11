#!/usr/bin/env python3
"""Resolve a CWD to a watchmen project_key by reading ~/.watchmen/projects.json.

The watchmen engine writes that index whenever a project is tracked or a curator
run completes, so the plugin never needs to know where the engine is installed.

Prints the project_key to stdout (followed by a newline), or nothing if no match.
Always exits 0 — callers treat empty output as "no project here".
"""

import json
import os
import sys
from pathlib import Path

INDEX = Path.home() / ".watchmen" / "projects.json"


def main() -> int:
    cwd = Path(sys.argv[1] if len(sys.argv) > 1 else os.getcwd()).resolve()
    if not INDEX.exists():
        return 0
    try:
        projects = json.loads(INDEX.read_text())
    except (json.JSONDecodeError, OSError):
        return 0

    best: tuple[int, str] | None = None
    for p in projects:
        repo = p.get("source_repo")
        key = p.get("project_key")
        if not (repo and key):
            continue
        try:
            repo_abs = str(Path(repo).resolve())
        except OSError:
            continue
        cwd_str = str(cwd)
        if cwd_str == repo_abs or cwd_str.startswith(repo_abs + os.sep):
            if best is None or len(repo_abs) > best[0]:
                best = (len(repo_abs), key)
    if best:
        print(best[1])
    return 0


if __name__ == "__main__":
    sys.exit(main())
