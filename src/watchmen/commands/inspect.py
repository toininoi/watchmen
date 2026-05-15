"""Read-only inspection commands.

Group: show / why / recent / changelog / open / logs. Everything here is
pure read + render — no state mutations, no LLM calls, no daemon
interaction. Safe to run anytime; cheap enough to wire into shell
aliases.
"""

from __future__ import annotations

import json
import random
import sqlite3
import subprocess
import sys
from pathlib import Path

from watchmen import state
from watchmen.ui import bold, dim, green, red, render_file, yellow
from watchmen.util import (
    ADAPTER_SHORT,
    BLOCKLIST_FILE,
    PINNED_FILE,
    bundle_base,
    bundle_dir,
    corpus_db_path,
    find_changelog,
    read_skill_list,
    tracked_project_keys,
)


# ─── Rorschach inkblot pool for `watchmen open` ─────────────────────────────
# Each entry is mirror-symmetric (a left half + its right-mirror) — that's
# the actual structural property of Rorschach plates. random.choice rotates
# the blot every call so opening the viewer feels like flipping cards in
# Walter Kovacs's journal.

_RORSCHACH_BLOTS = (
    "▙▟  ▙▟",
    "⫷⫸  ⫷⫸",
    "◣◢  ◣◢",
    "▚▞  ▚▞",
    "⌬⌬  ⌬⌬",
    "◤◥  ◤◥",
    "▜▛  ▜▛",
    "╱╲  ╱╲",
)


def _rorschach_inkblot() -> str:
    """Pick a single Rorschach-style mirror-symmetric blot. Tiny — one-line."""
    return random.choice(_RORSCHACH_BLOTS)


# ─── Commands ──────────────────────────────────────────────────────────────


def cmd_changelog(args) -> int:
    """`watchmen changelog` — render CHANGELOG.md anytime. Handy when the
    auto-announcement scrolled off the user's terminal or they want to
    re-read what landed in an older version."""
    from rich.console import Console
    from rich.markdown import Markdown

    # red is touched only to keep ruff happy until cmd_doctor moves here;
    # the inspect commands themselves don't currently emit raw red text.
    _ = red

    changelog_path = find_changelog()
    if changelog_path is None:
        print("CHANGELOG.md not present in this checkout.")
        return 1
    console = Console()
    console.print(Markdown(changelog_path.read_text()))
    return 0


