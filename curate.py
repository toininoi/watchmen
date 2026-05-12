"""4-stage skill + CLAUDE.md curator with critic sub-agent.

Stages:
  1. candidate-finder  : reads thesis + scans source repo, outputs filtered list of procedural
                         skill candidates that have actual code behind them.
  2. per-skill curator : one multi-turn agent per candidate. Investigates → drafts SKILL.md +
                         scripts → spawns critic → refines → commits.
  3. claude.md author  : reads finalized skills + thesis sections, drafts CLAUDE.md, runs through
                         critic, refines.
  4. index writer      : writes _index.md summarizing what was generated and why.

Output: kai_claude/<project_key>/ with skills/<name>/, CLAUDE.md, _curation_log.md, _index.md.

Usage:
  uv run curate.py --project tally-weijl-images --repo ~/Development/personal/tally-weijl-images
"""

import argparse
import json
import re
import shutil
import sqlite3
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from textwrap import dedent

import httpx

from agent import Agent, load_api_key
from cache import ReadRecorder, cache_hit, invalidate_all, wrap_handlers, write_cache
from tools_lib import make_tools

ROOT = Path(__file__).parent
DEFAULT_MODEL = "deepseek/deepseek-v4-flash"


# ─── Prompts ────────────────────────────────────────────────────────────────

CANDIDATE_FINDER_PROMPT = dedent("""
    You are identifying real, packageable skill candidates for a Claude Code workspace.

    A real skill candidate must satisfy ALL of these:
      - It is a recurring procedure (the user does it more than once across sessions).
      - It has actual artifacts (Python scripts, bash commands, structured prompts, file templates) that
        already exist in the source repo or in session transcripts.
      - It can be triggered by a specific kind of user request — you can describe the trigger.
      - Generalizing it (turning hardcoded paths/keys into args) preserves its usefulness.

    Reject candidates that are:
      - Behavioral observations only ("task-direct", "low-ceremony communication", "thanks mate") —
        these are about the user, not skills.
      - One-time exploratory work that didn't repeat.
      - Stuff that's already a one-line bash command with no procedure around it.
      - About OTHER repos, personal side projects, or non-coding workflows (e.g. investment
        analysis, stock trading, customer projects rooted in other directories), even if the
        thesis mentions them — they would not belong checked into THIS repo's skills/ directory.
        The test: would the artifacts live naturally inside the source repo you're scanning?

    Process:
      1. Read the thesis sections "Skill candidates" and "Workflow archetypes" via read_thesis_section.
      2. For each plausible candidate, use list_repo_files + read_repo_file to verify the artifacts exist.
      3. Pick session_ids that demonstrate the trigger pattern via query_corpus or by reading the thesis's
         "Notable sessions" section.
      4. When you have your filtered list, call finish_candidates with a JSON-like structured output.

    Be ruthless. Better to ship 4 strong candidates than 12 weak ones.
""").strip()


SKILL_CURATOR_PROMPT_TEMPLATE = dedent("""
    You are authoring a Claude Code skill bundle for: {skill_name}

    Description: {skill_description}
    Trigger / when-to-use: {when_to_use}
    Source repo files (hints): {source_files}
    Reference session_ids: {session_ids}

    Your job: produce a complete, runnable skill bundle under skills/{skill_slug}/ containing:
      - SKILL.md  — frontmatter (name, description, when-to-use), then instructions for the future agent
      - scripts/  — actual Python/bash files. Extract real code from the source repo (read_repo_file),
                    generalize hardcoded values into args. Don't reinvent.
      - references/ — optional. Extracted notes (API quirks, schemas) that the skill will load on demand.

    SKILL.md frontmatter format:
      ---
      name: {skill_slug}
      description: <1-line activation hint — what task type this skill serves>
      when_to_use: <bullets describing the user-prompt patterns that should trigger this>
      when_not_to_use: <bullets describing prompt patterns that LOOK similar but should NOT trigger this —
                       adjacent task shapes, superficially-related vocabulary, narrower/broader variants
                       that have their own skill or no skill at all>
      ---

    Then in the body:
      ## Procedure
      <numbered steps>
      ## When NOT to use
      <expanded form of the frontmatter field: concrete counter-examples of prompts that should be rejected,
      with a 1-line reason each. Cite session_ids where a similar-looking prompt went a different direction.
      This is the single biggest lever against false activation — be specific.>
      ## Inputs
      <args, env vars, files expected>
      ## Outputs
      <where things land>
      ## Examples
      <one or two real examples extracted from sessions, with the session_id cited>

    Process:
      1. Investigate via read_thesis_section, read_session_full, list_repo_files, read_repo_file.
         Focus on EXTRACTING real code, not reinventing.
      2. Draft the bundle — write SKILL.md and at least one script via write_kai_claude_file.
         Use paths like 'skills/{skill_slug}/SKILL.md', 'skills/{skill_slug}/scripts/<name>.py'.
      3. When draft is in place, call run_critic with skill_dir='skills/{skill_slug}' and a representative
         sample_task. The critic will report missing/ambiguous/hardcoded/broken issues.
      4. Refine based on critic feedback. Re-read with read_kai_claude_file, rewrite as needed.
      5. Loop until the critic says it's clean OR you've done 2 critic rounds.
      6. append_curation_log with a brief summary of decisions + critic findings.
      7. Call finish_skill with the slug and a short summary.

    The skill must be REUSABLE — generalize, don't hardcode the user's specific paths or keys.
""").strip()


