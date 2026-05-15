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

import os
import shutil
from pathlib import Path


PROJECT_ROOT = Path(__file__).parent
WATCHMEN_HOME = Path(os.environ.get("WATCHMEN_HOME", Path.home() / ".watchmen")).expanduser()


def runtime_dir(*parts: str) -> Path:
    """Directory for user-owned watchmen runtime data.

    Source checkouts and installed wheels should stay immutable. Generated
    databases, analyses, and curated artifacts live under ~/.watchmen by
    default, with WATCHMEN_HOME available for tests or alternate installs.
    """
    path = WATCHMEN_HOME.joinpath(*parts)
    path.mkdir(parents=True, exist_ok=True)
    return path


def runtime_path(name: str, *, migrate_legacy: bool = True, legacy_alias: str | None = None) -> Path:
    """Path under WATCHMEN_HOME, optionally copied from the old source-root
    location on first use. Copying preserves existing local data without
    destructively moving files out of a checkout.

    `legacy_alias` covers in-place renames: if the new name doesn't exist on
    disk but the alias does (under WATCHMEN_HOME), move the alias to the new
    name. Used for the kai_claude → bundles rename in 0.5.
    """
    dest = WATCHMEN_HOME / name
    if migrate_legacy and not dest.exists():
        # 1. In-place rename inside WATCHMEN_HOME (kai_claude → bundles)
        if legacy_alias:
            alias_path = WATCHMEN_HOME / legacy_alias
            if alias_path.exists():
                try:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    alias_path.rename(dest)
                    return dest
                except OSError:
                    pass
        # 2. Copy from old source-checkout location into WATCHMEN_HOME
        legacy = PROJECT_ROOT / name
        try:
            if legacy.is_dir():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(legacy, dest)
            elif legacy.is_file():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(legacy, dest)
        except OSError:
            # Runtime paths should not make imports or command startup fail.
            pass
    return dest


STATE_DB = runtime_path("state.db")
CORPUS_DB = runtime_path("corpus.db")
EVENTS_DB = runtime_path("events.db")
EVENTS_JSONL = runtime_path("events.jsonl")
ANALYSES_DIR = runtime_path("analyses")
# Renamed from `kai_claude` → `bundles` in 0.5 (Kai attestation scrub).
# `legacy_alias` migrates existing installs by renaming the dir on first import.
BUNDLES_DIR = runtime_path("bundles", legacy_alias="kai_claude")
OUTPUT_DIR = runtime_path("output")
INSIGHTS_DIR = runtime_dir("insights")


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
