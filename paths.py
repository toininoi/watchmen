"""Project path normalization.

Claude Code encodes the working directory into a folder name by replacing every
"/" with "-" and prefixing with "-": `/Users/x/dev/kai-frontend` →
`-Users-x-dev-kai-frontend`. The replacement is lossy — a dir name that already
contains "-" (like `kai-frontend`) ends up indistinguishable from a path
separator. We undo it by walking the real filesystem and trying the longest
possible joined segment at each level.

Codex stores the real cwd verbatim in `session_meta.cwd`, so no decoding needed.

When the original directory no longer exists on disk (project deleted, moved,
etc.), we fall back to a best-effort decode: leading "-" → "/", remaining "-"
→ "/". This is wrong for dirs with dashes in their names, but it's stable —
two transcripts from the same vanished project will still group together.
"""

from __future__ import annotations

from pathlib import Path


def decode_project_dir(encoded: str) -> str:
    """Map a Claude Code encoded project dir back to a real cwd.

    Returns a real path if the directory still exists on disk; otherwise
    returns the naive decode (leading "-" → "/", remaining "-" → "/")."""
    if not encoded.startswith("-"):
        return encoded  # already a real path
    resolved = _try_resolve_real_path("/" + encoded.lstrip("-").replace("-", "/"))
    if resolved:
        return str(resolved)
    return "/" + encoded.lstrip("-").replace("-", "/")


def _try_resolve_real_path(decoded: str) -> Path | None:
    parts = Path(decoded).parts
    if not parts or parts[0] != "/":
        return None
    cur = Path("/")
    i = 1
    while i < len(parts):
        children = [p for p in cur.iterdir() if p.is_dir()] if cur.exists() else []
        best = None
        best_j = i
        for j in range(len(parts), i, -1):
            candidate = "-".join(parts[i:j])
            for ch in children:
                if ch.name == candidate:
                    best = ch
                    best_j = j
                    break
            if best:
                break
        if not best:
            return None
        cur = best
        i = best_j
    return cur if cur.exists() else None
