"""watchmen — orchestrator CLI for the local Claude Code session intelligence pipeline.

Subcommands:
  status                    Dashboard: tracked projects, last-run, what needs analysis
  list                      Auto-detect projects from corpus.db (>=30 prompts)
  track <key> --repo <p>    Track a project so watchmen analyze/curate operates on it
  ingest                    Re-run corpus.py (rebuild corpus.db from all agents)
  analyze <key>             Run analyst (incremental — only days after last_analyst_day)
  curate <key>              Run curator (--regen-claude for stage 3 only)
  runs [--project <key>]    Recent run history
  onboard                   Interactive setup wizard (fresh install)
  reonboard                 Re-run the wizard (existing projects survive, new ones added)
  settings list|show|set    View / update per-project settings (threshold, enabled, repo, notes)
  daemon run|install|uninstall      Foreground run / launchd agent lifecycle
  viewer run|install|uninstall      Foreground run / launchd agent lifecycle (default :8979)
  hooks install|uninstall|status    Claude Code hook lifecycle + inspection
  statusline install|uninstall      💡 watchmen indicator in ~/.claude/settings.json
  plugin update|status              Marketplace clone management
  launchd status                    Inspect installed launchd agents

Old verb-noun-hyphen forms (install-daemon, hooks-status, …) still work but
print a soft deprecation hint to stderr — will be removed in a future release.

Designed to be invoked as `uv run watchmen <subcommand>` or via the script entry in pyproject.toml.
"""

import argparse
import subprocess
import sys
from pathlib import Path

import config
import state

ROOT = Path(__file__).parent
DEFAULT_MODEL = "deepseek/deepseek-v4-flash"
VIEWER_DEFAULT_HOST = config.VIEWER_DEFAULT_HOST
VIEWER_DEFAULT_PORT = config.VIEWER_DEFAULT_PORT


def _version() -> str:
    """Read version from package metadata if installed, else parse pyproject.toml."""
    try:
        from importlib.metadata import version as _v
        return _v("watchmen")
    except Exception:
        pass
    try:
        import tomllib  # 3.11+
        with (ROOT / "pyproject.toml").open("rb") as fh:
            return tomllib.load(fh)["project"]["version"]
    except Exception:
        return "0.0.0"


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


def cmd_onboard(args) -> int:
    import onboard
    return onboard.run()


def cmd_reonboard(args) -> int:
    """Re-run the onboarding wizard. Same code path as `onboard` — onboard.run()
    is already idempotent (existing projects show up tracked, get refreshed)."""
    import onboard
    print(_dim("Re-running onboarding wizard. Tracked projects survive — new ones are added."))
    return onboard.run()


# ─── Settings ───────────────────────────────────────────────────────────────


_SETTABLE_KEYS = ("enabled", "threshold", "repo", "notes")


def _parse_setting(key: str, value: str) -> tuple[str, object]:
    """Map a CLI-friendly key + value to (db_column, coerced_value).
    Raises ValueError with a human-readable message on bad input."""
    if key == "enabled":
        v = value.strip().lower()
        if v in ("true", "yes", "y", "on", "1"):
            return "enabled", 1
        if v in ("false", "no", "n", "off", "0"):
            return "enabled", 0
        raise ValueError(f"enabled must be true/false (got {value!r})")
    if key == "threshold":
        try:
            n = int(value)
        except ValueError:
            raise ValueError(f"threshold must be an integer (got {value!r})") from None
        if n < 1:
            raise ValueError("threshold must be ≥ 1")
        return "threshold_new_prompts", n
    if key == "repo":
        path = Path(value).expanduser().resolve()
        if not path.exists() or not path.is_dir():
            raise ValueError(f"not a directory: {path}")
        return "source_repo", str(path)
    if key == "notes":
        return "notes", value
    raise ValueError(f"unknown setting {key!r}. valid: {', '.join(_SETTABLE_KEYS)}")


def cmd_settings_list(args) -> int:
    state.init_db()
    rows = state.list_projects()
    if not rows:
        print(_dim("No projects tracked yet. Run `watchmen onboard` or `watchmen track <key> --repo <path>`."))
        return 0
    print(_bold(f"\n{len(rows)} tracked project(s):\n"))
    print(f"  {'project':<30} {'state':<8} {'threshold':>9}  {'repo'}")
    print(_dim("  " + "─" * 90))
    for p in rows:
        st = "enabled" if p["enabled"] else _yellow("paused")
        repo = (p["source_repo"] or "").replace(str(Path.home()), "~", 1)
        print(f"  {p['project_key'][:30]:<30} {st:<8} {p['threshold_new_prompts']:>9}  {repo}")
    return 0


