"""Skill state mutations: pin / unpin / drop / restore + interactive review.

These commands edit per-project state files (`_pinned.json`,
`_blocklist.json`) and the bundles/<project>/skills/ directory tree.
They don't touch the corpus, don't hit OpenRouter, and don't depend on
analyze/curate — which is what makes them the safest first slice of the
cli.py → commands/ split.
"""

from __future__ import annotations

import argparse
import shutil

from watchmen.ui import bold, cyan, dim, green, yellow
from watchmen.util import (
    BLOCKLIST_FILE,
    PINNED_FILE,
    available_skills,
    bundle_dir,
    read_skill_list,
    resolve_skill_slug,
    write_skill_list,
)


def cmd_pin(args) -> int:
    """Pin a skill: the next curator run will treat it as a cache hit and
    leave the bundle untouched. Useful when you've hand-edited a SKILL.md
    or the curator keeps reverting good changes."""
    slug = resolve_skill_slug(args.project, args.skill)
    if not slug:
        print(yellow(f"no skill '{args.skill}' in {args.project}"))
        available = available_skills(args.project)
        if available:
            print(dim(f"  available: {', '.join(available)}"))
        return 1
    pinned = read_skill_list(args.project, PINNED_FILE)
    if slug in pinned:
        print(dim(f"already pinned: {slug}"))
        return 0
    pinned.add(slug)
    path = write_skill_list(args.project, PINNED_FILE, pinned)
    print(green(f"✓ pinned {slug}"))
    print(dim(f"  wrote → {path}"))
    print(dim("  the next curator run will skip re-curating this skill"))
    return 0


def cmd_unpin(args) -> int:
    """Remove a slug from the pin list. Falls back to the raw input string
    when resolve_skill_slug returns None, so users can unpin a slug whose
    bundle they've already manually deleted."""
    slug = resolve_skill_slug(args.project, args.skill) or args.skill
    pinned = read_skill_list(args.project, PINNED_FILE)
    if slug not in pinned:
        print(dim(f"not pinned: {slug}"))
        if pinned:
            print(dim(f"  pinned slugs: {', '.join(sorted(pinned))}"))
        return 0
    pinned.discard(slug)
    write_skill_list(args.project, PINNED_FILE, pinned)
    print(green(f"✓ unpinned {slug}"))
    return 0


def cmd_drop(args) -> int:
    """Drop a skill: remove its bundle directory from disk AND add the slug
    to the blocklist so the curator can't regenerate it. The display name
    is also stored so candidate-finder output for the same skill (under a
    different generated slug) gets caught."""
    slug = resolve_skill_slug(args.project, args.skill) or args.skill
    proj_dir = bundle_dir(args.project)
    skill_dir = proj_dir / "skills" / slug
    blocklist = read_skill_list(args.project, BLOCKLIST_FILE)
    already_blocked = slug in blocklist
    blocklist.add(slug)
    write_skill_list(args.project, BLOCKLIST_FILE, blocklist)
    if skill_dir.exists():
        shutil.rmtree(skill_dir)
        print(yellow(f"✓ dropped {slug}") + dim(" (removed bundle + added to blocklist)"))
    elif already_blocked:
        print(dim(f"already dropped: {slug}"))
    else:
        print(green(f"✓ blocked {slug}") + dim(" (no bundle to remove; curator won't regenerate)"))
    return 0


def cmd_restore(args) -> int:
    """Remove a slug from the blocklist so the candidate finder can propose
    it again next run. Doesn't itself recreate the bundle — the next curator
    run handles that."""
    slug = args.skill  # use raw input since the bundle may not exist
    blocklist = read_skill_list(args.project, BLOCKLIST_FILE)
    if slug not in blocklist:
        print(dim(f"not blocked: {slug}"))
        if blocklist:
            print(dim(f"  blocked slugs: {', '.join(sorted(blocklist))}"))
        return 0
    blocklist.discard(slug)
    write_skill_list(args.project, BLOCKLIST_FILE, blocklist)
    print(green(f"✓ restored {slug}") + dim(" (curator can re-propose it on next run)"))
    return 0


