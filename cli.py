"""watchmen — orchestrator CLI for the local Claude Code session intelligence pipeline.

Subcommands:
  status                    Dashboard: tracked projects, last-run, what needs analysis
  list                      Auto-detect projects from corpus.db (>=30 prompts)
  track <key> --repo <p>    Track a project so watchmen analyze/curate operates on it
  ingest                    Re-run corpus.py (rebuild corpus.db from ~/.claude/projects)
  analyze <key>             Run analyst (incremental — only days after last_analyst_day)
  curate <key>              Run curator (--regen-claude for stage 3 only)
  runs [--project <key>]    Recent run history
  config                    Open config in $EDITOR (placeholder for now)
  viewer                    Start local web viewer (placeholder for P2)

Designed to be invoked as `uv run watchmen <subcommand>` or via the script entry in pyproject.toml.
"""

import argparse
import subprocess
import sys
from pathlib import Path

import state

ROOT = Path(__file__).parent
DEFAULT_MODEL = "deepseek/deepseek-v4-flash"


# ─── Helpers ────────────────────────────────────────────────────────────────


def _bold(s: str) -> str:
    return f"\033[1m{s}\033[0m"


def _dim(s: str) -> str:
    return f"\033[90m{s}\033[0m"


def _green(s: str) -> str:
    return f"\033[32m{s}\033[0m"


def _yellow(s: str) -> str:
    return f"\033[33m{s}\033[0m"


# ─── Subcommands ────────────────────────────────────────────────────────────


def cmd_status(args) -> int:
    state.init_db()
    tracked = state.list_projects()
    print(_bold("\nwatchmen status\n"))
    if not tracked:
        print("No projects tracked yet. Run:")
        print(_dim("  uv run watchmen list             # see auto-detected projects"))
        print(_dim("  uv run watchmen track <key> --repo <abs-path>"))
        return 0

    print(f"  {'project':<30} {'state':<10} {'last analyst':<22} {'new prompts':>11}  notes")
    print(_dim("  " + "─" * 100))
    for p in tracked:
        progress = state.get_project_progress(p["project_key"])
        last_day = p["last_analyst_day"] or "—"
        new_n = progress.get("new_prompts_since_last_analysis", "?")
        st = "enabled" if p["enabled"] else "paused"
        flag = ""
        if progress.get("needs_analysis"):
            flag = _yellow("● needs analysis")
        elif p["last_analyst_day"]:
            flag = _green("● up to date")
        print(f"  {p['project_key'][:30]:<30} {st:<10} {last_day:<22} {str(new_n):>11}  {flag}")

    print()
    runs = state.recent_runs(limit=5)
    if runs:
        print(_bold("Recent runs:"))
        for r in runs:
            t = r["started_at"][:19]
            status = r["status"]
            color = _green if status == "ok" else _yellow if status == "running" else _dim
            print(f"  {t}  {r['project_key']:<25} {r['kind']:<22} {color(status)}")
    return 0


def cmd_list(args) -> int:
    detected = state.auto_detect_projects()
    if not detected:
        print("No projects detected — run `watchmen ingest` to populate corpus.db.")
        return 0
    print(_bold(f"\nDetected {len(detected)} projects with ≥30 prompts:\n"))
    print(f"  {'project_key':<32} {'prompts':>8} {'sessions':>9}  repo")
    print(_dim("  " + "─" * 100))
    tracked_keys = {p["project_key"] for p in state.list_projects()}
    for d in detected:
        marker = _green("✓") if d["project_key"] in tracked_keys else " "
        repo_short = d["source_repo"].replace(str(Path.home()), "~", 1)
        print(f"{marker} {d['project_key']:<32} {d['prompts']:>8} {d['sessions']:>9}  {repo_short}")
    print()
    print(_dim("Tracked projects show ✓ — track new ones with `watchmen track <key> --repo <path>`."))
    return 0