CRITIC_PROMPT = dedent("""
    You are a critic evaluating a Claude Code skill bundle. The bundle lives at: {skill_dir}

    Sample task to consider: {sample_task}

    Your job: Read every file in the skill bundle. Then evaluate as if you were a future agent receiving
    that sample task. Could you execute the skill end-to-end? Where would you fail?

    Specifically check:
      - Trigger description: would a future agent know when to use this from a user prompt?
      - Anti-triggers: is `when_not_to_use` present and specific? Would a borderline prompt
        (adjacent task shape, similar vocabulary) be correctly REJECTED based on what's written?
        Generic exclusions ("not for unrelated tasks") don't count — flag them as ambiguous.
      - Procedure steps: are they unambiguous? Any "do the thing" hand-waving?
      - Scripts: hardcoded paths, API keys, or values that should be args/env vars?
      - Imports / dependencies: are they declared somewhere?
      - Inputs/Outputs: clear about what the skill consumes and where it writes?
      - References: any file referenced from SKILL.md that doesn't exist?

    Process:
      1. list_kai_claude_files(subdir=skill_dir) to enumerate.
      2. read_kai_claude_file each one.
      3. Optionally query_corpus or read_session_full for context if a claim looks suspicious.
      4. Call finish_critique with structured findings: list of {{type, location, issue, suggestion}}.
         type ∈ "missing" | "ambiguous" | "hardcoded" | "broken" | "ok-overall".

    Be specific. "scripts/submit_batch.py:12 — FAL_KEY is hardcoded, should read from env" beats
    "scripts have hardcoded values".
""").strip()


CLAUDE_MD_PROMPT = dedent("""
    You are authoring the workspace-level CLAUDE.md for project: {project_key}

    Output to: kai_claude/{project_key}/CLAUDE.md

    A good CLAUDE.md is a STANDING BRIEF for any future Claude Code session opened in this repo. It
    answers EVERY question a fresh agent might have on day 1: what this project is, how it's structured,
    how to build/test/run it, what conventions exist, what skills are available, what should I know
    before doing anything, what landmines exist, how the user communicates.

    Your job: capture as much actionable, observable signal as possible — combining BEHAVIORAL signal
    from the thesis with STRUCTURAL signal from the source repo (code organization, build tooling,
    env contracts, conventions).

    Use ALL of these sections. Skip a section ONLY if there is genuinely no signal for it.

      # {project_key} — workspace context

      ## What this project is
      <2-3 sentences: what it does, who it's for, key tech stack (frameworks, languages, infra)>

      ## Code structure
      <key directories and what lives where; conventions like "lib/api/ is generated, never hand-edit";
      derive from list_repo_files + read_repo_file on package.json / pyproject.toml / Cargo.toml / etc.>

      ## Development environment
      <env vars required, dev server quirks, ports/proxies, key dependencies, OS-specific gotchas>

      ## Common commands
      <build / lint / test / dev / deploy — the actual npm / cargo / uv / pnpm scripts the user runs;
      derive from package.json scripts or thesis "Notable sessions">

      ## Coding patterns & conventions
      <state management lib, data fetching, file naming, component patterns, type policies, formatting;
      derive from repo files + session evidence>

      ## Test strategy
      <what tests exist or don't; framework; where they live; what the user does for verification
      (e.g. "no automated tests yet — minimum check is npm run lint")>

      ## Active workflows
      <bulleted list — reference the SKILLS that handle each. e.g. "- Bulk image generation → see skills/fal-bulk-generation/">

      ## Available skills
      <table: skill name | location | activation hint phrases>

      ## Recurring patterns
      <workflow archetypes from thesis NOT covered by a discrete skill — cost-conscious sampling,
      audit-before-expensive, drop-path-and-go, etc.>

      ## Coding rules (do's and don'ts)
      <derived from session corrections: where the user said "no, don't do that" or "always do X".
      Cite session_id when possible. Examples: "Don't describe wash/color in prompts that have a
      product reference", "Don't run npm run build for verification">

      ## Known landmines
      <pitfalls observed across sessions: error patterns, content-policy issues, env-override priority
      bugs, sub-agent auth failures, etc. Each bullet should be a *specific* trap with *how to avoid* it>

      ## Debugging playbook
      <when something breaks, what does the user actually do? Each entry: symptom → diagnostic procedure
      (the specific logs, queries, or commands the user reached for) → likely root causes, cited by
      session_id. This is the *recovery* counterpart to landmines: landmines say "don't step here",
      this section says "if you did step there, here's how to find out and fix it". Mine sessions with
      high tool_error_count or visible frustration for the actual diagnostic moves used.>

      ## Communication style notes
      <how the user talks to the agent — tone, expectations, what to mirror; e.g. enthusiasm welcome,
      task-direct compressed handoffs, fast self-correction without friction>

    Process:
      1. read_thesis_section for: 'Workflow archetypes', 'Notable sessions', 'Communication style',
         'Drift', 'Frustration / pushback patterns', 'Skill candidates'. The 'Frustration / pushback
         patterns' section is especially important for the Debugging playbook — it points to sessions
         where the user hit errors and recovered. Use query_corpus with filters like
         `SELECT session_id, tool_error_count FROM sessions WHERE tool_error_count > 3 ORDER BY
         tool_error_count DESC LIMIT 10` to find high-friction sessions, then read_session_full to
         extract the actual diagnostic moves (which commands/queries the user ran to localize the bug).
      2. list_repo_files (broad pattern like '*' or '**/*') to map the repo's structure.
      3. read_repo_file on key infrastructure files: package.json, pyproject.toml, Cargo.toml,
         README.md, .env.example, tsconfig.json — whichever apply.
      4. list_kai_claude_files(subdir='skills') and read each SKILL.md frontmatter.
      5. Draft and write CLAUDE.md via write_kai_claude_file.
      6. run_critic with skill_dir='' (review whole CLAUDE.md), sample_task='a fresh agent opens this repo'.
      7. Refine if critic flags issues.
      8. append_curation_log + call finish_claude_md.

    Prefer concrete observation over generic prose. "uses Zustand for auth state, React Query for
    data fetching" beats "uses modern state management". Cite session_ids for behavioral claims.
""").strip()