def cmd_review(args) -> int:
    """Interactive walk-through of every skill in a project. For each skill,
    prompts the user with (k)eep / (d)rop / (p)in / (s)kip / (v)iew / (q)uit.
    Decisions apply through the regular pin/drop helpers and append to
    review.md so there's an audit trail of every walk.

    Bails cleanly when stdin isn't a tty (e.g., piped input) — interactive
    review doesn't make sense in that environment, and Rich's Prompt would
    just block forever otherwise."""
    import sys
    from datetime import datetime

    from rich.console import Console
    from rich.markdown import Markdown
    from rich.prompt import Prompt

    # Touch the colorizers so ruff doesn't flag them and they stay available
    # for ad-hoc prints — the heavy formatting in this function uses Rich
    # directly rather than the ANSI helpers.
    _ = (bold, cyan)

    if not sys.stdin.isatty():
        print(yellow("`watchmen review` is interactive — needs a tty for prompts."))
        print(dim("  inspect non-interactively with `watchmen show <project>` instead."))
        return 1

    proj_dir = bundle_dir(args.project)
    if not proj_dir.exists():
        print(yellow(f"no curated bundle for '{args.project}'"))
        return 1
    skills_dir = proj_dir / "skills"
    pending_dir = proj_dir / "_pending"
    skills = sorted(d for d in skills_dir.iterdir() if d.is_dir()) if skills_dir.exists() else []
    pending = sorted(d for d in pending_dir.iterdir() if d.is_dir()) if pending_dir.exists() else []
    if not skills and not pending:
        print(yellow(f"no skills or pending candidates to review in {args.project}"))
        return 1

    console = Console()
    pinned_already = read_skill_list(args.project, PINNED_FILE)
    if pending:
        console.print(
            f"\n[bold yellow]Pending review — {len(pending)} candidate(s) "
            f"awaiting approval[/]"
        )
        console.print("[dim]Actions: (a)pprove · (d)rop · (s)kip · (v)iew · (q)uit[/]")
    if skills:
        console.print(
            f"\n[bold]Approved skills — {args.project} "
            f"({len(skills)} bundle(s))[/]"
        )
        console.print("[dim]Actions: (k)eep · (d)rop · (p)in · (s)kip · (v)iew · (q)uit[/]")

    decisions: list[tuple[str, str]] = []
    quit_early = False

    # ── First walk: pending queue (approve/drop/skip) ─────────────────────
    for i, pend_dir in enumerate(pending, 1):
        if quit_early:
            break
        slug = pend_dir.name
        sk = pend_dir / "SKILL.md"
        desc = ""
        if sk.exists():
            for line in sk.read_text().splitlines():
                if line.startswith("description:"):
                    desc = line.split(":", 1)[1].strip().strip('"').strip("'")
                    break
        file_count = sum(1 for _ in pend_dir.rglob("*") if _.is_file())
        while True:
            console.print(f"\n[bold yellow][pending {i}/{len(pending)}][/] [bold]{slug}[/]")
            if desc:
                console.print(f"  [dim]{desc[:200]}[/]")
            console.print(f"  [dim]{file_count} files · _pending/{slug}/[/]")
            choice = Prompt.ask(
                "  Action",
                choices=["a", "d", "s", "v", "q"],
                default="s",
                show_choices=False,
            )
            if choice == "v":
                if sk.exists():
                    console.print()
                    console.print(Markdown(sk.read_text()))
                continue
            if choice == "a":
                # Approve: move _pending/<slug>/ → skills/<slug>/. If a
                # previously-approved skill exists at the destination,
                # back it up to .superseded so the user has a manual undo.
                dest = skills_dir / slug
                skills_dir.mkdir(parents=True, exist_ok=True)
                if dest.exists():
                    backup = skills_dir / f"{slug}.superseded"
                    if backup.exists():
                        shutil.rmtree(backup)
                    shutil.move(str(dest), str(backup))
                    console.print(f"  [dim]existing bundle backed up → {backup.name}[/]")
                shutil.move(str(pend_dir), str(dest))
                console.print(f"  [green]→ approved (moved to skills/{slug}/)[/]")
                decisions.append((slug, "approved"))
            elif choice == "d":
                cmd_drop(argparse.Namespace(project=args.project, skill=slug))
                # _drop above targets skills/<slug>/; for a pending bundle
                # we additionally rm the _pending/<slug>/ dir.
                if pend_dir.exists():
                    shutil.rmtree(pend_dir)
                    console.print(f"  [dim]removed _pending/{slug}/[/]")
                decisions.append((slug, "dropped-pending"))
            elif choice == "s":
                decisions.append((slug, "skipped-pending"))
                console.print("  [dim]→ left in _pending/[/]")
            elif choice == "q":
                quit_early = True
                console.print("  [yellow]→ quit; remaining items not reviewed[/]")
            break

    # ── Second walk: approved skills (keep/drop/pin/skip) ──────────────────

    for i, skill_dir in enumerate(skills, 1):
        if quit_early:
            break
        slug = skill_dir.name
        skill_md = skill_dir / "SKILL.md"
        desc = ""
        if skill_md.exists():
            for line in skill_md.read_text().splitlines():
                if line.startswith("description:"):
                    desc = line.split(":", 1)[1].strip().strip('"').strip("'")
                    break
        file_count = sum(1 for _ in skill_dir.rglob("*") if _.is_file())
        pin_marker = " [bright_blue]🔒 (already pinned)[/]" if slug in pinned_already else ""

        while True:
            console.print(f"\n[bold cyan][{i}/{len(skills)}][/] [bold]{slug}[/]{pin_marker}")
            if desc:
                console.print(f"  [dim]{desc[:200]}[/]")
            console.print(f"  [dim]{file_count} files[/]")
            choice = Prompt.ask(
                "  Action",
                choices=["k", "d", "p", "s", "v", "q"],
                default="k",
                show_choices=False,
            )
            if choice == "v":
                # Render SKILL.md and loop back to prompt — viewing isn't a
                # decision, just an inspection step.
                if skill_md.exists():
                    console.print()
                    console.print(Markdown(skill_md.read_text()))
                continue
            if choice == "k":
                decisions.append((slug, "kept"))
                console.print("  [green]→ kept[/]")
            elif choice == "d":
                cmd_drop(argparse.Namespace(project=args.project, skill=slug))
                decisions.append((slug, "dropped"))
            elif choice == "p":
                cmd_pin(argparse.Namespace(project=args.project, skill=slug))
                decisions.append((slug, "pinned"))
            elif choice == "s":
                decisions.append((slug, "skipped"))
                console.print("  [dim]→ skipped[/]")
            elif choice == "q":
                quit_early = True
                console.print("  [yellow]→ quit; remaining skills not reviewed[/]")
            break

    # Summary + audit log
    console.print()
    summary: dict[str, int] = {}
    for _, action in decisions:
        summary[action] = summary.get(action, 0) + 1
    console.print("[bold]Summary[/]")
    for action in ("approved", "kept", "pinned",
                   "dropped", "dropped-pending",
                   "skipped", "skipped-pending"):
        if summary.get(action):
            console.print(f"  {action}: {summary[action]}")
    if quit_early:
        unreviewed = len(skills) - len(decisions)
        console.print(f"  [dim](quit early — {unreviewed} skill(s) not reviewed)[/]")

    if decisions:
        review_path = proj_dir / "review.md"
        ts = datetime.now().isoformat(timespec="seconds")
        block = [f"## review {ts}", ""]
        for slug, action in decisions:
            block.append(f"- {slug}: **{action}**")
        block.append("")
        # Append latest at the top so the most recent review is immediately visible.
        existing = review_path.read_text() if review_path.exists() else ""
        review_path.write_text("\n".join(block) + ("\n" + existing if existing else ""))
        console.print(f"\n  [dim]audit log → {review_path}[/]")
    return 0