def cmd_settings_show(args) -> int:
    state.init_db()
    p = state.get_project(args.project)
    if not p:
        print(f"ERROR: project '{args.project}' not tracked. Run `watchmen list` to see candidates.")
        return 1
    print(_bold(f"\n{args.project}\n"))
    for k in ("source_repo", "enabled", "threshold_new_prompts", "notes",
              "last_analyst_day", "last_analyst_run",
              "last_curator_run", "last_curator_skill_count",
              "created_at", "updated_at"):
        v = p.get(k)
        if isinstance(v, int) and k == "enabled":
            v = "true" if v else "false"
        print(f"  {k:<28}  {v if v is not None else _dim('(unset)')}")
    return 0


def cmd_settings_set(args) -> int:
    state.init_db()
    if not state.get_project(args.project):
        print(f"ERROR: project '{args.project}' not tracked.")
        return 1
    try:
        column, value = _parse_setting(args.key, args.value)
    except ValueError as ex:
        print(f"ERROR: {ex}")
        return 1
    state.update_project(args.project, **{column: value})
    print(_green(f"✓ {args.project}: {args.key} = {value}"))
    return 0


def _check_openrouter_key(key: str) -> tuple[bool, str]:
    """Probe OpenRouter's /auth/key endpoint with the given key. Returns
    (ok, human_message). Used by `watchmen settings api-key [--check]` to
    surface bad keys BEFORE they reach the analyst/curator and turn into
    silent 401s halfway through a run."""
    import httpx
    try:
        r = httpx.get(
            "https://openrouter.ai/api/v1/auth/key",
            headers={"Authorization": f"Bearer {key}"},
            timeout=10.0,
        )
    except httpx.RequestError as e:
        return False, f"connection error: {type(e).__name__}"
    if r.status_code == 200:
        try:
            info = (r.json() or {}).get("data") or {}
        except ValueError:
            info = {}
        usage = info.get("usage")
        limit = info.get("limit")
        if usage is not None and limit is not None and limit > 0:
            return True, f"valid — credits used ${float(usage):.2f} of ${float(limit):.2f}"
        if usage is not None and limit is None:
            return True, f"valid — credits used ${float(usage):.2f} (no hard limit)"
        return True, "valid"
    if r.status_code == 401:
        try:
            msg = (r.json().get("error") or {}).get("message", "")
        except (ValueError, AttributeError):
            msg = ""
        return False, f"401 — {msg or 'unauthorized'}"
    return False, f"HTTP {r.status_code} — {r.text[:120]}"


def _read_current_api_key() -> str | None:
    """Return the OpenRouter API key from env or ~/.config/watchmen/.env."""
    return config.read_env_var("OPENROUTER_API_KEY")


def _write_api_key(key: str) -> Path:
    """Persist the OpenRouter key to ~/.config/watchmen/.env, preserving unrelated lines."""
    return config.write_env_var("OPENROUTER_API_KEY", key)


def cmd_settings_port(args) -> int:
    """Get or set the viewer port. Writes to ~/.config/watchmen/.env so the
    setting survives restarts and is honored by every layer (CLI defaults,
    onboard, curator-generated viewer URLs)."""
    from rich.console import Console
    console = Console()

    if args.value is None:
        current = config.viewer_port()
        source = "default" if current == config.VIEWER_DEFAULT_PORT and not config.read_env_var("WATCHMEN_VIEWER_PORT") else "config"
        console.print(f"viewer port: [bold]{current}[/] [dim]({source})[/]")
        if source == "default":
            console.print(f"  [dim]set with: watchmen settings port <N>[/]")
        return 0

    try:
        port = int(args.value)
    except ValueError:
        console.print(f"[red]✗[/] port must be an integer (got {args.value!r})")
        return 1
    if not (1024 <= port <= 65535):
        console.print(f"[red]✗[/] port must be in 1024–65535")
        return 1

    path = config.write_env_var("WATCHMEN_VIEWER_PORT", str(port))
    console.print(f"[green]✓[/] viewer port set to [bold]{port}[/]")
    console.print(f"  [dim]wrote → {path}[/]")
    # The launchd plist is baked at install time — port changes don't propagate
    # until reinstall. Make the next step obvious.
    try:
        import launchd_setup
        if launchd_setup._is_loaded(launchd_setup.VIEWER_LABEL):
            console.print(f"  [yellow]![/] viewer launchd agent is running on its old port — run [bold]watchmen viewer install[/] to move it")
    except Exception:
        pass
    return 0