def cmd_track(args) -> int:
    state.init_db()
    repo = Path(args.repo).expanduser().resolve()
    if not repo.exists():
        print(f"ERROR: source repo does not exist: {repo}")
        return 1
    state.track_project(args.project, str(repo), threshold=args.threshold)
    print(_green(f"Tracking {args.project} → {repo}"))
    # Bootstrap from disk if analyses already exist
    summary = state.sync_from_disk(args.project)
    if summary.get("analyst"):
        print(_dim(f"  synced analyst state — last day {summary['analyst']['last_day']}, {summary['analyst']['files']} day-files"))
    if summary.get("curator"):
        print(_dim(f"  synced curator state — {summary['curator']['skill_count']} skills"))
    return 0


def cmd_sync(args) -> int:
    state.init_db()
    targets = [args.project] if args.project else [p["project_key"] for p in state.list_projects()]
    for key in targets:
        summary = state.sync_from_disk(key)
        a = summary.get("analyst")
        c = summary.get("curator")
        bits = []
        if a: bits.append(f"analyst:last_day={a['last_day']}")
        if c: bits.append(f"curator:{c['skill_count']} skills")
        print(f"  {key:<30}  {'  '.join(bits) if bits else _dim('(no artifacts on disk)')}")
    return 0


def cmd_ingest(args) -> int:
    print(_dim("Running corpus.py scan..."))
    r = subprocess.run([sys.executable, str(ROOT / "corpus.py"), "scan"], cwd=str(ROOT))
    return r.returncode


def cmd_analyze(args) -> int:
    state.init_db()
    proj = state.get_project(args.project)
    if not proj and not args.repo:
        print(f"ERROR: project '{args.project}' not tracked. Run `watchmen track {args.project} --repo <path>` first, or pass --repo.")
        return 1

    progress = state.get_project_progress(args.project)
    if progress.get("error"):
        print(f"ERROR: {progress['error']}")
        return 1

    # Decide whether to run
    if args.full:
        from_day = None  # full re-run
    else:
        from_day = progress.get("last_analyst_day")
        if from_day and progress["new_prompts_since_last_analysis"] == 0:
            print(_green(f"{args.project}: already up to date (last analyst day: {from_day})"))
            return 0

    cmd = [sys.executable, str(ROOT / "analyze.py"), "-p", args.project, "--model", args.model]
    if from_day:
        cmd.extend(["--from-day", from_day])

    run_id = state.start_run(args.project, "analyst", notes=f"from_day={from_day}")
    print(_dim(f"Running: {' '.join(cmd)}"))
    r = subprocess.run(cmd, cwd=str(ROOT))
    if r.returncode == 0:
        # Update last_analyst_day from the latest day in analyses/
        analyses_dir = ROOT / "analyses" / args.project
        if analyses_dir.exists():
            day_files = sorted(p.stem for p in analyses_dir.glob("20*.md"))
            if day_files:
                state.update_project(args.project, last_analyst_day=day_files[-1], last_analyst_run=state.now_iso())
        state.finish_run(run_id, "ok")
        print(_green(f"\n{args.project}: analyst run completed."))
    else:
        state.finish_run(run_id, "failed", notes=f"exit code {r.returncode}")
        print(_yellow(f"\n{args.project}: analyst run failed (exit {r.returncode})."))
    return r.returncode


def cmd_curate(args) -> int:
    state.init_db()
    proj = state.get_project(args.project)
    if not proj:
        print(f"ERROR: project '{args.project}' not tracked.")
        return 1

    cmd = [sys.executable, str(ROOT / "curate.py"),
           "--project", args.project, "--repo", proj["source_repo"], "--model", args.model]
    if args.regen_claude:
        cmd.extend(["--skip-finder", "--skip-skills"])

    kind = "curator-claude-only" if args.regen_claude else "curator-full"
    run_id = state.start_run(args.project, kind)
    print(_dim(f"Running: {' '.join(cmd)}"))
    r = subprocess.run(cmd, cwd=str(ROOT))
    if r.returncode == 0:
        skills_dir = ROOT / "kai_claude" / args.project / "skills"
        skill_count = sum(1 for d in skills_dir.iterdir() if d.is_dir()) if skills_dir.exists() else 0
        state.update_project(args.project, last_curator_run=state.now_iso(), last_curator_skill_count=skill_count)
        state.finish_run(run_id, "ok", notes=f"{skill_count} skills")
        print(_green(f"\n{args.project}: curator run completed ({skill_count} skills)."))
    else:
        state.finish_run(run_id, "failed", notes=f"exit code {r.returncode}")
    return r.returncode