# ─── Critic spawner ─────────────────────────────────────────────────────────

def make_critic_runner(client: httpx.Client, model: str, project_key: str, log_path: Path):
    """Returns a callable run_critic(skill_dir, sample_task) → critique JSON string."""
    specs, handlers = make_tools(source_repo="/", project_key=project_key)
    # Critic gets a read-only subset
    read_only = ["query_corpus", "read_session_full", "read_thesis_section",
                 "list_kai_claude_files", "read_kai_claude_file"]
    crit_specs = [s for s in specs if s["function"]["name"] in read_only]
    crit_handlers = {k: handlers[k] for k in read_only}

    crit_specs.append({"type": "function", "function": {
        "name": "finish_critique",
        "description": "Submit your structured critique. findings is a list of {type, location, issue, suggestion}.",
        "parameters": {"type": "object", "properties": {
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string"},
                        "location": {"type": "string"},
                        "issue": {"type": "string"},
                        "suggestion": {"type": "string"},
                    },
                    "required": ["type", "issue"],
                },
            },
            "overall": {"type": "string", "description": "1-line overall verdict: 'clean' | 'needs work' | 'broken'"},
        }, "required": ["findings", "overall"]},
    }})

    def run_critic(skill_dir: str, sample_task: str) -> str:
        critic = Agent(
            name=f"critic[{skill_dir or 'CLAUDE.md'}]",
            model=model,
            system_prompt=CRITIC_PROMPT.format(skill_dir=skill_dir or "(root: CLAUDE.md)", sample_task=sample_task),
            tool_specs=crit_specs,
            tool_handlers=crit_handlers,
            terminal_tool="finish_critique",
            client=client,
            log_path=log_path,
        )
        result, _ = critic.run(
            f"Evaluate the bundle at '{skill_dir or 'kai_claude root'}' against this sample task: {sample_task}",
            max_iter=15,
        )
        return json.dumps(result, indent=2) if result else "(critic produced no findings)"

    return run_critic


# ─── Stage builders ─────────────────────────────────────────────────────────

def build_finder_agent(client, model, project_key, source_repo, log_path, recorder: ReadRecorder | None = None):
    specs, handlers = make_tools(source_repo=source_repo, project_key=project_key)
    keep = ["query_corpus", "read_thesis_section", "list_repo_files", "read_repo_file"]
    finder_specs = [s for s in specs if s["function"]["name"] in keep]
    finder_handlers = {k: handlers[k] for k in keep}
    if recorder is not None:
        finder_handlers = wrap_handlers(finder_handlers, recorder)
    finder_specs.append({"type": "function", "function": {
        "name": "finish_candidates",
        "description": "Submit the filtered list of skill candidates.",
        "parameters": {"type": "object", "properties": {
            "candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "slug": {"type": "string", "description": "kebab-case directory name"},
                        "description": {"type": "string"},
                        "when_to_use": {"type": "string"},
                        "source_files": {"type": "array", "items": {"type": "string"}},
                        "session_ids": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["name", "slug", "description", "when_to_use"],
                },
            },
        }, "required": ["candidates"]},
    }})
    return Agent(
        name="finder",
        model=model,
        system_prompt=CANDIDATE_FINDER_PROMPT,
        tool_specs=finder_specs,
        tool_handlers=finder_handlers,
        terminal_tool="finish_candidates",
        client=client,
        log_path=log_path,
    )


