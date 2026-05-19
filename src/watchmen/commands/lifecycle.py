"""Service lifecycle commands — `watchmen up` and `watchmen down`.

The flat `daemon install` / `viewer install` / `hooks install` commands work
fine when you know watchmen's three-subsystem architecture, but they're a lot
to type and remember on first install. `up` collapses them into one verb:
install hooks + daemon + viewer + auto-start, print a summary banner.

`down` is the inverse — uninstall the scheduler units and remove the hooks
settings.json entries. Crucially it does NOT touch the corpus / state / bundles
directories. Config-preserving, not data-wiping. Users who want a full wipe
can follow up with `watchmen reset` per-project.

Each piece is opt-out via `--skip-{hooks,daemon,viewer}` for the rare case
someone wants the lifecycle command to skip one subsystem (CI, remote
servers without a desktop session, etc.).
"""

from __future__ import annotations

from watchmen.ui import bold, cyan, dim, green, yellow


def cmd_up(args) -> int:
    """Install + start daemon, viewer, and hooks in one shot.

    Each subsystem failure is reported but doesn't abort the rest — partial
    setup is more useful than no setup, and the user can re-run `watchmen up`
    after fixing the failed piece. Exit code is non-zero iff anything failed.
    """
    from watchmen import config, hooks_setup, service
    from watchmen.agent import provider_banner

    skip_hooks  = bool(getattr(args, "skip_hooks", False))
    skip_daemon = bool(getattr(args, "skip_daemon", False))
    skip_viewer = bool(getattr(args, "skip_viewer", False))

    failures: list[str] = []
    print(bold("Starting watchmen…"))
    print(dim(f"  {provider_banner()}"))
    print()

    if not skip_hooks:
        print(cyan("→ hooks"))
        try:
            rc = hooks_setup.install()
            if rc != 0:
                failures.append(f"hooks ({rc})")
        except Exception as e:
            failures.append(f"hooks ({type(e).__name__}: {e})")
            print(yellow(f"  ✗ {e}"))
        print()

    if not skip_daemon:
        print(cyan("→ daemon"))
        try:
            rc = service.install_daemon(
                model=None,           # resolves to config.default_model()
                interval=7200,        # 2h default — matches existing default
                dry_run=False,
            )
            if rc != 0:
                failures.append(f"daemon ({rc})")
        except Exception as e:
            failures.append(f"daemon ({type(e).__name__}: {e})")
            print(yellow(f"  ✗ {e}"))
        print()

    if not skip_viewer:
        print(cyan("→ viewer"))
        try:
            rc = service.install_viewer(host=None, port=None, dry_run=False)
            if rc != 0:
                failures.append(f"viewer ({rc})")
        except Exception as e:
            failures.append(f"viewer ({type(e).__name__}: {e})")
            print(yellow(f"  ✗ {e}"))
        print()

    if failures:
        print(yellow(f"✗ partial install — failed: {', '.join(failures)}"))
        print(dim("  re-run `watchmen up` after fixing, or use `watchmen status` to inspect"))
        return 1

    # Success summary: condense the "what just happened" + "where to go next"
    # into one block so the user doesn't need to remember three separate URLs.
    print(green("✓ watchmen is up"))
    if not skip_viewer:
        port = config.viewer_port()
        print(dim(f"  viewer:  http://127.0.0.1:{port}"))
    if not skip_daemon:
        print(dim("  daemon:  running on 2h cycle (analyst + curator per tracked project)"))
    if not skip_hooks:
        print(dim("  hooks:   real-time corpus ingest from Claude Code + Codex"))
    print()
    print(dim("  next:  watchmen status   (inspect)"))
    print(dim("         watchmen track <project> --repo <path>   (add a project)"))
    return 0


def cmd_down(args) -> int:
    """Uninstall daemon + viewer + hooks. Leaves corpus / state / bundles alone.

    Requires explicit confirmation (`--yes`) for CI / scripting; in an
    interactive shell a single y/N prompt covers the whole teardown rather
    than asking three separate times. Better UX, harder to half-quit by
    accident.
    """
    import sys
    from watchmen import hooks_setup, service

    skip_hooks  = bool(getattr(args, "skip_hooks", False))
    skip_daemon = bool(getattr(args, "skip_daemon", False))
    skip_viewer = bool(getattr(args, "skip_viewer", False))

    if not getattr(args, "yes", False):
        if not sys.stdin.isatty():
            print(yellow("`watchmen down` needs --yes when stdin isn't a tty"))
            return 1
        targets = []
        if not skip_daemon: targets.append("daemon scheduler unit")
        if not skip_viewer: targets.append("viewer scheduler unit")
        if not skip_hooks:  targets.append("hooks settings.json entries")
        if not targets:
            print(yellow("nothing to do — all three subsystems were --skip'd"))
            return 0
        print(bold("This will uninstall:"))
        for t in targets:
            print(f"  • {t}")
        print(dim("  corpus.db, state.db, and bundles/ are preserved."))
        try:
            choice = input("Proceed? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 1
        if choice not in ("y", "yes"):
            print(dim("cancelled"))
            return 0

    failures: list[str] = []
    print()

    if not skip_daemon:
        print(cyan("→ daemon"))
        try:
            rc = service.uninstall_daemon()
            if rc != 0:
                failures.append(f"daemon ({rc})")
        except Exception as e:
            failures.append(f"daemon ({type(e).__name__}: {e})")
            print(yellow(f"  ✗ {e}"))
        print()

    if not skip_viewer:
        print(cyan("→ viewer"))
        try:
            rc = service.uninstall_viewer()
            if rc != 0:
                failures.append(f"viewer ({rc})")
        except Exception as e:
            failures.append(f"viewer ({type(e).__name__}: {e})")
            print(yellow(f"  ✗ {e}"))
        print()

    if not skip_hooks:
        print(cyan("→ hooks"))
        try:
            rc = hooks_setup.uninstall()
            if rc != 0:
                failures.append(f"hooks ({rc})")
        except Exception as e:
            failures.append(f"hooks ({type(e).__name__}: {e})")
            print(yellow(f"  ✗ {e}"))
        print()

    if failures:
        print(yellow(f"✗ partial uninstall — failed: {', '.join(failures)}"))
        return 1

    print(green("✓ watchmen is down"))
    print(dim("  corpus / state / bundles preserved — `watchmen up` brings everything back"))
    return 0