def cmd_settings_api_key(args) -> int:
    """Set or check the OpenRouter API key. Live-validates against OpenRouter's
    /auth/key endpoint so a bad key gets caught BEFORE the analyst/curator
    silently 401s halfway through a run."""
    from rich.console import Console
    from rich.prompt import Confirm, Prompt
    console = Console()

    current = _read_current_api_key()
    if current:
        ok, info = _check_openrouter_key(current)
        marker = "[green]✓[/]" if ok else "[red]✗[/]"
        console.print(f"{marker} current key: {info}  [dim]({current[:8]}…{current[-4:]})[/]")
    else:
        console.print("[dim]no key currently set[/]")

    if args.check:
        return 0 if (current and ok) else 1

    if args.set:
        new_key = args.set.strip()
    else:
        console.print()
        new_key = Prompt.ask(
            "Paste new OpenRouter API key (enter to keep current)",
            password=True, default="", show_default=False,
        ).strip()
    if not new_key:
        console.print("[dim]no change.[/]")
        return 0

    ok, info = _check_openrouter_key(new_key)
    if ok:
        path = _write_api_key(new_key)
        console.print(f"[green]✓[/] {info}")
        console.print(f"  wrote → {path} [dim](chmod 600)[/]")
        return 0
    console.print(f"[red]✗[/] new key rejected: {info}")
    if not Confirm.ask("Save anyway?", default=False):
        return 1
    path = _write_api_key(new_key)
    console.print(f"[yellow]![/] saved despite rejection → {path}")
    return 0