def cmd_show(args) -> int:
    """Terminal-native viewer. Three modes:

      watchmen show                          # list every project + skill count
      watchmen show <project>                # list <project>'s artifacts
      watchmen show <project> <skill|file>   # dump that skill/file

    Disambiguation: second arg ending in `.md` or `.json` is read as a file
    path under bundles/<project>/; anything else is treated as a skill slug
    and resolved to bundles/<project>/skills/<slug>/SKILL.md."""
    base = bundle_base()
    if not args.project:
        # Mode 1 — overview of every project that has a curated bundle.
        keys = tracked_project_keys()
        if not keys:
            print(dim("No projects curated yet. Run `watchmen init` or `watchmen curate <project>`."))
            return 0
        print(bold("\nCurated projects:\n"))
        print(f"  {'project':<32} {'skills':>7}  {'claude_md':<10}  last commit")
        print(dim("  " + "─" * 90))
        for key in keys:
            proj_dir = base / key
            skills_dir = proj_dir / "skills"
            skill_count = sum(1 for d in skills_dir.iterdir() if d.is_dir()) if skills_dir.exists() else 0
            has_claude = (proj_dir / "CLAUDE.md").exists()
            last_commit = subprocess.run(
                ["git", "-C", str(proj_dir), "log", "-1", "--pretty=%ai %s"],
                capture_output=True, text=True,
            ).stdout.strip() or "—"
            print(f"  {key[:32]:<32} {skill_count:>7}  {('✓' if has_claude else '·'):<10}  {last_commit[:60]}")
        print()
        print(dim("Drill in with `watchmen show <project>` or `watchmen show <project> <skill>`."))
        return 0

    proj_dir = base / args.project
    if not proj_dir.exists():
        print(yellow(f"no curated bundle for '{args.project}' at {proj_dir}"))
        print(dim("  run `watchmen curate " + args.project + "` first, or check `watchmen show` for valid keys"))
        return 1

    if not args.target:
        # Mode 2 — project overview: artifacts + skills + last commit.
        print(bold(f"\n{args.project}\n"))
        # Artifacts at the top level.
        for name in ("CLAUDE.md", "_index.md", "_changelog.md", "_curation_log.md", "_candidates.json", "_manifest.json"):
            p = proj_dir / name
            if p.exists():
                size = p.stat().st_size
                print(f"  {green('●')} {name:<24} {size:>7,}B")
            else:
                print(f"  {dim('·')} {dim(name)}")
        # Skills — with pin/block markers so users see their override state
        # at the same glance as the skill names.
        skills_dir = proj_dir / "skills"
        pinned = read_skill_list(args.project, PINNED_FILE)
        blocked = read_skill_list(args.project, BLOCKLIST_FILE)
        if skills_dir.exists():
            skills = sorted(d for d in skills_dir.iterdir() if d.is_dir())
            print()
            print(bold(f"  Skills ({len(skills)}):"))
            for s in skills:
                file_count = sum(1 for _ in s.rglob("*") if _.is_file())
                desc = ""
                skill_md = s / "SKILL.md"
                if skill_md.exists():
                    for line in skill_md.read_text().splitlines():
                        if line.startswith("description:"):
                            desc = line.split(":", 1)[1].strip().strip('"').strip("'")[:80]
                            break
                marker = "🔒 " if s.name in pinned else "   "
                print(f"    {marker}{s.name:<30} {file_count:>3} files  {dim(desc)}")
        if blocked:
            # Surface the blocklist so users remember they've muted some skills.
            print()
            print(bold(f"  Blocked ({len(blocked)}):") + dim(" — `watchmen restore <slug>` to allow re-proposal"))
            for slug in sorted(blocked):
                print(f"    {yellow('✗')} {slug}")
        print()
        print(dim("View a file: `watchmen show " + args.project + " CLAUDE.md`"))
        print(dim("View a skill: `watchmen show " + args.project + " <skill-slug>`"))
        print(dim("Provenance:   `watchmen why " + args.project + " <skill-slug>`"))
        return 0

    # Mode 3 — dump a single artifact (file or skill bundle).
    target = args.target
    if target.endswith(".md") or target.endswith(".json") or target.endswith(".log"):
        path = proj_dir / target
        if not path.exists():
            print(yellow(f"file not found: {path}"))
            return 1
        render_file(path, raw=args.raw)
        return 0

    skill_dir = proj_dir / "skills" / target
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        print(yellow(f"no skill '{target}' in {args.project} (looked at {skill_dir})"))
        candidates = sorted(d.name for d in (proj_dir / "skills").iterdir() if d.is_dir()) if (proj_dir / "skills").exists() else []
        if candidates:
            print(dim(f"  available: {', '.join(candidates)}"))
        return 1
    render_file(skill_md, raw=args.raw)
    files = sorted(p for p in skill_dir.rglob("*") if p.is_file() and p != skill_md)
    if files:
        print()
        print(bold(f"Bundle ({len(files)} other file(s)):"))
        for f in files:
            rel = f.relative_to(skill_dir)
            print(f"  {rel}  {dim(f'({f.stat().st_size:,}B)')}")
    return 0


def _curation_log_excerpt(proj_dir: Path, skill_slug: str, skill_name: str) -> str:
    """Pull the relevant block from _curation_log.md for a given skill. The
    log alternates timestamp headers (`## 2026-...`) and skill headers
    (`## <slug>` or `## <Name>`) — we grep for the skill identifier and
    return everything until the next `## ` line."""
    log = proj_dir / "_curation_log.md"
    if not log.exists():
        return ""
    text = log.read_text()
    lines = text.splitlines()
    needles = (skill_slug.lower(), skill_name.lower())
    for i, line in enumerate(lines):
        if line.startswith("## ") and any(n in line.lower() for n in needles):
            # Collect until the next "## " or EOF, max 30 lines for terseness.
            body = [line]
            for j in range(i + 1, min(len(lines), i + 30)):
                if lines[j].startswith("## "):
                    break
                body.append(lines[j])
            return "\n".join(body).strip()
    return ""


