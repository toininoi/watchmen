"""User-installed skill harness reader.

The curator's candidate finder runs in isolation by default — it suggests
skills from session evidence without knowing which skills the user already
has installed in their Claude Code harness. That blind spot leads to (a)
duplicate proposals (`gpu-pod-sniping` proposed when the user already has
`provision-prime-gpu`) and (b) missed continual-learning opportunities
(propose a new monolithic `plan-and-implement` instead of composing the
user's existing `craft-plan` + `implement`).

This module reads `~/.claude/skills/*/SKILL.md` and returns a structured
list the candidate finder can reference. Each entry includes the skill's
slug, name, one-line description, and full frontmatter so the LLM can
decide whether a proposal duplicates, enhances, or composes an existing
skill.

Surface area is intentionally tiny: one public function (`installed_skills`)
and one filter helper (`overlaps_existing`). Caching is per-process — the
harness rarely changes within a single curator run.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

# Default location for user-installed Claude Code skills. Users with a
# custom HOME can still override via the env reader (kept simple here —
# the canonical path covers >99% of installs).
HARNESS_SKILLS_DIR = Path.home() / ".claude" / "skills"

# Inline YAML-frontmatter parser. We avoid PyYAML for a 3-key extraction.
_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
_KV_RE = re.compile(r"^([a-zA-Z_][a-zA-Z0-9_-]*)\s*:\s*(.*)$")


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Return a dict from the leading YAML-style frontmatter block.
    Tolerant of missing/malformed blocks — returns {} rather than raising
    so a single broken SKILL.md doesn't poison the whole harness read."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    out: dict[str, str] = {}
    for line in m.group(1).splitlines():
        m2 = _KV_RE.match(line.strip())
        if not m2:
            continue
        out[m2.group(1).strip().lower()] = m2.group(2).strip().strip('"').strip("'")
    return out


def installed_skills(skills_dir: Path | None = None) -> list[dict]:
    """List every `~/.claude/skills/<slug>/SKILL.md` and return one dict per
    skill: {slug, name, description, when_to_use, path}.

    Skills with no SKILL.md or unreadable frontmatter are skipped (not
    fatal). The path lets callers print a file:line reference if the LLM
    wants to enhance the file.

    Empty list when the harness dir doesn't exist (e.g. user never
    installed any Claude Code skills) — caller treats that as "no harness
    context to inject", which is the safe default."""
    base = skills_dir if skills_dir is not None else HARNESS_SKILLS_DIR
    if not base.exists() or not base.is_dir():
        return []
    out: list[dict] = []
    for skill_dir in sorted(base.iterdir()):
        if not skill_dir.is_dir():
            continue
        sk = skill_dir / "SKILL.md"
        if not sk.exists():
            continue
        try:
            text = sk.read_text(encoding="utf-8")
        except OSError:
            continue
        fm = _parse_frontmatter(text)
        out.append({
            "slug": fm.get("name") or skill_dir.name,
            "name": fm.get("name") or skill_dir.name,
            "description": fm.get("description", "").strip(),
            "when_to_use": fm.get("when_to_use", "").strip(),
            "path": str(sk),
        })
    return out


def overlaps_existing(slug: str, installed: Iterable[dict]) -> dict | None:
    """Return the installed skill that overlaps with `slug` (case-insensitive
    slug match), or None. Used by `--skip-overlap` to drop overlapping
    candidates before Stage 2 runs (the prompt-side `enhancement_of` route
    is the default path; this is the harder "ignore entirely" opt-out)."""
    s = slug.strip().lower()
    if not s:
        return None
    for entry in installed:
        if (entry.get("slug") or "").strip().lower() == s:
            return entry
    return None


def format_for_prompt(installed: list[dict], limit: int = 40) -> str:
    """Render the installed-skills list as a compact bullet block for the
    candidate finder system prompt. Caps at `limit` entries so a power
    user with 80 installed skills doesn't blow the prompt budget — the
    most common case is <20 skills. Returns '' when nothing's installed
    so callers can `if block: prompt += block` cleanly."""
    if not installed:
        return ""
    rows = installed[:limit]
    lines = ["The user has these skills already installed in their Claude Code harness:"]
    for s in rows:
        desc = s.get("description") or "(no description)"
        # Trim each line so a long when_to_use doesn't dominate the block.
        lines.append(f"  - {s['slug']}: {desc[:140]}")
    if len(installed) > limit:
        lines.append(f"  … and {len(installed) - limit} more (truncated for brevity)")
    return "\n".join(lines)