def cmd_doctor(args) -> int:
    """Health check: API key, OpenRouter reachability, corpus, tracked projects,
    daemon/viewer state, hooks, latest run age, disk free.

    Used to self-diagnose a broken install — single screen of ✓/✗ rows. Returns
    0 if everything is green, 1 if any required check fails."""
    from rich.console import Console
    from rich.table import Table
    console = Console()

    fails = 0
    warns = 0
    table = Table(title="watchmen doctor", show_header=True, header_style="bold cyan", expand=False)
    table.add_column("check", style="bold")
    table.add_column("status", justify="center", width=4)
    table.add_column("detail")

    def row(label: str, ok: bool, detail: str, severity: str = "fail") -> None:
        nonlocal fails, warns
        if ok:
            mark = "[green]✓[/]"
        elif severity == "warn":
            mark = "[yellow]![/]"
            warns += 1
        else:
            mark = "[red]✗[/]"
            fails += 1
        table.add_row(label, mark, detail)

    # 1. OpenRouter API key
    current = _read_current_api_key()
    if not current:
        row("OpenRouter key", False, "not set — run `watchmen settings api-key`")
    else:
        ok, info = _check_openrouter_key(current)
        row("OpenRouter key", ok, info)

    # 2. corpus.db
    corpus_db = ROOT / "corpus.db"
    if not corpus_db.exists():
        row("corpus.db", False, "missing — run `watchmen ingest`")
    else:
        import sqlite3
        cc = sqlite3.connect(corpus_db)
        n_sessions = cc.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        n_prompts = cc.execute("SELECT COUNT(*) FROM prompts").fetchone()[0]
        cc.close()
        if n_sessions == 0:
            row("corpus.db", False, "no sessions ingested yet — run `watchmen ingest`")
        else:
            row("corpus.db", True, f"{n_sessions:,} sessions / {n_prompts:,} prompts")

    # 3. tracked projects
    state.init_db()
    projects = state.list_projects()
    if not projects:
        row("tracked projects", False, "0 — run `watchmen init` or `watchmen track`")
    else:
        row("tracked projects", True, f"{len(projects)} project(s)")

    # 4. daemon launchd state
    try:
        import launchd_setup
        daemon_loaded = launchd_setup._is_loaded(launchd_setup.DAEMON_LABEL)
        viewer_loaded = launchd_setup._is_loaded(launchd_setup.VIEWER_LABEL)
    except Exception:
        daemon_loaded = viewer_loaded = False
    row("daemon (launchd)", daemon_loaded, "loaded" if daemon_loaded else "not loaded — `watchmen daemon install`", severity="warn")

    # 5. viewer responding
    try:
        import httpx
        r = httpx.get(f"http://{VIEWER_DEFAULT_HOST}:{VIEWER_DEFAULT_PORT}/", timeout=2.0)
        viewer_up = r.status_code < 500
        viewer_detail = f"http://{VIEWER_DEFAULT_HOST}:{VIEWER_DEFAULT_PORT}/ → {r.status_code}"
    except Exception as e:
        viewer_up = False
        viewer_detail = f"not responding ({type(e).__name__}) — `watchmen viewer install`"
    row("viewer", viewer_up, viewer_detail, severity="warn")

    # 6. hooks installed
    try:
        import hooks_setup, json as _json
        settings = _json.loads(hooks_setup.SETTINGS_FILE.read_text()) if hooks_setup.SETTINGS_FILE.exists() else {}
        wired = sum(
            1 for entries in (settings.get("hooks") or {}).values()
            for e in entries
            for h in e.get("hooks") or []
            if "watchmen" in (h.get("command") or "")
        )
        row("Claude Code hooks", wired > 0, f"{wired} watchmen entries wired" if wired else "not wired — `watchmen hooks install`", severity="warn")
    except Exception as e:
        row("Claude Code hooks", False, f"could not read settings.json ({type(e).__name__})", severity="warn")

    # 7. latest run age
    runs = state.recent_runs(limit=1)
    if runs:
        last = runs[0]
        from datetime import datetime, timezone
        try:
            t = datetime.fromisoformat(last["started_at"])
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            age = datetime.now(timezone.utc) - t
            hours = age.total_seconds() / 3600
            age_str = f"{hours:.1f}h ago" if hours < 48 else f"{age.days}d ago"
            row("latest run", True, f"{last['kind']} for {last['project_key']} — {age_str} ({last['status']})")
        except Exception:
            row("latest run", True, f"{last['kind']} for {last['project_key']}")
    else:
        row("latest run", False, "no runs recorded yet", severity="warn")

    # 8. disk free
    import shutil
    free = shutil.disk_usage(ROOT).free
    free_gb = free / 1024**3
    row("disk free (cwd)", free_gb > 1.0, f"{free_gb:.1f} GiB")

    console.print()
    console.print(table)
    if fails == 0 and warns == 0:
        console.print("\n[green]all green.[/]")
    elif fails == 0:
        console.print(f"\n[yellow]{warns} warning(s)[/] — non-blocking; fix when convenient.")
    else:
        console.print(f"\n[red]{fails} failure(s)[/] / [yellow]{warns} warning(s)[/].")
    return 1 if fails else 0


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
        print(_yellow(f"warning: viewer at {base} isn't responding — start with `watchmen viewer run` or `watchmen viewer install`"))

    print(f"opening {url}")
    opened = webbrowser.open(url, new=2)
    if not opened:
        print(_dim("(browser didn't auto-open — copy the URL above)"))
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
        print(_yellow(f"no logs found for '{args.name}' — has the service been started?"))
        print(_dim(f"expected at: {log_dir}/watchmen.*"))
        return 1
    print(_dim(f"# tailing {len(existing)} file(s): {' '.join(existing)}"), flush=True)
    flags = ["-F", "-n", str(args.lines)] if args.follow else ["-n", str(args.lines)]
    try:
        return subprocess.run(["tail", *flags, *existing]).returncode
    except KeyboardInterrupt:
        return 0


def cmd_init(args) -> int:
    """Alias for onboard — `init` is the discoverable name; `onboard` kept as a
    hidden alias for muscle memory."""
    import onboard
    return onboard.run()


