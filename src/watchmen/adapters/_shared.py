"""Shared helpers used across the per-agent adapters.

Adapters live as separate modules (`claude_code.py`, `codex.py`, `pi.py`,
`opencode.py`) because their on-disk session formats diverge wildly. But a
few extraction primitives are genuinely cross-cutting — anything driven by
*what skills look like on disk* rather than by the specific transcript
schema — so they live here instead of being copy-pasted.

The big one is `extract_skill_from_path`. Claude Code is the only agent
with a first-class `Skill` tool primitive that records the slug it's
invoking (`Skill(skill='foo')`). In every other agent — Codex, pi.dev,
OpenCode — skills are just SKILL.md files on disk, and "invocation"
manifests as the model reading that file via the normal read/bash tool.
The deterministic, schema-free signal that a skill was activated is
therefore: a tool call whose path argument matches `…/skills/<slug>/SKILL.md`.

We extract the slug from that path here so every non-Claude-Code adapter
populates `tool_calls.skill_name` the same way the Claude Code adapter
does. Prune telemetry, dashboard sparklines, and the prune judge's
per-skill `usage_count` all key on that column, so this is the difference
between "watchmen sees skill use across all agents" and "watchmen only
sees skill use in Claude Code".
"""

from __future__ import annotations

import re

# Match `.../skills/<slug>/SKILL.md` anywhere in a path string. The slug
# must look like a real identifier (no slashes, no whitespace), which
# keeps `.../skills/SKILL.md` and `.../skills/sub/dir/SKILL.md` from
# producing false positives.
#
# Covers all the real-world locations skills live in:
#   ~/.claude/skills/<slug>/SKILL.md
#   ~/.codex/skills/<slug>/SKILL.md
#   ~/.codex/skills/.system/<slug>/SKILL.md   (Codex's "system" namespace)
#   ~/.pi/skills/<slug>/SKILL.md
#   ~/.watchmen/bundles/<project>/skills/<slug>/SKILL.md  (watchmen-managed)
#   <repo>/.claude/skills/<slug>/SKILL.md
_SKILL_PATH_RE = re.compile(r"[/\\]skills[/\\](?:\.system[/\\])?([A-Za-z0-9_.-]+)[/\\]SKILL\.md\b")


def extract_skill_from_path(value) -> str | None:
    """Return the skill slug if `value` references a SKILL.md file, else None.

    `value` is whatever the adapter has in hand at a tool-call site —
    typically the path string passed to a `read` / `bash` tool. We accept
    arbitrary types (None, dicts, lists, ints) and return None for any
    non-string input, so adapter sites can pass `block.get("path")` or
    `args.get("command")` without pre-validation.
    """
    if not isinstance(value, str) or not value:
        return None
    m = _SKILL_PATH_RE.search(value)
    return m.group(1) if m else None


def extract_skill_from_args(args) -> str | None:
    """Look through a tool-call's arguments object for a SKILL.md reference.

    Adapters call this with whatever shape the agent uses for tool args:
    a dict (Codex `function_call.arguments` after JSON-parse, pi
    `toolCall.arguments`), a bare string (a bash command line that may
    contain a path), a list, or None. We walk the structure shallowly
    and return the first slug we find.

    Why not deep-walk? Tool args in practice are flat: either a path
    string, a command string with the path as an argv token, or a small
    dict with one or two keys. A shallow walk catches every real case
    and won't accidentally pull a slug out of, say, a large `output`
    field on a tool result.
    """
    if isinstance(args, str):
        return extract_skill_from_path(args)
    if isinstance(args, dict):
        for v in args.values():
            slug = extract_skill_from_path(v) if isinstance(v, str) else None
            if slug:
                return slug
    if isinstance(args, list):
        for v in args:
            slug = extract_skill_from_path(v) if isinstance(v, str) else None
            if slug:
                return slug
    return None