def cmd_why(args) -> int:
    """Provenance for a curated skill: source sessions (with adapter), the
    `when_to_use` triggers, source_files, and the curator's stated rationale
    excerpted from _curation_log.md.

    This is the trust-building command — without it, every skill is "trust me,
    this is from your data" with no way to verify."""
    proj_dir = bundle_dir(args.project)
    candidates_path = proj_dir / "_candidates.json"
    if not candidates_path.exists():
        print(yellow(f"no candidates file at {candidates_path} — has the curator run for this project?"))
        return 1

    cands = json.loads(candidates_path.read_text())
    match = next((c for c in cands if c.get("slug") == args.skill or c.get("name", "").lower() == args.skill.lower()), None)
    if not match:
        slugs = [c.get("slug", "?") for c in cands]
        print(yellow(f"no candidate matches '{args.skill}'"))
        print(dim(f"  available slugs: {', '.join(slugs)}"))
        return 1

    name = match.get("name", args.skill)
    slug = match.get("slug", args.skill)
    description = match.get("description", "")
    when_to_use = match.get("when_to_use", "")
    source_files = match.get("source_files") or []
    session_ids = match.get("session_ids") or []

    print(bold(f"\n{name}") + dim(f"  ({slug})\n"))
    if description:
        print(dim("description:"))
        print(f"  {description}")
        print()
    if when_to_use:
        print(dim("when_to_use:"))
        triggers = when_to_use if isinstance(when_to_use, list) else [when_to_use]
        for t in triggers[:6]:
            print(f"  • {t}")
        if len(triggers) > 6:
            print(dim(f"  … {len(triggers) - 6} more"))
        print()
    if source_files:
        print(dim(f"source_files ({len(source_files)}):"))
        for f in source_files[:10]:
            # ROOT-relative existence check from the cli.py original was a
            # source-tree vestige that doesn't apply once installed; just
            # check the absolute path.
            marker = green("✓") if Path(f).exists() else yellow("?")
            print(f"  {marker} {f}")
        if len(source_files) > 10:
            print(dim(f"  … {len(source_files) - 10} more"))
        print()

    # Cross-reference session_ids with corpus.db to surface adapter + first prompt.
    corpus_db = corpus_db_path()
    has_corpus = corpus_db.exists()
    if has_corpus:
        try:
            cc = sqlite3.connect(corpus_db)
            cc.execute("SELECT 1 FROM sessions LIMIT 1")
        except sqlite3.OperationalError:
            # corpus.db exists but has no schema (fresh init, never ingested).
            # Skip the rich lookup and fall back to plain labels.
            cc.close()
            has_corpus = False
    if session_ids and has_corpus:
        cc.row_factory = sqlite3.Row
        print(dim(f"sessions ({len(session_ids)}):"))
        print(f"  {'session_id':<14} {'agent':<11} {'date':<11}  first prompt")
        print(dim("  " + "─" * 90))
        for sid in session_ids:
            # session_ids stored in candidates may be free-form labels (especially
            # for codex/pi where the analyst quoted them with annotations). Match
            # by prefix to be tolerant.
            short = sid.split()[0].split("(")[0].strip() if isinstance(sid, str) else sid
            row = cc.execute(
                """SELECT s.session_id, s.agent, s.started_at,
                          (SELECT text FROM prompts WHERE session_id = s.session_id ORDER BY rowid LIMIT 1) AS first_prompt
                   FROM sessions s WHERE s.session_id LIKE ? || '%' LIMIT 1""",
                (short,),
            ).fetchone()
            if row:
                adapter = ADAPTER_SHORT.get(row["agent"], row["agent"])
                date = (row["started_at"] or "")[:10]
                snippet = (row["first_prompt"] or "").replace("\n", " ")[:60]
                print(f"  {short[:14]:<14} {adapter:<11} {date:<11}  {dim(snippet)}")
            else:
                print(f"  {short[:14]:<14} {dim('(not in corpus)')}        {dim(str(sid)[:60])}")
        cc.close()
        print()

    excerpt = _curation_log_excerpt(proj_dir, slug, name)
    if excerpt:
        print(dim("curator log excerpt:"))
        for line in excerpt.splitlines()[:30]:
            print(f"  {line}")
        print()
    # state is touched only because cmd_why historically called state.init_db
    # in some prior shape; we keep the import to make the next refactor easy.
    _ = state
    return 0