# ─── reset ─────────────────────────────────────────────────────────────────


# Files we delete in the bundles/<project>/ dir during reset. Anything not
# in this set is preserved (so a future addition like a custom user file
# won't be silently wiped). Pins + blocklist are always opt-in via
# --wipe-all so we don't trash the user's curation intent by accident.
_RESET_BUNDLE_FILES = (
    "CLAUDE.md",
    "AGENTS.md",
    "_candidates.json",
    "_curation_log.md",
    "_index.md",
    "_run.log",
    "_cache.json",
    "review.md",
)
_RESET_BUNDLE_DIRS = (
    "skills",
    "_pending",
)
_RESET_BUNDLE_STEERING = (
    "_pinned.json",
    "_blocklist.json",
)
# state.db columns reset by `watchmen reset`. Keeps the project row itself
# (and source_repo, threshold, notes — those are user-set config that
# shouldn't be touched by re-curate) but clears all "last ran X" markers
# so the next analyst / curator cycle behaves like a fresh install.
_RESET_STATE_FIELDS = {
    "last_analyst_day": None,
    "last_analyst_run": None,
    "last_curator_run": None,
    "last_curator_skill_count": 0,
}


def _collect_reset_targets(project_key: str, wipe_all: bool) -> list:
    """Return list of (path, kind) tuples for everything `cmd_reset` will
    delete given the supplied flags. Computed up-front so --dry-run can
    print the exact same set the destructive path would touch."""
    from watchmen.util import analyses_base, bundle_dir
    targets: list = []
    bdir = bundle_dir(project_key)
    adir = analyses_base() / project_key

    # Analyses: wipe the whole directory contents (thesis snapshots +
    # _running.md). Cheaper to delete the dir than enumerate files.
    if adir.exists():
        targets.append((adir, "dir"))

    if bdir.exists():
        for fname in _RESET_BUNDLE_FILES:
            f = bdir / fname
            if f.exists():
                targets.append((f, "file"))
        for dname in _RESET_BUNDLE_DIRS:
            d = bdir / dname
            if d.exists():
                targets.append((d, "dir"))
        if wipe_all:
            for sname in _RESET_BUNDLE_STEERING:
                f = bdir / sname
                if f.exists():
                    targets.append((f, "file"))

    return targets