def build_skill_curator(client, model, project_key, source_repo, candidate, log_path, run_critic, recorder: ReadRecorder | None = None):
    specs, handlers = make_tools(source_repo=source_repo, project_key=project_key)
    slug = candidate["slug"]
    expected_prefix = f"skills/{slug}/"
    raw_writer = handlers["write_kai_claude_file"]

    def write_skill_scoped(file_path: str, content: str) -> str:
        # If agent typed the slug as a prefix (the original path bug), strip it
        if file_path.startswith(slug + "/"):
            file_path = file_path[len(slug) + 1 :]
        if file_path.startswith(expected_prefix):
            return raw_writer(file_path=file_path, content=content)
        # Block writes to another skill or absolute paths
        if file_path.startswith("skills/") or file_path.startswith("/") or ".." in file_path:
            return (
                f"ERROR: this skill curator can only write under '{expected_prefix}'. "
                f"You tried '{file_path}'. Use relative paths like 'SKILL.md', 'scripts/foo.py'."
            )
        # Auto-scope relative paths under skills/<slug>/
        clean = file_path.lstrip("./")
        full = expected_prefix + clean
        result = raw_writer(file_path=full, content=content)
        return (
            f"NOTE: path '{file_path}' auto-scoped to '{full}'. Always prefix paths with "
            f"'{expected_prefix}' explicitly. {result}"
        )

    # Replace the write tool spec + handler with the scoped version
    specs = [s for s in specs if s["function"]["name"] != "write_kai_claude_file"]
    specs.append({"type": "function", "function": {
        "name": "write_kai_claude_file",
        "description": (
            f"Write a file UNDER skills/{slug}/ (this skill's bundle). Use relative paths: "
            f"'SKILL.md', 'scripts/<name>.py', 'references/<name>.md', 'requirements.txt', "
            f"'.env.example'. Paths are auto-scoped under skills/{slug}/. Cannot write outside this scope."
        ),
        "parameters": {"type": "object", "properties": {
            "file_path": {"type": "string", "description": f"Relative path under skills/{slug}/"},
            "content": {"type": "string"},
        }, "required": ["file_path", "content"]},
    }})
    handlers = dict(handlers)
    handlers["write_kai_claude_file"] = write_skill_scoped

    specs.append({"type": "function", "function": {
        "name": "run_critic",
        "description": "Spawn the critic sub-agent to evaluate the current state of a skill bundle. Returns structured JSON findings.",
        "parameters": {"type": "object", "properties": {
            "skill_dir": {"type": "string", "description": f"always 'skills/{slug}'"},
            "sample_task": {"type": "string", "description": "a representative user prompt that should trigger this skill"},
        }, "required": ["skill_dir", "sample_task"]},
    }})
    specs.append({"type": "function", "function": {
        "name": "finish_skill",
        "description": "Submit the finalized skill. Pass slug + a short summary of decisions.",
        "parameters": {"type": "object", "properties": {
            "slug": {"type": "string"},
            "summary": {"type": "string"},
        }, "required": ["slug", "summary"]},
    }})

    handlers["run_critic"] = run_critic

    if recorder is not None:
        handlers = wrap_handlers(handlers, recorder)

    sys_prompt = SKILL_CURATOR_PROMPT_TEMPLATE.format(
        skill_name=candidate["name"],
        skill_description=candidate["description"],
        when_to_use=candidate.get("when_to_use", ""),
        source_files=", ".join(candidate.get("source_files", [])) or "(use list_repo_files to find)",
        session_ids=", ".join(candidate.get("session_ids", [])) or "(use query_corpus + thesis to find)",
        skill_slug=candidate["slug"],
    )

    return Agent(
        name=f"curator[{candidate['slug']}]",
        model=model,
        system_prompt=sys_prompt,
        tool_specs=specs,
        tool_handlers=handlers,
        terminal_tool="finish_skill",
        client=client,
        log_path=log_path,
    )


def build_claude_md_author(client, model, project_key, source_repo, log_path, run_critic, recorder: ReadRecorder | None = None):
    specs, handlers = make_tools(source_repo=source_repo, project_key=project_key)
    specs = list(specs)
    specs.append({"type": "function", "function": {
        "name": "run_critic",
        "description": "Spawn the critic sub-agent to evaluate the current state.",
        "parameters": {"type": "object", "properties": {
            "skill_dir": {"type": "string"},
            "sample_task": {"type": "string"},
        }, "required": ["skill_dir", "sample_task"]},
    }})
    specs.append({"type": "function", "function": {
        "name": "finish_claude_md",
        "description": "Submit the finalized CLAUDE.md.",
        "parameters": {"type": "object", "properties": {
            "summary": {"type": "string"},
        }, "required": ["summary"]},
    }})
    handlers = dict(handlers)
    handlers["run_critic"] = run_critic

    if recorder is not None:
        handlers = wrap_handlers(handlers, recorder)

    return Agent(
        name="claude_md_author",
        model=model,
        system_prompt=CLAUDE_MD_PROMPT.format(project_key=project_key),
        tool_specs=specs,
        tool_handlers=handlers,
        terminal_tool="finish_claude_md",
        client=client,
        log_path=log_path,
    )