def cmd_runs(args) -> int:
    state.init_db()
    runs = state.recent_runs(limit=args.limit, project_key=args.project)
    if not runs:
        print("No runs recorded yet.")
        return 0
    print(_bold(f"\nRecent runs (limit {args.limit}):\n"))
    for r in runs:
        t = (r["started_at"] or "?")[:19]
        end = (r["ended_at"] or "running")[:19] if r["ended_at"] else "running"
        status = r["status"]
        color = _green if status == "ok" else _yellow if status == "running" else _dim
        print(f"  {t}  {r['project_key']:<25} {r['kind']:<22} {color(status):<14}  {r['notes'] or ''}")
    return 0


def cmd_config(args) -> int:
    print(_dim("config command — placeholder for P3 (will edit ~/.config/watchmen/config.yaml)"))
    return 0


def cmd_viewer(args) -> int:
    state.init_db()
    from viewer.server import serve
    serve(host=args.host, port=args.port)
    return 0


def cmd_daemon(args) -> int:
    import daemon as _daemon
    return _daemon.run(args)


def cmd_install_daemon(args) -> int:
    import launchd_setup
    return launchd_setup.install_daemon(model=args.model, interval=args.interval, dry_run=args.dry_run)


def cmd_install_viewer(args) -> int:
    import launchd_setup
    return launchd_setup.install_viewer(host=args.host, port=args.port, dry_run=args.dry_run)


def cmd_uninstall_daemon(args) -> int:
    import launchd_setup
    return launchd_setup.uninstall_daemon()


def cmd_uninstall_viewer(args) -> int:
    import launchd_setup
    return launchd_setup.uninstall_viewer()


def cmd_launchd_status(args) -> int:
    import launchd_setup
    return launchd_setup.status()


def cmd_install_hooks(args) -> int:
    import hooks_setup
    return hooks_setup.install()


def cmd_uninstall_hooks(args) -> int:
    import hooks_setup
    return hooks_setup.uninstall()


def cmd_hooks_status(args) -> int:
    import hooks_setup
    return hooks_setup.status()


def cmd_update_plugin(args) -> int:
    import plugin_setup
    return plugin_setup.update_marketplace()


def cmd_install_statusline(args) -> int:
    import plugin_setup
    return plugin_setup.install_statusline(force=args.force)


def cmd_uninstall_statusline(args) -> int:
    import plugin_setup
    return plugin_setup.uninstall_statusline()


def cmd_plugin_status(args) -> int:
    import plugin_setup
    return plugin_setup.status()