def cmd_reset(args) -> int:
    """Wipe a project's analyst output + curated bundle, then reset state.db
    markers so the next `watchmen learn` (or analyst/curator pair) treats
    it as a fresh install.

    By default preserves:
    - `_pinned.json` / `_blocklist.json` — your steering intent; use
      `--wipe-all` to nuke those too.
    - `corpus.db` + raw transcripts — upstream data, not a curation.
    - `state.db` project row config (`source_repo`, `threshold`, `notes`,
      `enabled`, `approval_required`, `skip_overlapping_skills`).

    Flags:
    - `--dry-run` lists what would be removed without touching anything.
    - `--yes` skips the confirmation prompt (CI / scripting).
    - `--wipe-all` also removes pins + blocklist.
    - `--then-learn` runs `watchmen learn --full` after the reset (so
      the rerun-from-scratch is one command).
    """
    from watchmen import state
    from watchmen.util import bundle_dir
    project_key = args.project

    state.init_db()
    proj = state.get_project(project_key)
    if not proj:
        print(yellow(f"'{project_key}' not tracked. Run `watchmen list` to see candidates."))
        return 1

    targets = _collect_reset_targets(project_key, wipe_all=bool(getattr(args, "wipe_all", False)))

    has_state_markers = any(proj.get(k) for k in _RESET_STATE_FIELDS)
    if not targets and not has_state_markers:
        print(green(f"✓ {project_key}: nothing to reset (no analyses, no bundle, no run history)"))
        return 0

    # Preview — same set whether dry-run or not, so the user can see
    # exactly what's about to disappear before they confirm.
    print(bold(f"\nReset plan — {project_key}\n"))
    if targets:
        for path, kind in targets:
            kind_marker = dim("dir/" if kind == "dir" else "    ")
            print(f"  {yellow('✗')} {kind_marker} {path}")
    if has_state_markers:
        marker_summary = ", ".join(k for k, _ in _RESET_STATE_FIELDS.items() if proj.get(k))
        print(f"  {yellow('↺')}      state.db markers ({marker_summary})")
    print()
    if not getattr(args, "wipe_all", False) and (bundle_dir(project_key) / "_pinned.json").exists():
        print(dim("  preserved: _pinned.json (pass --wipe-all to also remove)"))
    if not getattr(args, "wipe_all", False) and (bundle_dir(project_key) / "_blocklist.json").exists():
        print(dim("  preserved: _blocklist.json (pass --wipe-all to also remove)"))
    print()

    if getattr(args, "dry_run", False):
        print(dim("dry-run — no files removed."))
        return 0

    if not getattr(args, "yes", False):
        try:
            answer = input(f"Type {bold(project_key)} to confirm: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            print(dim("aborted."))
            return 1
        if answer != project_key:
            print(dim("aborted — project key didn't match."))
            return 1

    # Apply
    import shutil as _shutil
    deleted_files = 0
    deleted_dirs = 0
    for path, kind in targets:
        try:
            if kind == "dir":
                _shutil.rmtree(path)
                deleted_dirs += 1
            else:
                path.unlink()
                deleted_files += 1
        except OSError as e:
            print(yellow(f"  ! failed to remove {path}: {e}"))
            # Continue with the rest — partial reset is better than no reset
            # if one file is locked.

    if has_state_markers:
        state.update_project(project_key, **_RESET_STATE_FIELDS)

    print(green(f"✓ reset {project_key}: {deleted_files} file(s), {deleted_dirs} dir(s) removed"))
    if has_state_markers:
        print(dim(f"  cleared state.db markers"))

    # Optional chained re-learn — convenience for the common case of
    # "wipe and immediately re-run from scratch".
    if getattr(args, "then_learn", False):
        print()
        print(bold(f"Chaining → watchmen learn {project_key} --full"))
        print()
        # Local import to avoid a control↔pipeline cycle at module load.
        from watchmen.commands.pipeline import cmd_learn
        learn_args = argparse.Namespace(
            project=project_key,
            full=True,
            model=getattr(args, "model", None) or _default_learn_model(),
        )
        return cmd_learn(learn_args)
    return 0


def _default_learn_model() -> str:
    """Resolve the default model the same way cli.py's argparse does.
    Lazy import keeps the control.py surface free of config + providers
    imports for the non-reset commands."""
    from watchmen import config
    return config.default_model()