def cmd_metrics(args) -> int:
    import metrics as _metrics
    from rich.console import Console
    from rich.table import Table

    rows = _metrics.daily_metrics(args.project, days=args.days)
    if not rows:
        print(f"No data for project '{args.project}'. Run `watchmen ingest` first?")
        return 1
    last7 = _metrics.summarize_window(rows, min(7, args.days))
    last30 = _metrics.summarize_window(rows, args.days)
    console = Console()
    rollup = Table(title=f"{args.project} — {args.days}-day rollup", show_header=True, header_style="bold magenta")
    rollup.add_column("Metric")
    rollup.add_column("7d", justify="right")
    rollup.add_column(f"{args.days}d", justify="right")
    rollup.add_row("Sessions", str(last7["sessions"]), str(last30["sessions"]))
    rollup.add_row("Prompts", str(last7["prompts"]), str(last30["prompts"]))
    rollup.add_row("Tool errors", str(last7["tool_errors"]), str(last30["tool_errors"]))
    rollup.add_row("Input tokens", f"{last7['input_tokens']:,}", f"{last30['input_tokens']:,}")
    rollup.add_row("Output tokens", f"{last7['output_tokens']:,}", f"{last30['output_tokens']:,}")
    rollup.add_row("Cost (USD)", f"${last7['cost_usd']:.2f}", f"${last30['cost_usd']:.2f}")
    rollup.add_row("Suggestions fired", str(last7["suggestions_fired"]), str(last30["suggestions_fired"]))
    rollup.add_row("Uptake", str(last7["uptake"]), str(last30["uptake"]))
    console.print(rollup)
    console.print(f"\n  full daily breakdown: {config.viewer_base_url()}/p/{args.project}/metrics")
    return 0


# ─── Argument parsing ───────────────────────────────────────────────────────


def _deprecate(new_form: str, fn):
    """Wrap a subcommand handler so it prints a soft deprecation hint to stderr
    before delegating. Exit code is unchanged — callers don't break.

    Why dual-form: noun-verb (`daemon install`) is more discoverable + tab-
    completion-friendly than verb-noun-with-hyphens (`install-daemon`), but
    every teammate's scripts + launchd plists use the old form. We keep both
    working and nudge usage toward the new shape via this line."""
    def wrapper(args):
        sys.stderr.write(f"\033[90m[deprecated, use 'watchmen {new_form}']\033[0m\n")
        return fn(args)
    return wrapper


def _add_daemon_run_args(p) -> None:
    """Foreground-daemon args. Same set for old `watchmen daemon` and new
    `watchmen daemon run`."""
    p.add_argument("--once", action="store_true", help="single cycle then exit")
    p.add_argument("--interval", type=int, default=7200, help="seconds between analyst cycles (default 7200 = 2h)")
    p.add_argument("--curator-age", type=int, default=86400)
    p.add_argument("--curator-hours", default="2,14", help="local-time hours when full curator runs (default '2,14' = 2am + 2pm)")
    p.add_argument("--full-curator-min-age", type=int, default=28800, help="min seconds between full curator runs per project (default 8h)")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--log-file", default=str(Path.home() / "Library" / "Logs" / "watchmen.log"))


def _add_daemon_install_args(p) -> None:
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--interval", type=int, default=7200, help="seconds between analyst cycles (default 7200 = 2h)")
    p.add_argument("--dry-run", action="store_true", help="print plist without installing")


def _add_viewer_run_args(p) -> None:
    p.add_argument("--host", default=config.VIEWER_DEFAULT_HOST)
    p.add_argument("--port", type=int, default=config.viewer_port())


def _add_viewer_install_args(p) -> None:
    p.add_argument("--host", default=config.VIEWER_DEFAULT_HOST)
    p.add_argument("--port", type=int, default=config.viewer_port())
    p.add_argument("--dry-run", action="store_true")


def _add_statusline_install_args(p) -> None:
    p.add_argument("--force", action="store_true", help="overwrite a non-watchmen statusLine entry")


# Subcommand groupings rendered by _print_grouped_help. Order = display order.
# Each entry: (subcommand, one-line description). Hidden aliases are NOT listed.
_HELP_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    ("Get started", [
        ("init",       "5-minute interactive setup wizard"),
        ("doctor",     "diagnose your install — API key, corpus, services"),
        ("settings",   "view / update OpenRouter key + per-project settings"),
    ]),
    ("Pipeline", [
        ("status",     "dashboard: tracked projects + last-run summary"),
        ("analyze",    "run analyst on a project (incremental by default)"),
        ("curate",     "run curator (skill bundles + CLAUDE.md)"),
        ("runs",       "recent run history"),
        ("metrics",    "daily token / cost / uptake rollup for a project"),
    ]),
    ("Project inventory", [
        ("list",       "auto-detect projects from corpus"),
        ("track",      "add a project to tracking"),
        ("ingest",     "re-scan ~/.claude/projects into corpus.db"),
        ("sync",       "bootstrap state from existing artifacts on disk"),
    ]),
    ("Background services", [
        ("daemon",     "run / install / uninstall the scheduling daemon"),
        ("viewer",     "run / install / uninstall the local web viewer"),
        ("hooks",      "install / uninstall / inspect Claude Code hooks"),
        ("statusline", "install / uninstall the 💡 watchmen indicator"),
        ("plugin",     "manage the Claude Code plugin marketplace clone"),
        ("launchd",    "inspect installed launchd agents"),
    ]),
    ("Inspect", [
        ("open",       "open the viewer in your browser"),
        ("logs",       "tail launchd logs (daemon | viewer | all)"),
    ]),
]