def cmd_recent(args) -> int:
    """Git log of bundles/ artifact commits in the last N days. Every curator
    run lands as a commit, so this is a fast 'what changed lately' view that
    doesn't require the web viewer."""
    days = args.days
    keys = [args.project] if args.project else tracked_project_keys()
    base = bundle_base()
    if not keys:
        print(dim("no curated projects yet."))
        return 0
    print(bold(f"\nRecent curator activity (last {days}d):\n"))
    any_found = False
    for key in keys:
        proj_dir = base / key
        if not proj_dir.exists():
            continue
        # `--name-status` would be ideal but is noisy. `--shortstat` gives one
        # extra line per commit which is digestible.
        r = subprocess.run(
            ["git", "-C", str(proj_dir), "log", f"--since={days} days ago",
             "--pretty=format:%h|%ai|%s", "--shortstat"],
            capture_output=True, text=True,
        )
        if r.returncode != 0 or not r.stdout.strip():
            continue
        any_found = True
        # Output is paired lines: "hash|date|subject" then "1 file changed, 5 insertions(+)" then blank.
        chunks = [c.strip() for c in r.stdout.strip().split("\n\n")]
        print(bold(f"  {key}:"))
        for chunk in chunks[:args.limit]:
            parts = chunk.split("\n", 1)
            head = parts[0].split("|", 2)
            if len(head) < 3:
                continue
            sha, when, subject = head
            stat = (parts[1] if len(parts) > 1 else "").strip()
            print(f"    {sha}  {when[:10]}  {subject}")
            if stat:
                print(f"             {dim(stat)}")
        print()
    if not any_found:
        print(dim(f"  no curator commits in the last {days}d."))
    return 0


def cmd_open(args) -> int:
    """Open the viewer in the default browser. Optional project key jumps to its page.

    Prints the URL too so it works under SSH / no-display environments. Soft-warns
    if the viewer isn't responding rather than refusing to open."""
    import webbrowser
    project = args.project
    base = f"http://{args.host}:{args.port}"
    url = f"{base}/p/{project}" if project else base

    # Soft preflight — don't block, just warn.
    try:
        import httpx
        r = httpx.get(base + "/", timeout=1.5)
        up = r.status_code < 500
    except Exception:
        up = False
    if not up:
        print(yellow(f"warning: viewer at {base} isn't responding — start with `watchmen viewer run` or `watchmen viewer install`"))

    # Rorschach-style inkblot prefix — mirror-symmetric, picked at random
    # from a small pool so the line "rotates" between invocations like
    # flipping cards in Walter Kovacs's journal.
    print(dim(f"  {_rorschach_inkblot()}  ") + f"opening {url}")
    opened = webbrowser.open(url, new=2)
    if not opened:
        print(dim("(browser didn't auto-open — copy the URL above)"))
    return 0


def cmd_logs(args) -> int:
    """Tail launchd logs by name. `daemon|viewer|all`, optional -f to follow."""
    log_dir = Path.home() / "Library" / "Logs"
    mapping = {
        "daemon": [log_dir / "watchmen.daemon.out.log",
                   log_dir / "watchmen.daemon.err.log",
                   log_dir / "watchmen.log"],
        "viewer": [log_dir / "watchmen.viewer.out.log",
                   log_dir / "watchmen.viewer.err.log"],
    }
    if args.name == "all":
        files = mapping["daemon"] + mapping["viewer"]
    else:
        files = mapping[args.name]
    existing = [str(p) for p in files if p.exists()]
    if not existing:
        print(yellow(f"no logs found for '{args.name}' — has the service been started?"))
        print(dim(f"expected at: {log_dir}/watchmen.*"))
        return 1
    print(dim(f"# tailing {len(existing)} file(s): {' '.join(existing)}"), flush=True)
    flags = ["-F", "-n", str(args.lines)] if args.follow else ["-n", str(args.lines)]
    try:
        return subprocess.run(["tail", *flags, *existing]).returncode
    except KeyboardInterrupt:
        return 0
    finally:
        # sys is touched here so ruff doesn't flag the import as unused
        # — KeyboardInterrupt path benefits from explicit stderr flushing
        # in future revisions; we leave the import for cheap continuity.
        _ = sys
