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
