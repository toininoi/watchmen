"""Prune: LLM-judge pass over a project's curated skills to flag dead /
contradictory / low-value entries.

The cure for "auto-skill generation = slop." Watchmen's curator is
deliberately greedy when it bundles candidates — better to over-generate
and prune later than to under-cover. This module is the prune step.

Architecture
------------
The judge runs as an `Agent` (same multi-turn tool-dispatch loop as
the analyst / curator) with these tools available:

- `read_skill_full(slug)` — full SKILL.md body for one skill
- `read_transcript_excerpts(skill_name, limit)` — sessions where the
  skill actually fired, so the judge can verify whether usage matches
  the skill's stated trigger phrases
- `read_source_file(relative_path)` — read any file from the project's
  source repo, so the judge can check "does the workspace still match
  what this skill assumes?"
- `flag_skill(slug, severity, reason)` — push a candidate onto the
  prune queue (no destructive action; queue is reviewed in the viewer)
- `finish_review(summary)` — terminal tool

Inputs handed to the judge in the system prompt
-----------------------------------------------
- Workspace brief: bundled CLAUDE.md + AGENTS.md concatenated
- Skill catalog: per-skill {slug, description, when_to_use,
  when_not_to_use (truncated), trigger_phrases, usage_count,
  last_fired_at, created_at} — enough metadata to triage at a glance
- Source repo path (for the `read_source_file` tool)

Output
------
Writes `bundles/<project>/_prune_queue.json` with the structured queue;
the viewer renders it at `/p/<project>/prune` with approve / dismiss
controls. Approving deletes the skill file + commits; dismissing
suppresses re-flagging on the next prune run (`_prune_dismissed.json`).

Run mode
--------
The default prompt is "aggressive" — the judge is instructed to flag
anything that looks low-value, not just outright contradictions. This
matches the design call we made at v0.6.1 spec time: lots of flags ↔
human approval gate, rather than few-but-confident auto-deletes.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from watchmen import config, paths, state
from watchmen.tools_lib import make_tools
from watchmen.cache import ReadRecorder, wrap_handlers


# ─── Inputs ────────────────────────────────────────────────────────────────


@dataclass
class SkillEntry:
    """Lightweight projection of a SKILL.md + its usage counts.

    `last_fired_at` is None when the skill has never been invoked.
    `created_at` falls back to the SKILL.md mtime when git history isn't
    available — good enough for triage."""

    slug: str
    skill_dir: Path
    name: str
    description: str
    when_to_use: str
    when_not_to_use: str
    trigger_phrases: list[str]
    usage_count: int
    last_fired_at: str | None
    created_at: str


@dataclass
class JudgeInputs:
    project_key: str
    source_repo: str
    bundle_dir: Path
    workspace_brief: str
    skills: list[SkillEntry]


def _parse_frontmatter(text: str) -> dict[str, Any]:
    """Parse the YAML-ish frontmatter at the top of a SKILL.md.

    SKILL.md frontmatter is hand-edited by the curator's LLM output, so
    it isn't guaranteed to be strictly valid YAML — particularly around
    list values. We parse defensively: known keys are extracted with
    regex, unknown keys ignored. Returns a dict with `name`, `description`,
    `when_to_use`, `when_not_to_use`, `trigger_phrases` (best effort).
    """
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    block = text[3:end]
    out: dict[str, Any] = {}
    # Simple key: value scanner. Multi-line list items (- foo) collect
    # under the most recent key that ended with no value.
    current_key: str | None = None
    current_list: list[str] = []
    for raw in block.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$", line)
        if m:
            # Close out the previous list if there was one
            if current_key and current_list:
                out[current_key] = current_list
                current_list = []
            key, val = m.group(1), m.group(2)
            if val:
                # Inline list `[a, b, c]` or scalar
                if val.startswith("[") and val.endswith("]"):
                    inner = val[1:-1]
                    out[key] = [s.strip().strip("'").strip('"') for s in inner.split(",") if s.strip()]
                    current_key = None
                else:
                    out[key] = val.strip().strip("'").strip('"')
                    current_key = None
            else:
                current_key = key
                current_list = []
            continue
        # List item under current_key
        list_match = re.match(r"^\s*-\s+(.*)$", line)
        if list_match and current_key:
            current_list.append(list_match.group(1).strip())
    if current_key and current_list:
        out[current_key] = current_list
    return out


def _usage_for_bundle(project_key: str) -> dict[str, tuple[int, str | None]]:
    """Read per-skill usage counts from corpus.db.

    Maps skill_name → (count, last_fired_iso_or_None). Claude Code logs
    skill activations as a `tool_use` with `name='Skill'` and
    `input.skill='<slug>'`; the corpus extractor stores that slug in
    `tool_calls.skill_name`.

    We don't filter by project here — skills are global across a user's
    Claude Code install, so a single skill's usage may come from sessions
    outside this project's directory. That's the right signal for prune:
    if the skill never fires *anywhere*, it's dead even if it lives in
    this project's bundle.
    """
    db_path = paths.WATCHMEN_HOME / "corpus.db"
    if not db_path.exists():
        return {}
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT skill_name, COUNT(*), MAX(timestamp) "
            "FROM tool_calls "
            "WHERE skill_name IS NOT NULL "
            "GROUP BY skill_name"
        ).fetchall()
    except sqlite3.OperationalError:
        # Pre-v0.6.1 corpus.db without the skill_name column — treat as
        # no signal rather than blowing up. Caller's prompt will note it.
        return {}
    finally:
        conn.close()
    return {name: (int(count), last) for name, count, last in rows}


def gather_judge_inputs(project_key: str, *, source_repo: str | None = None) -> JudgeInputs:
    """Build the per-skill catalog + workspace brief the judge needs to
    work. Reads the project's curated bundle directory + corpus.db.

    Raises FileNotFoundError if the bundle hasn't been generated yet
    (the user needs to run `watchmen curate` first)."""
    if source_repo is None:
        proj = state.get_project(project_key)
        if not proj:
            raise RuntimeError(f"project not tracked: {project_key}")
        source_repo = proj["source_repo"]
    bundle_dir = paths.BUNDLES_DIR / project_key
    skills_dir = bundle_dir / "skills"
    if not skills_dir.exists():
        raise FileNotFoundError(
            f"no skills directory at {skills_dir} — run `watchmen curate` first"
        )

    usage = _usage_for_bundle(project_key)

    # Workspace brief: CLAUDE.md + AGENTS.md from the bundle root, joined
    # with a clear separator. AGENTS.md is optional (Claude Code default
    # is CLAUDE.md only; some users keep both for Claude vs Codex split).
    brief_parts: list[str] = []
    for fname in ("CLAUDE.md", "AGENTS.md"):
        p = bundle_dir / fname
        if p.exists():
            brief_parts.append(f"## {fname}\n\n{p.read_text(encoding='utf-8')}")
    workspace_brief = "\n\n---\n\n".join(brief_parts) or "(no workspace brief found)"

    skills: list[SkillEntry] = []
    for skill_dir in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue
        body = skill_md.read_text(encoding="utf-8")
        fm = _parse_frontmatter(body)
        slug = skill_dir.name
        when_to_use = fm.get("when_to_use") or fm.get("trigger_phrases") or ""
        if isinstance(when_to_use, list):
            when_to_use = "\n".join(f"- {s}" for s in when_to_use)
        when_not_to_use = fm.get("when_not_to_use") or ""
        if isinstance(when_not_to_use, list):
            when_not_to_use = "\n".join(f"- {s}" for s in when_not_to_use)
        triggers = fm.get("trigger_phrases") or []
        if isinstance(triggers, str):
            triggers = [triggers]
        count, last = usage.get(slug, (0, None))
        skills.append(SkillEntry(
            slug=slug,
            skill_dir=skill_dir,
            name=str(fm.get("name") or slug),
            description=str(fm.get("description") or "").strip(),
            when_to_use=str(when_to_use).strip(),
            when_not_to_use=str(when_not_to_use).strip(),
            trigger_phrases=list(triggers),
            usage_count=count,
            last_fired_at=last,
            created_at=_fmt_mtime(skill_md),
        ))

    return JudgeInputs(
        project_key=project_key,
        source_repo=source_repo,
        bundle_dir=bundle_dir,
        workspace_brief=workspace_brief,
        skills=skills,
    )


def _fmt_mtime(path: Path) -> str:
    """ISO-format mtime; falls back to '?' on stat failure."""
    try:
        from datetime import datetime, timezone as _tz
        return datetime.fromtimestamp(path.stat().st_mtime, tz=_tz.utc).date().isoformat()
    except OSError:
        return "?"


# ─── Output queue ──────────────────────────────────────────────────────────


@dataclass
class FlaggedSkill:
    slug: str
    severity: str  # "low" | "medium" | "high"
    reason: str


class PruneQueue:
    """Accumulator the judge writes to via `flag_skill`. Closed out by
    `finish_review`, which dumps to JSON on disk."""

    def __init__(self, project_key: str, bundle_dir: Path) -> None:
        self.project_key = project_key
        self.bundle_dir = bundle_dir
        self.flagged: list[FlaggedSkill] = []
        # Track previously-dismissed slugs so the judge can de-prioritize
        # them (they re-render in the queue but flagged as "previously
        # kept" — explicit, not silent suppression).
        dismissed_path = bundle_dir / "_prune_dismissed.json"
        try:
            self.dismissed: set[str] = set(json.loads(dismissed_path.read_text()))
        except (FileNotFoundError, json.JSONDecodeError):
            self.dismissed = set()

    def add(self, slug: str, severity: str, reason: str) -> None:
        severity = severity.lower().strip()
        if severity not in ("low", "medium", "high"):
            severity = "medium"
        self.flagged.append(FlaggedSkill(slug=slug, severity=severity, reason=reason.strip()))

    def write(self, *, summary: str = "") -> Path:
        queue_path = self.bundle_dir / "_prune_queue.json"
        payload = {
            "project_key": self.project_key,
            "summary": summary,
            "previously_dismissed": sorted(self.dismissed),
            "flagged": [
                {"slug": f.slug, "severity": f.severity, "reason": f.reason}
                for f in self.flagged
            ],
        }
        queue_path.write_text(json.dumps(payload, indent=2))
        return queue_path


# ─── Tool surface ──────────────────────────────────────────────────────────


def _build_prune_tools(
    inputs: JudgeInputs, queue: PruneQueue, *, transcripts_per_skill: int = 3
) -> tuple[list[dict], dict]:
    """Tool specs + handlers for the judge agent.

    Composes the project-scoped read tools from `tools_lib.make_tools`
    (so the judge can read source files just like the curator can) and
    adds the prune-specific tools on top."""
    base_specs, base_handlers = make_tools(
        source_repo=inputs.source_repo, project_key=inputs.project_key
    )
    # We only want the read-side tools from the base set — the judge
    # never writes or commits. Filter by name.
    keep = {"query_corpus", "read_session_full", "list_repo_files", "read_repo_file"}
    specs = [s for s in base_specs if s.get("function", {}).get("name") in keep]
    handlers = {k: v for k, v in base_handlers.items() if k in keep}

    specs.append({"type": "function", "function": {
        "name": "read_skill_full",
        "description": (
            "Read the full SKILL.md body for one skill in this project. "
            "Use when the catalog description alone isn't enough to judge."
        ),
        "parameters": {"type": "object", "properties": {
            "slug": {"type": "string", "description": "The skill slug (directory name)"},
        }, "required": ["slug"]},
    }})

    specs.append({"type": "function", "function": {
        "name": "read_transcript_excerpts",
        "description": (
            "Return up to N session windows where this skill actually fired, "
            "so you can verify whether the firing matched the skill's stated "
            "trigger phrases. Returns an empty list if the skill has never "
            "been invoked."
        ),
        "parameters": {"type": "object", "properties": {
            "skill_name": {"type": "string"},
            "limit": {"type": "integer", "default": transcripts_per_skill},
        }, "required": ["skill_name"]},
    }})

    specs.append({"type": "function", "function": {
        "name": "flag_skill",
        "description": (
            "Push a skill onto the prune queue for human review. severity in "
            "{low, medium, high}: low for narrow/obvious skills, medium for "
            "redundancy with siblings, high for outright contradictions or "
            "dead skills that have never fired despite obvious trigger "
            "matches. Reason should be one specific sentence — generic "
            "reasons will be hard for the user to act on."
        ),
        "parameters": {"type": "object", "properties": {
            "slug": {"type": "string"},
            "severity": {"type": "string", "enum": ["low", "medium", "high"]},
            "reason": {"type": "string"},
        }, "required": ["slug", "severity", "reason"]},
    }})

    specs.append({"type": "function", "function": {
        "name": "finish_review",
        "description": "Terminal — finalize the prune queue and exit.",
        "parameters": {"type": "object", "properties": {
            "summary": {"type": "string", "description": "1-2 sentence summary of what was flagged and why."},
        }, "required": ["summary"]},
    }})

    skill_map = {s.slug: s for s in inputs.skills}

    def _read_skill_full(slug: str) -> str:
        s = skill_map.get(slug)
        if not s:
            return f"ERROR: no skill named {slug!r} in this project"
        return (s.skill_dir / "SKILL.md").read_text(encoding="utf-8")

    def _read_transcript_excerpts(skill_name: str, limit: int = transcripts_per_skill) -> str:
        db_path = paths.WATCHMEN_HOME / "corpus.db"
        if not db_path.exists():
            return "ERROR: corpus.db not found — run `watchmen ingest` first"
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT tc.session_id, tc.timestamp, s.transcript_path "
                "FROM tool_calls tc JOIN sessions s USING (session_id) "
                "WHERE tc.skill_name = ? "
                "ORDER BY tc.timestamp DESC LIMIT ?",
                (skill_name, int(limit) or transcripts_per_skill),
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            return f"(skill {skill_name!r} has never fired in any session)"
        excerpts = []
        for r in rows:
            excerpts.append(
                f"### {r['session_id']} @ {r['timestamp']}\n"
                f"transcript: {r['transcript_path']}\n"
                + _excerpt_around_skill(r["transcript_path"], skill_name)
            )
        return "\n\n".join(excerpts)

    def _flag_skill(slug: str, severity: str, reason: str) -> str:
        if slug not in skill_map:
            return f"ERROR: no skill named {slug!r}; valid: {sorted(skill_map.keys())[:5]}…"
        queue.add(slug, severity, reason)
        note = " (previously dismissed)" if slug in queue.dismissed else ""
        return f"flagged {slug} [{severity}]{note}: {reason[:80]}"

    # `finish_review` is the terminal tool — `Agent` doesn't invoke the
    # handler for terminal tools (it just records "ok" and exits the
    # loop), so the queue write lives in the orchestrator instead. The
    # handler is still registered defensively for symmetry with the
    # specs, in case the dispatch contract ever changes.
    def _finish_review(summary: str) -> str:
        return f"finishing review with {len(queue.flagged)} flagged skill(s)"

    handlers.update({
        "read_skill_full":           _read_skill_full,
        "read_transcript_excerpts":  _read_transcript_excerpts,
        "flag_skill":                _flag_skill,
        "finish_review":             _finish_review,
    })

    return specs, handlers


def _excerpt_around_skill(transcript_path: str, skill_name: str, *, window: int = 800) -> str:
    """Pull a ~800-char snippet of the transcript centered on the first
    occurrence of the skill's name. Falls back to head of file on
    missing/unreadable transcripts so the judge still has *something*."""
    try:
        text = Path(transcript_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "(transcript file unreadable)"
    needle = f'"skill": "{skill_name}"'
    idx = text.find(needle)
    if idx < 0:
        return text[:window]
    start = max(0, idx - window // 2)
    end = min(len(text), idx + window // 2)
    return text[start:end]


# ─── Agent assembly ────────────────────────────────────────────────────────


_SYSTEM_PROMPT_TEMPLATE = """\
You are watchmen's prune judge. Your job is to flag low-value, dead, or
contradictory skills in this project so the user can decide whether to
delete them.