def _print_grouped_help(parser: argparse.ArgumentParser) -> None:
    """Custom help renderer that groups subcommands into sections.

    Argparse can't group subparsers natively — its --help renders a flat list
    of choices that reads like an unsorted soup. We render our own help block
    while leaving argparse parsing alone."""
    print(f"watchmen v{_version()} — local Claude Code session intelligence\n")
    print("usage: watchmen [--version] <command> [...]\n")
    for group_name, commands in _HELP_GROUPS:
        print(_bold(f"{group_name}:"))
        for cmd, desc in commands:
            print(f"  {cmd:<12}  {desc}")
        print()
    print(_bold("Quick start:"))
    print(f"  {_dim('$')} watchmen init           # 5-min setup wizard")
    print(f"  {_dim('$')} watchmen status         # see your tracked projects")
    print(f"  {_dim('$')} watchmen open           # open the viewer in your browser")
    print()
    print(_dim("Run `watchmen <command> -h` for command-specific help."))
    print(_dim("Docs + repo: https://github.com/firstbatchxyz/watchmen"))


def _is_first_run() -> bool:
    """Heuristic: 'fresh install' = no tracked projects AND no corpus.db.
    Used to nudge first-time users toward `watchmen init`."""
    if not (ROOT / "state.db").exists() and not (ROOT / "corpus.db").exists():
        return True
    try:
        state.init_db()
        return not state.list_projects()
    except Exception:
        return True