# ─── Changelog generation ───────────────────────────────────────────────────


def _changelog_label(rel_path: str) -> str:
    if rel_path == "CLAUDE.md":
        return "CLAUDE.md"
    parts = rel_path.split("/")
    if len(parts) >= 2 and parts[0] == "skills":
        return f"skills/{parts[1]}"
    return rel_path


def write_changelog(out_dir: Path, run_kind: str) -> None:
    """Manifest-diff approach. Compares CLAUDE.md + each skills/<slug>/SKILL.md mtime
    against the prior _manifest.json snapshot and prepends a dated entry to _changelog.md
    if anything was added/updated/removed. No-op if nothing changed."""
    manifest_path = out_dir / "_manifest.json"
    try:
        prev = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    except (json.JSONDecodeError, OSError):
        prev = {}

    current: dict[str, float] = {}
    claude_md = out_dir / "CLAUDE.md"
    if claude_md.exists():
        current["CLAUDE.md"] = claude_md.stat().st_mtime
    skills_dir = out_dir / "skills"
    if skills_dir.exists():
        for p in skills_dir.glob("*/SKILL.md"):
            current[str(p.relative_to(out_dir))] = p.stat().st_mtime

    # 1s slack to avoid noise when prev mtime equals current within filesystem precision
    added = sorted({_changelog_label(k) for k in current if k not in prev})
    updated = sorted({_changelog_label(k) for k in current
                      if k in prev and current[k] - prev[k] > 1.0})
    removed = sorted({_changelog_label(k) for k in prev if k not in current})

    if not (added or updated or removed):
        manifest_path.write_text(json.dumps(current, indent=2, sort_keys=True))
        return

    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"## {ts} — {run_kind}", ""]
    if added:
        lines.append("**Added:**")
        lines += [f"- {a}" for a in added]
        lines.append("")
    if updated:
        lines.append("**Updated:**")
        lines += [f"- {u}" for u in updated]
        lines.append("")
    if removed:
        lines.append("**Removed:**")
        lines += [f"- {r}" for r in removed]
        lines.append("")
    new_entry = "\n".join(lines) + "\n"

    changelog_path = out_dir / "_changelog.md"
    if changelog_path.exists():
        existing = changelog_path.read_text()
        if existing.startswith("# Changelog"):
            header, rest = existing.split("\n", 1)
            new_text = f"{header}\n\n{new_entry}{rest.lstrip(chr(10))}"
        else:
            new_text = f"# Changelog\n\n{new_entry}\n{existing}"
    else:
        new_text = f"# Changelog\n\n{new_entry}"
    changelog_path.write_text(new_text)
    manifest_path.write_text(json.dumps(current, indent=2, sort_keys=True))

    # Commit artifacts to a per-project git repo so the viewer can render
    # a diff between successive runs. Non-fatal on failure (git missing, etc.).
    last_commit: str | None = None
    try:
        last_commit = _git_commit_artifacts(
            project_dir=out_dir,
            run_kind=run_kind,
            ts_str=ts,
            added=added,
            updated=updated,
            removed=removed,
        )
    except Exception as e:
        print(f"      _git_commit_artifacts failed (non-fatal): {type(e).__name__}: {e}", flush=True)

    # Publish state for the Claude Code plugin to read at ~/.watchmen/.
    # Decoupled from the engine's install location so the plugin doesn't need
    # to know where the engine lives.
    try:
        _publish_watchmen_state(
            project_key=out_dir.name,
            run_kind=run_kind,
            ts_str=ts,
            added=added,
            updated=updated,
            removed=removed,
            last_commit=last_commit,
        )
    except Exception as e:
        print(f"      _publish_watchmen_state failed (non-fatal): {type(e).__name__}: {e}", flush=True)

    # Rebuild the FTS5 skill index. Plugin's UserPromptSubmit hook queries this
    # to surface "you could have used /<skill>" suggestions.
    try:
        _build_skill_index()
    except Exception as e:
        print(f"      _build_skill_index failed (non-fatal): {type(e).__name__}: {e}", flush=True)