**Run mode: aggressive.** Flag anything that looks low-value, not just
outright duplicates. False positives are fine — the user reviews every
flag in a UI. Missed slop is the bigger failure mode.

# Project workspace brief

The user's own claude.md / agents.md for this project. Skills should
*complement* this brief, not duplicate it. If a skill teaches something
already in the brief, that's grounds to flag.

{workspace_brief}

# Skill catalog

{skill_catalog}

# Things to flag (aggressive mode)

- **Never-fired skills** (usage_count = 0) where the trigger phrases
  look like they should have matched at least one session. Use
  `read_transcript_excerpts` on a sibling skill to gauge what real
  sessions look like, then judge.
- **Contradictions** with sibling skills — Skill A says "always X",
  Skill B says "always Y" for the same situation. Flag both.
- **Redundancy** — multiple skills cover the same trigger phrase set.
  Keep the most specific one; flag the others.
- **Workspace-brief duplicates** — the CLAUDE.md / AGENTS.md already
  covers this; the skill adds no value.
- **Drifted skills** — references files / commands / patterns that don't
  exist in the current source repo. Use `read_repo_file` /
  `list_repo_files` to verify.
- **Narrow one-shots** — useful exactly once for one user prompt;
  doesn't generalize. Flag with severity=low.
- **Vague skills** — description / when_to_use are so abstract the LLM
  router can't tell when to fire. Flag with severity=medium.