def _bare_default() -> int:
    """What `watchmen` (no subcommand) does. Fresh installs see a banner + the
    `init` nudge; users with state see `status` directly."""
    if _is_first_run():
        try:
            import banner
            from rich.console import Console
            banner.render(Console())
        except Exception:
            pass
        print(_bold("First run? Get started in 5 minutes:"))
        print(f"  {_dim('$')} watchmen init       # interactive setup wizard")
        print(f"  {_dim('$')} watchmen --help     # full command reference")
        print()
        return 0
    return cmd_status(argparse.Namespace())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="watchmen", description=__doc__.split("\n")[0], add_help=False)
    # Override the argparse-generated help with our grouped renderer.
    parser.format_help = lambda: ""  # type: ignore[method-assign]
    parser.print_help = lambda *a, **kw: _print_grouped_help(parser)  # type: ignore[method-assign]
    parser.add_argument("-h", "--help", action="help", help=argparse.SUPPRESS)
    parser.add_argument("--version", action="version", version=f"watchmen {_version()}")
    sub = parser.add_subparsers(dest="cmd")

    # Primary canonical entry point — first-time users discover this via --help.
    sub.add_parser("init", help="5-minute interactive setup wizard").set_defaults(func=cmd_init)
    p_doc = sub.add_parser("doctor", help="diagnose your install — API key, corpus, services")
    p_doc.set_defaults(func=cmd_doctor)

    p_open = sub.add_parser("open", help="open the viewer in your default browser")
    p_open.add_argument("project", nargs="?", help="optional project key — jumps to its page")
    p_open.add_argument("--host", default=config.VIEWER_DEFAULT_HOST)
    p_open.add_argument("--port", type=int, default=config.viewer_port())
    p_open.set_defaults(func=cmd_open)

    p_logs = sub.add_parser("logs", help="tail launchd logs (daemon | viewer | all)")
    p_logs.add_argument("name", choices=("daemon", "viewer", "all"), nargs="?", default="all")
    p_logs.add_argument("-f", "--follow", action="store_true", help="tail -F (follow appended lines)")
    p_logs.add_argument("-n", "--lines", type=int, default=50, help="initial lines to print (default 50)")
    p_logs.set_defaults(func=cmd_logs)

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

    # `config` was a P3 placeholder — kept as hidden alias until removed entirely.
    sub.add_parser("config", help=argparse.SUPPRESS).set_defaults(func=cmd_config)

    # ── daemon (noun) ──────────────────────────────────────────────────────
    p_daemon = sub.add_parser("daemon", help="run / install / uninstall the watchmen daemon")
    daemon_sub = p_daemon.add_subparsers(dest="daemon_cmd")
    p_drun = daemon_sub.add_parser("run", help="run scheduling loop in the foreground")
    _add_daemon_run_args(p_drun)
    p_drun.set_defaults(func=cmd_daemon)
    p_dins = daemon_sub.add_parser("install", help="install launchd agent for autostart on login")
    _add_daemon_install_args(p_dins)
    p_dins.set_defaults(func=cmd_install_daemon)
    daemon_sub.add_parser("uninstall", help="remove the launchd agent").set_defaults(func=cmd_uninstall_daemon)
    p_daemon.set_defaults(func=lambda a: (p_daemon.print_help() or 1))

    # ── viewer (noun) ──────────────────────────────────────────────────────
    p_viewer = sub.add_parser("viewer", help="run / install / uninstall the local web viewer")
    viewer_sub = p_viewer.add_subparsers(dest="viewer_cmd")
    p_vrun = viewer_sub.add_parser("run", help=f"start the viewer in the foreground ({config.viewer_base_url()})")
    _add_viewer_run_args(p_vrun)
    p_vrun.set_defaults(func=cmd_viewer)
    p_vins = viewer_sub.add_parser("install", help="install launchd agent for autostart on login")
    _add_viewer_install_args(p_vins)
    p_vins.set_defaults(func=cmd_install_viewer)
    viewer_sub.add_parser("uninstall", help="remove the launchd agent").set_defaults(func=cmd_uninstall_viewer)
    p_viewer.set_defaults(func=lambda a: (p_viewer.print_help() or 1))

    # ── hooks (noun) ───────────────────────────────────────────────────────
    p_hooks = sub.add_parser("hooks", help="install / uninstall / inspect Claude Code hooks")
    hooks_sub = p_hooks.add_subparsers(dest="hooks_cmd")
    hooks_sub.add_parser("install", help="wire watchmen_observe.sh into ~/.claude/settings.json").set_defaults(func=cmd_install_hooks)
    hooks_sub.add_parser("uninstall", help="remove watchmen entries from ~/.claude/settings.json").set_defaults(func=cmd_uninstall_hooks)
    hooks_sub.add_parser("status", help="show which hook events are wired up").set_defaults(func=cmd_hooks_status)
    p_hooks.set_defaults(func=lambda a: (p_hooks.print_help() or 1))

    # ── statusline (noun) ──────────────────────────────────────────────────
    p_sl = sub.add_parser("statusline", help="install / uninstall the 💡 watchmen indicator")
    sl_sub = p_sl.add_subparsers(dest="statusline_cmd")
    p_slin = sl_sub.add_parser("install", help="wire the 💡 watchmen indicator into ~/.claude/settings.json")
    _add_statusline_install_args(p_slin)
    p_slin.set_defaults(func=cmd_install_statusline)
    sl_sub.add_parser("uninstall", help="remove the watchmen statusLine entry").set_defaults(func=cmd_uninstall_statusline)
    p_sl.set_defaults(func=lambda a: (p_sl.print_help() or 1))

    # ── plugin (noun) ──────────────────────────────────────────────────────
    p_plug = sub.add_parser("plugin", help="manage the watchmen Claude Code plugin marketplace clone")
    plug_sub = p_plug.add_subparsers(dest="plugin_cmd")
    plug_sub.add_parser("update", help="git pull the marketplace clone so /plugin install picks up the latest").set_defaults(func=cmd_update_plugin)
    plug_sub.add_parser("status", help="show plugin marketplace + cache + statusLine state").set_defaults(func=cmd_plugin_status)
    p_plug.set_defaults(func=lambda a: (p_plug.print_help() or 1))

    # ── launchd (noun) ─────────────────────────────────────────────────────
    p_ld = sub.add_parser("launchd", help="inspect the watchmen launchd agents")
    ld_sub = p_ld.add_subparsers(dest="launchd_cmd")
    ld_sub.add_parser("status", help="show installed/loaded launchd agents").set_defaults(func=cmd_launchd_status)
    p_ld.set_defaults(func=lambda a: (p_ld.print_help() or 1))

    # ── deprecated aliases ─────────────────────────────────────────────────
    # Old verb-noun-with-hyphens forms. Keep working, print soft deprecation
    # line. Plan: remove after 1-2 releases once teammates' scripts update.
    # All deprecated aliases use argparse.SUPPRESS so they don't pollute --help,
    # but still parse correctly for existing scripts.
    p_id = sub.add_parser("install-daemon", help=argparse.SUPPRESS)
    _add_daemon_install_args(p_id)
    p_id.set_defaults(func=_deprecate("daemon install", cmd_install_daemon))

    p_iv = sub.add_parser("install-viewer", help=argparse.SUPPRESS)
    _add_viewer_install_args(p_iv)
    p_iv.set_defaults(func=_deprecate("viewer install", cmd_install_viewer))

    sub.add_parser("uninstall-daemon", help=argparse.SUPPRESS).set_defaults(
        func=_deprecate("daemon uninstall", cmd_uninstall_daemon))
    sub.add_parser("uninstall-viewer", help=argparse.SUPPRESS).set_defaults(
        func=_deprecate("viewer uninstall", cmd_uninstall_viewer))
    sub.add_parser("launchd-status", help=argparse.SUPPRESS).set_defaults(
        func=_deprecate("launchd status", cmd_launchd_status))

    sub.add_parser("install-hooks", help=argparse.SUPPRESS).set_defaults(
        func=_deprecate("hooks install", cmd_install_hooks))
    sub.add_parser("uninstall-hooks", help=argparse.SUPPRESS).set_defaults(
        func=_deprecate("hooks uninstall", cmd_uninstall_hooks))
    sub.add_parser("hooks-status", help=argparse.SUPPRESS).set_defaults(
        func=_deprecate("hooks status", cmd_hooks_status))

    p_isl = sub.add_parser("install-statusline", help=argparse.SUPPRESS)
    _add_statusline_install_args(p_isl)
    p_isl.set_defaults(func=_deprecate("statusline install", cmd_install_statusline))
    sub.add_parser("uninstall-statusline", help=argparse.SUPPRESS).set_defaults(
        func=_deprecate("statusline uninstall", cmd_uninstall_statusline))

    sub.add_parser("update-plugin", help=argparse.SUPPRESS).set_defaults(
        func=_deprecate("plugin update", cmd_update_plugin))
    sub.add_parser("plugin-status", help=argparse.SUPPRESS).set_defaults(
        func=_deprecate("plugin status", cmd_plugin_status))

    # `onboard` / `reonboard` are hidden aliases — `init` is the canonical name.
    sub.add_parser("onboard", help=argparse.SUPPRESS).set_defaults(func=cmd_onboard)
    sub.add_parser("reonboard", help=argparse.SUPPRESS).set_defaults(func=cmd_reonboard)

    p_settings = sub.add_parser("settings", help="view / update per-project settings")
    settings_sub = p_settings.add_subparsers(dest="settings_cmd")
    settings_sub.add_parser("list", help="show all tracked projects + their settings").set_defaults(func=cmd_settings_list)
    p_show = settings_sub.add_parser("show", help="show one project's full settings")
    p_show.add_argument("project")
    p_show.set_defaults(func=cmd_settings_show)
    p_set = settings_sub.add_parser("set", help=f"update a setting. keys: {', '.join(_SETTABLE_KEYS)}")
    p_set.add_argument("project")
    p_set.add_argument("key", choices=_SETTABLE_KEYS)
    p_set.add_argument("value")
    p_set.set_defaults(func=cmd_settings_set)
    p_apikey = settings_sub.add_parser("api-key", help="set or check the OpenRouter API key (live-validated against /auth/key)")
    p_apikey.add_argument("--check", action="store_true", help="check current key without changing it")
    p_apikey.add_argument("--set", metavar="KEY", help="set a key non-interactively (for scripting)")
    p_apikey.set_defaults(func=cmd_settings_api_key)
    p_port = settings_sub.add_parser("port", help="get or set the viewer port (writes to ~/.config/watchmen/.env)")
    p_port.add_argument("value", nargs="?", help="new port (omit to print current)")
    p_port.set_defaults(func=cmd_settings_port)
    p_settings.set_defaults(func=lambda a: (p_settings.print_help() or 1))

    p_metrics = sub.add_parser("metrics", help="daily efficiency rollup (sessions, tokens, cost, suggestion uptake)")
    p_metrics.add_argument("project", help="project key")
    p_metrics.add_argument("--days", type=int, default=30, help="window length (default 30)")
    p_metrics.set_defaults(func=cmd_metrics)

    args = parser.parse_args(argv)
    if not args.cmd:
        return _bare_default()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