def _git_commit_artifacts(
    project_dir: Path,
    run_kind: str,
    ts_str: str,
    added: list[str],
    updated: list[str],
    removed: list[str],
) -> str | None:
    """Init git in kai_claude/<project>/ on first call, commit the current state,
    return the resulting commit SHA. Returns None if git is unavailable, the
    directory is missing, or nothing changed on a working repo with no HEAD.

    Each curator run becomes a commit; the viewer renders the diff between
    consecutive commits as the substantive "what watchmen changed" view."""
    if not shutil.which("git") or not project_dir.exists():
        return None

    if not (project_dir / ".git").exists():
        r = subprocess.run(
            ["git", "-C", str(project_dir), "init", "-q", "-b", "main"],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            return None
        # Local identity so commits work in environments without global git config.
        subprocess.run(["git", "-C", str(project_dir), "config", "user.email", "curator@watchmen"],
                       capture_output=True)
        subprocess.run(["git", "-C", str(project_dir), "config", "user.name", "watchmen curator"],
                       capture_output=True)
        # Skip mtime-only bookkeeping that creates noise in diffs.
        gitignore = project_dir / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text("_manifest.json\n_run.log\n")

    subprocess.run(["git", "-C", str(project_dir), "add", "-A"], capture_output=True, text=True)
    r = subprocess.run(["git", "-C", str(project_dir), "status", "--porcelain"],
                       capture_output=True, text=True)
    if not r.stdout.strip():
        # No staged diff — return existing HEAD if any (curator wrote nothing substantive).
        r = subprocess.run(["git", "-C", str(project_dir), "rev-parse", "HEAD"],
                           capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else None

    title = f"{run_kind} @ {ts_str}"
    body_lines = []
    if added:
        body_lines.append("Added:")
        body_lines.extend(f"  - {a}" for a in added)
    if updated:
        body_lines.append("Updated:")
        body_lines.extend(f"  - {u}" for u in updated)
    if removed:
        body_lines.append("Removed:")
        body_lines.extend(f"  - {x}" for x in removed)
    msg = title + ("\n\n" + "\n".join(body_lines) if body_lines else "")

    r = subprocess.run(["git", "-C", str(project_dir), "commit", "-q", "-m", msg],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None
    r = subprocess.run(["git", "-C", str(project_dir), "rev-parse", "HEAD"],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def _extract_frontmatter_field(fm: str, field: str) -> str:
    """Pull a single field's value out of a SKILL.md YAML frontmatter as flat text.
    Joins bullet lists into space-separated plain text suitable for FTS5 indexing."""
    pat = rf"^{field}:\s*(.*?)(?=\n[a-z_]+:\s|\n---|\Z)"
    m = re.search(pat, fm, re.MULTILINE | re.DOTALL)
    if not m:
        return ""
    raw = m.group(1).strip()
    parts = []
    for line in raw.split("\n"):
        line = line.strip()
        if line.startswith(("- ", "* ")):
            line = line[2:].strip()
        if line:
            parts.append(line)
    return " ".join(parts)


def _build_skill_index() -> None:
    """Rebuild ~/.watchmen/skill_index.db (FTS5) from every tracked project's skill
    bundles. The plugin's UserPromptSubmit hook queries this to surface
    'you could have used /<skill> to save time & tokens' indicators."""
    import state as _state

    base = Path.home() / ".watchmen"
    base.mkdir(parents=True, exist_ok=True)
    db_path = base / "skill_index.db"

    rows: list[tuple[str, str, str, str]] = []
    for p in _state.list_projects():
        project_key = p.get("project_key")
        if not project_key:
            continue
        skills_dir = ROOT / "kai_claude" / project_key / "skills"
        if not skills_dir.exists():
            continue
        for skill_dir in skills_dir.iterdir():
            if not skill_dir.is_dir():
                continue
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.exists():
                continue
            content = skill_md.read_text(errors="replace")
            m = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
            fm = m.group(1) if m else ""
            when_to = _extract_frontmatter_field(fm, "when_to_use")
            when_not = _extract_frontmatter_field(fm, "when_not_to_use")
            description = _extract_frontmatter_field(fm, "description")
            # Indexable: trigger phrases + description. when_not_to_use is stored but kept
            # in a separate column so the hook can subtract it later if needed.
            indexable = " ".join(filter(None, [when_to, description]))
            rows.append((project_key, skill_dir.name, indexable, when_not))

    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("DROP TABLE IF EXISTS skill_match")
        conn.execute("""
            CREATE VIRTUAL TABLE skill_match USING fts5(
                when_to_use,
                when_not_to_use,
                skill_slug UNINDEXED,
                project_key UNINDEXED
            )
        """)
        conn.executemany(
            "INSERT INTO skill_match (project_key, skill_slug, when_to_use, when_not_to_use) "
            "VALUES (?, ?, ?, ?)",
            rows,
        )
    print(f"      indexed {len(rows)} skill(s) across projects → {db_path}", flush=True)


def _publish_watchmen_state(
    project_key: str,
    run_kind: str,
    ts_str: str,
    added: list[str],
    updated: list[str],
    removed: list[str],
    last_commit: str | None,
) -> None:
    """Write ~/.watchmen/state/<project>.json + refresh ~/.watchmen/projects.json.

    Plugin reads these to render the statusLine indicator and the /watchmen:brief
    skill body. Format is stable; bump the schema version if changing fields."""
    import state as _state  # local import to avoid pulling state into module imports

    base = Path.home() / ".watchmen"
    state_dir = base / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    # Pick a suggested skill: prefer a freshly-added one (whole bundles), else fall back to nothing.
    added_skills = [a for a in added if a.startswith("skills/")]
    suggested = added_skills[0].split("/", 1)[1] if added_skills else None

    parts = []
    if added:
        parts.append(f"+{len(added)} added")
    if updated:
        parts.append(f"~{len(updated)} updated")
    if removed:
        parts.append(f"-{len(removed)} removed")
    summary = ", ".join(parts) or "no changes"

    if last_commit:
        diff_url = f"http://127.0.0.1:8888/p/{project_key}/diff/{last_commit}"
    else:
        diff_url = None

    payload = {
        "schema": 2,
        "project_key": project_key,
        "ts": ts_str,
        "run_kind": run_kind,
        "summary": summary,
        "details": {"added": added, "updated": updated, "removed": removed},
        "suggested_skill": suggested,
        "last_commit": last_commit,
        "viewer_url": f"http://127.0.0.1:8888/p/{project_key}",
        "diff_url": diff_url,
    }
    (state_dir / f"{project_key}.json").write_text(json.dumps(payload, indent=2))

    # Refresh projects.json index so the plugin can resolve cwd → project_key.
    try:
        projects = _state.list_projects()
        index = [
            {"project_key": p["project_key"], "source_repo": p["source_repo"]}
            for p in projects
            if p.get("source_repo")
        ]
        (base / "projects.json").write_text(json.dumps(index, indent=2))
    except Exception:
        pass  # index refresh is best-effort; state file already written


# ─── Driver ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True, help="project key, e.g. 'tally-weijl-images'")
    parser.add_argument("--repo", required=True, help="absolute path to source repo on disk")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--max-skills", type=int, default=8)
    parser.add_argument("--skip-finder", action="store_true", help="reuse existing _candidates.json")
    parser.add_argument("--skip-skills", action="store_true", help="skip stage 2 — assume kai_claude/<project>/skills/* is already populated")
    parser.add_argument("--regen-all", action="store_true", help="invalidate every input cache for this project, forcing full re-curation")
    parser.add_argument("--curator-concurrency", type=int, default=4, help="parallel per-skill curator agents in stage 2 (default 4)")
    args = parser.parse_args()

    api_key = load_api_key()
    out_dir = ROOT / "kai_claude" / args.project
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "_run.log"
    log_path.write_text("", encoding="utf-8")  # truncate

    if args.regen_all:
        removed = invalidate_all(out_dir)
        print(f"   --regen-all: invalidated {removed} cache file(s)", flush=True)

    # Build a separate, un-instrumented handler set for cache replay. We rebuild
    # it lazily per stage (Stage 2 has per-skill scoped handlers) — but for
    # Stages 1 and 3 the shared set is enough.
    _, replay_handlers = make_tools(source_repo=args.repo, project_key=args.project)

    print(f"== curate {args.project} (repo={args.repo}, model={args.model})", flush=True)
    print(f"   output → kai_claude/{args.project}/  log → {log_path.name}", flush=True)

    with httpx.Client(timeout=300.0) as client:
        run_critic = make_critic_runner(client, args.model, args.project, log_path)

        # ─── Stage 1: candidate-finder ────────────────────────────────────────
        candidates_path = out_dir / "_candidates.json"
        candidates_cache = out_dir / ".candidates.inputs.json"
        finder_recorder = ReadRecorder()

        if args.skip_finder and candidates_path.exists():
            print("[1/4] skipping finder, reusing _candidates.json", flush=True)
            candidates = json.loads(candidates_path.read_text())
        elif candidates_path.exists() and cache_hit(candidates_cache, replay_handlers):
            print("[1/4] candidate-finder: cache hit — reusing _candidates.json", flush=True)
            candidates = json.loads(candidates_path.read_text())
        else:
            print("[1/4] candidate-finder...", flush=True)
            t0 = time.time()
            finder = build_finder_agent(client, args.model, args.project, args.repo, log_path, finder_recorder)
            result, _ = finder.run(
                f"Identify the strongest procedural skill candidates for project '{args.project}' "
                f"located at '{args.repo}'. Verify each has artifacts. Cap at {args.max_skills}.",
                max_iter=36,
            )
            candidates = (result or {}).get("candidates", [])[: args.max_skills]
            candidates_path.write_text(json.dumps(candidates, indent=2))
            # Persist read-log only on successful candidate emission (terminal tool fired).
            if candidates:
                from cache import write_cache
                write_cache(candidates_cache, finder_recorder)
            print(f"      → {len(candidates)} candidates in {time.time()-t0:.1f}s", flush=True)
            for c in candidates:
                print(f"         - {c.get('slug', '?'):<35} {c.get('description', '')[:80]}", flush=True)

        if not candidates:
            print("no candidates — stopping.", flush=True)
            return

        # ─── Stage 2: per-skill curator ───────────────────────────────────────
        completed: list[str] = []
        if args.skip_skills:
            existing_skills_dir = out_dir / "skills"
            if existing_skills_dir.exists():
                completed = sorted(d.name for d in existing_skills_dir.iterdir() if d.is_dir())
            print(f"[2/4] skipping stage 2 — found {len(completed)} existing skills: {', '.join(completed)}", flush=True)
        else:
            # Phase 2a: sequential cache scan — cheap (~ms per skill), determines
            # which candidates need the expensive agent run.
            cache_hits: list[str] = []
            miss_list: list[dict] = []
            for cand in candidates:
                slug = cand.get("slug")
                if not slug:
                    continue
                skill_dir = out_dir / "skills" / slug
                if (skill_dir / "SKILL.md").exists() and cache_hit(skill_dir / ".inputs.json", replay_handlers):
                    cache_hits.append(slug)
                else:
                    miss_list.append(cand)
            completed.extend(cache_hits)

            print(
                f"[2/4] per-skill curators ({len(candidates)} skills, {len(cache_hits)} cached, "
                f"{len(miss_list)} to curate, concurrency={args.curator_concurrency})...",
                flush=True,
            )
            for slug in cache_hits:
                print(f"      {slug} — cache hit", flush=True)

            # Phase 2b: parallel agent runs for cache misses. Each skill is
            # independent — separate Agent instance, separate output directory,
            # separate cache file. httpx.Client is thread-safe and shared.
            def _curate_one(cand: dict) -> tuple[str, str | None, ReadRecorder, float]:
                """Run one skill curator. Returns (slug, summary, recorder, elapsed_seconds).
                summary=None means the agent didn't fire its terminal tool."""
                slug = cand["slug"]
                t0 = time.time()
                rec = ReadRecorder()
                curator = build_skill_curator(
                    client, args.model, args.project, args.repo, cand, log_path, run_critic, rec
                )
                result, _ = curator.run(
                    f"Author the skill bundle for '{cand['name']}'. Investigate, draft, run the critic, refine, finish.",
                    max_iter=45,
                )
                summary = (result or {}).get("summary") if result else None
                return slug, summary, rec, time.time() - t0

            concurrency = max(1, args.curator_concurrency)
            done_count = 0
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                futures = {pool.submit(_curate_one, c): c for c in miss_list}
                for fut in as_completed(futures):
                    cand = futures[fut]
                    slug = cand.get("slug", "?")
                    done_count += 1
                    try:
                        slug, summary, rec, elapsed = fut.result()
                        if summary is not None:
                            completed.append(slug)
                            write_cache(out_dir / "skills" / slug / ".inputs.json", rec)
                            print(
                                f"      [{done_count}/{len(miss_list)}] {slug} done in {elapsed:.1f}s — {(summary or '')[:80]}",
                                flush=True,
                            )
                        else:
                            print(f"      [{done_count}/{len(miss_list)}] {slug} no finish call in {elapsed:.1f}s", flush=True)
                    except Exception as e:
                        print(f"      [{done_count}/{len(miss_list)}] {slug} FAILED: {type(e).__name__}: {e}", flush=True)

        # ─── Stage 3: claude.md author ────────────────────────────────────────
        claude_md_path = out_dir / "CLAUDE.md"
        claude_md_cache = out_dir / ".claude_md.inputs.json"
        if claude_md_path.exists() and cache_hit(claude_md_cache, replay_handlers):
            print("[3/4] claude.md author: cache hit — reusing CLAUDE.md", flush=True)
        else:
            print("[3/4] claude.md author...", flush=True)
            t0 = time.time()
            try:
                claude_md_recorder = ReadRecorder()
                author = build_claude_md_author(client, args.model, args.project, args.repo, log_path, run_critic, claude_md_recorder)
                result, _ = author.run(
                    f"Author CLAUDE.md for '{args.project}'. {len(completed)} skills generated: {', '.join(completed)}.",
                    max_iter=30,
                )
                if result:
                    write_cache(claude_md_cache, claude_md_recorder)
                print(f"      done in {time.time()-t0:.1f}s — {(result or {}).get('summary', '')[:80]}", flush=True)
            except Exception as e:
                print(f"      FAILED: {type(e).__name__}: {e}", flush=True)

        # ─── Stage 4: write _index.md ─────────────────────────────────────────
        print("[4/4] writing _index.md...", flush=True)
        index_lines = [f"# kai_claude/{args.project} — generated artifacts\n"]
        index_lines.append(f"- Model: {args.model}")
        index_lines.append(f"- Source repo: {args.repo}")
        index_lines.append(f"- Skills generated: {len(completed)}\n")
        index_lines.append("## Skills\n")
        for slug in completed:
            cand = next((c for c in candidates if c.get("slug") == slug), {})
            index_lines.append(f"- **{slug}** — {cand.get('description', '')}")
        index_lines.append("\n## Files\n")
        for p in sorted((out_dir).rglob("*")):
            if p.is_file():
                rel = p.relative_to(out_dir)
                index_lines.append(f"- `{rel}` ({p.stat().st_size:,} bytes)")
        (out_dir / "_index.md").write_text("\n".join(index_lines), encoding="utf-8")

        run_kind = "claude-md regen" if (args.skip_finder and args.skip_skills) else "full curator"
        try:
            write_changelog(out_dir, run_kind)
        except Exception as e:
            print(f"      changelog write failed: {type(e).__name__}: {e}", flush=True)

        print(f"      done. final output: kai_claude/{args.project}/", flush=True)


if __name__ == "__main__":
    main()