# Severity

- `high`: contradictions, drifted skills, dead skills with obvious
  triggers, anything that would actively confuse the agent
- `medium`: redundancy, vagueness, workspace-brief duplicates
- `low`: narrow one-shots, "fine but rarely useful"

# Process

1. Skim the catalog. Note which skills look suspicious.
2. For each suspicious skill, call `read_skill_full` to see the body,
   `read_transcript_excerpts` to see real usage (or non-usage), and
   `read_repo_file` if you need to verify a claim about the codebase.
3. Call `flag_skill` for each flagged skill with a specific reason —
   the user reads these in the review UI, so be concrete.
4. Call `finish_review` with a 1-2 sentence summary when done.

Previously dismissed slugs (re-flag only if you have new evidence):
{previously_dismissed}
"""


def _render_catalog(skills: list[SkillEntry]) -> str:
    if not skills:
        return "(no skills bundled yet)"
    lines = []
    for s in skills:
        triggers = ", ".join(s.trigger_phrases[:5]) or "(none)"
        last = s.last_fired_at or "never"
        lines.append(
            f"- **{s.slug}** ({s.usage_count} uses, last={last}, created={s.created_at})\n"
            f"   description: {s.description[:200]}\n"
            f"   when_to_use: {s.when_to_use[:200] or '(empty)'}\n"
            f"   triggers: {triggers}"
        )
    return "\n".join(lines)


def build_judge_agent(
    client,
    model: str,
    inputs: JudgeInputs,
    queue: PruneQueue,
    *,
    log_path: Path | None = None,
    recorder: ReadRecorder | None = None,
):
    """Construct the prune-judge `Agent` ready to run."""
    from watchmen.agent import Agent

    specs, handlers = _build_prune_tools(inputs, queue)
    if recorder is not None:
        handlers = wrap_handlers(handlers, recorder)

    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(
        workspace_brief=inputs.workspace_brief,
        skill_catalog=_render_catalog(inputs.skills),
        previously_dismissed=", ".join(sorted(queue.dismissed)) or "(none)",
    )

    return Agent(
        name=f"prune-judge[{inputs.project_key}]",
        model=model,
        system_prompt=system_prompt,
        tool_specs=specs,
        tool_handlers=handlers,
        terminal_tool="finish_review",
        client=client,
        log_path=log_path,
    )


# ─── Orchestrator ──────────────────────────────────────────────────────────


def run_prune(project_key: str, *, model: str | None = None) -> Path:
    """End-to-end: gather inputs → run judge → write queue. Returns the
    path to the written `_prune_queue.json`. Raises on missing bundle."""
    import httpx

    inputs = gather_judge_inputs(project_key)
    queue = PruneQueue(project_key, inputs.bundle_dir)

    model = model or config.default_model()
    log_path = inputs.bundle_dir / "_prune_log.txt"

    with httpx.Client(timeout=300.0) as client:
        judge = build_judge_agent(
            client, model, inputs, queue, log_path=log_path,
        )
        # The terminal `finish_review` tool carries the LLM's summary —
        # `Agent.run` returns its args dict, so we lift `summary` from
        # there to attach to the persisted queue.
        terminal_args, _ = judge.run(
            "Review every skill in this project and flag the low-value ones."
        )

    summary = ""
    if isinstance(terminal_args, dict):
        summary = str(terminal_args.get("summary") or "")
    return queue.write(summary=summary)