# ─── Argument parsing ───────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="watchmen", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("status", help="dashboard view").set_defaults(func=cmd_status)
    sub.add_parser("list", help="auto-detect projects from corpus").set_defaults(func=cmd_list)

    p_track = sub.add_parser("track", help="add a project to tracking")
    p_track.add_argument("project", help="project key (used to filter corpus by project_dir substring)")
    p_track.add_argument("--repo", required=True, help="absolute path to source repo on disk")
    p_track.add_argument("--threshold", type=int, default=30, help="min new prompts to trigger run")
    p_track.set_defaults(func=cmd_track)

    sub.add_parser("ingest", help="re-scan ~/.claude/projects into corpus.db").set_defaults(func=cmd_ingest)

    p_sync = sub.add_parser("sync", help="bootstrap state from existing analyses/ + kai_claude/ on disk")
    p_sync.add_argument("--project", help="just one project (default: all tracked)")
    p_sync.set_defaults(func=cmd_sync)

    p_an = sub.add_parser("analyze", help="run analyst (incremental by default)")
    p_an.add_argument("project")
    p_an.add_argument("--full", action="store_true", help="full re-run (ignore prior thesis)")
    p_an.add_argument("--repo", help="override repo path (only needed if not tracked)")
    p_an.add_argument("--model", default=DEFAULT_MODEL)
    p_an.set_defaults(func=cmd_analyze)

    p_cu = sub.add_parser("curate", help="run curator (skill bundles + CLAUDE.md)")
    p_cu.add_argument("project")
    p_cu.add_argument("--regen-claude", action="store_true", help="rerun stage 3 only (use existing skills)")
    p_cu.add_argument("--model", default=DEFAULT_MODEL)
    p_cu.set_defaults(func=cmd_curate)

    p_runs = sub.add_parser("runs", help="recent run history")
    p_runs.add_argument("--project", help="filter by project_key")
    p_runs.add_argument("--limit", type=int, default=20)
    p_runs.set_defaults(func=cmd_runs)

    sub.add_parser("config", help="edit config (P3)").set_defaults(func=cmd_config)
    p_view = sub.add_parser("viewer", help="start local web viewer at http://127.0.0.1:8888")
    p_view.add_argument("--host", default="127.0.0.1")
    p_view.add_argument("--port", type=int, default=8888)
    p_view.set_defaults(func=cmd_viewer)

    p_d = sub.add_parser("daemon", help="run scheduling loop (foreground; use install-daemon for autostart)")
    p_d.add_argument("--once", action="store_true", help="single cycle then exit")
    p_d.add_argument("--interval", type=int, default=7200, help="seconds between analyst cycles (default 7200 = 2h)")
    p_d.add_argument("--curator-age", type=int, default=86400)
    p_d.add_argument("--curator-hours", default="2,14", help="local-time hours when full curator runs (default '2,14' = 2am + 2pm)")
    p_d.add_argument("--full-curator-min-age", type=int, default=28800, help="min seconds between full curator runs per project (default 8h)")
    p_d.add_argument("--model", default=DEFAULT_MODEL)
    p_d.add_argument("--log-file", default=str(Path.home() / "Library" / "Logs" / "watchmen.log"))
    p_d.set_defaults(func=cmd_daemon)

    p_id = sub.add_parser("install-daemon", help="install launchd agent for watchmen daemon (autostart on login)")
    p_id.add_argument("--model", default=DEFAULT_MODEL)
    p_id.add_argument("--interval", type=int, default=7200, help="seconds between analyst cycles (default 7200 = 2h)")
    p_id.add_argument("--dry-run", action="store_true", help="print plist without installing")
    p_id.set_defaults(func=cmd_install_daemon)

    p_iv = sub.add_parser("install-viewer", help="install launchd agent for watchmen viewer (autostart on login)")
    p_iv.add_argument("--host", default="127.0.0.1")
    p_iv.add_argument("--port", type=int, default=8888)
    p_iv.add_argument("--dry-run", action="store_true")
    p_iv.set_defaults(func=cmd_install_viewer)

    sub.add_parser("uninstall-daemon", help="remove the watchmen daemon launchd agent").set_defaults(func=cmd_uninstall_daemon)
    sub.add_parser("uninstall-viewer", help="remove the watchmen viewer launchd agent").set_defaults(func=cmd_uninstall_viewer)
    sub.add_parser("launchd-status", help="show installed/loaded launchd agents").set_defaults(func=cmd_launchd_status)

    sub.add_parser("install-hooks", help="wire watchmen_observe.sh into ~/.claude/settings.json").set_defaults(func=cmd_install_hooks)
    sub.add_parser("uninstall-hooks", help="remove watchmen entries from ~/.claude/settings.json").set_defaults(func=cmd_uninstall_hooks)
    sub.add_parser("hooks-status", help="show which hook events are wired up").set_defaults(func=cmd_hooks_status)

    sub.add_parser("update-plugin", help="git pull the marketplace clone so /plugin install picks up the latest").set_defaults(func=cmd_update_plugin)
    p_isl = sub.add_parser("install-statusline", help="wire the 💡 watchmen indicator into ~/.claude/settings.json")
    p_isl.add_argument("--force", action="store_true", help="overwrite a non-watchmen statusLine entry")
    p_isl.set_defaults(func=cmd_install_statusline)
    sub.add_parser("uninstall-statusline", help="remove the watchmen statusLine entry").set_defaults(func=cmd_uninstall_statusline)
    sub.add_parser("plugin-status", help="show plugin marketplace + cache + statusLine state").set_defaults(func=cmd_plugin_status)

    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
