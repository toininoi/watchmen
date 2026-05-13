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
import random
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


def _bright_blue(s: str) -> str:
    return f"\033[94m{s}\033[0m"


def _cyan(s: str) -> str:
    return f"\033[36m{s}\033[0m"


# ─── TUI visualization helpers ──────────────────────────────────────────────
# Unicode block characters used for compact bar charts + sparklines. No
# external chart deps — we just print sized strings inside Rich Tables /
# plain stdout. Both helpers degrade gracefully when their input is empty
# or all-zero, returning empty strings rather than ZeroDivisionError.


_SPARK_BLOCKS = "▁▂▃▄▅▆▇█"


def _sparkline(values: list[float]) -> str:
    """Compact one-character-per-data-point trend line. Auto-scales to the
    max value in the series — useful for showing daily cost or session counts
    over a 30-day window in 30 visible characters."""
    if not values:
        return ""
    peak = max(values)
    if peak <= 0:
        return _SPARK_BLOCKS[0] * len(values)
    return "".join(_SPARK_BLOCKS[min(7, int((v / peak) * 7))] for v in values)


def _bar(value: float, max_value: float, width: int = 30) -> str:
    """Horizontal bar with half-block precision. Empty when value or max ≤ 0
    so projects with no spend render cleanly as an empty cell instead of `0`
    pixels of bar."""
    if max_value <= 0 or value <= 0:
        return ""
    ratio = max(0.0, min(1.0, value / max_value))
    cells = ratio * width
    full = int(cells)
    half = "▌" if (cells - full) >= 0.5 else ""
    return "█" * full + half


# ─── Watchmen aesthetic helpers ─────────────────────────────────────────────
# Small thematic touches keyed to each character: Manhattan blue for `doctor`,
# Doomsday Clock yellow for `status`, mission-log framing for `runs`. Kept
# tiny so the CLI stays readable on narrow terminals.


def _doomsday_minutes_to_midnight(needs_analysis: int, total: int) -> int:
    """Map project staleness to the iconic Watchmen Doomsday Clock position.

    Curve calibrated so a small fleet with 1-2 stale projects sits at the
    classic "five to midnight" (Watchmen's opening clock position), while
    a fleet where >70% of projects need attention is the harrowing
    one-minute-to-midnight.

    All up to date  → 12 to midnight (safe)
    < 20% stale     → 8 to midnight
    < 40% stale     → 5 to midnight  (canonical Watchmen position)
    < 70% stale     → 2 to midnight
    >= 70% stale    → 1 to midnight  (critical)"""
    if total == 0:
        return 12
    ratio = needs_analysis / total
    if ratio == 0:    return 12
    if ratio < 0.2:   return 8
    if ratio < 0.4:   return 5
    if ratio < 0.7:   return 2
    return 1


# Spelled-out form for the clock line — reads better than "5 minutes" and is
# the exact phrasing used in the comic.
_DOOMSDAY_WORD = {12: "twelve", 8: "eight", 5: "five", 2: "two", 1: "one"}

# Hand glyph by minutes-to-midnight. The minute hand rotates *back* from 12
# toward 11 as we approach doom. We can't capture the rotation precisely in
# a single character, but the variation gives the clock face a real "look".
_DOOMSDAY_HAND = {
    12: "·",   # near 12 — calm dot
    8:  "╲",   # 24° back from vertical
    5:  "┘",   # 30° back, the canonical Watchmen position
    2:  "╲",   # almost vertical again
    1:  "│",   # essentially at 12, the doom moment
}

# Random tick-tock taglines per severity. Pooled so consecutive runs feel
# different — the comic itself plays variations on "tick tock tick tock".
_DOOMSDAY_TAGLINES_CLEAR = (
    "all clear.",
    "the city sleeps.",
    "no field activity required.",
    "midnight is far.",
)
_DOOMSDAY_TAGLINES_TICK = (
    "tick. tock.",
    "the clock advances.",
    "minutes are short here.",
    "tick tock tick tock.",
)
_DOOMSDAY_TAGLINES_CRITICAL = (
    "tick. tock. tick.",
    "midnight is near.",
    "the hour grows late.",
    "no one is watching us watch.",
)


# ─── Dr. Manhattan flavor for `watchmen doctor` ─────────────────────────────
# A small atom panel + three pools of in-character quotes (one per severity).
# random.choice rotates the line each run so the doctor command never feels
# canned. Header text stays stable (tests + scripts can grep "Dr. Manhattan").

_MANHATTAN_QUOTES_OK = (
    "I see all the body's mechanisms, intact and predictable.",
    "Nothing here requires my intervention.",
    "The structure holds. No deviation from expected paths.",
    "All is precisely as it should be.",
    "Causality is uninterrupted.",
    "On Mars, I would call this a peaceful arrangement.",
)
_MANHATTAN_QUOTES_WARN = (
    "A pattern frays — observable, not yet consequential.",
    "Minor deviations. The mechanism still turns.",
    "One inconsistency. Time will absorb it.",
    "A small fault. I leave it for you to mend.",
)
_MANHATTAN_QUOTES_FAIL = (
    "The mechanism is impeded. Correction is necessary.",
    "Causality is interrupted here. Attend to it.",
    "I observe a discontinuity.",
    "Something is broken. I cannot fix what I do not touch.",
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


def _manhattan_atom_panel() -> list[str]:
    """3-line atom panel — small enough to sit above the doctor table without
    consuming the visible terminal. The ⚛ glyph is literally a stylized
    hydrogen atom, the same one Manhattan wears on his forehead in the comic."""
    return [
        "  ┌─────┐",
        "  │  ⚛  │",
        "  └─────┘",
    ]


def _doomsday_ascii(needs: int, total: int) -> list[str]:
    """3-line render: tiny clock face + 12-segment doom-bar + spelled label.

    Each ASCII element is conditional on the calculated minutes-to-midnight,
    so the visual reads as a real, slightly-different clock each time you
    invoke `watchmen status`. Doom-bar fills left-to-right as we approach
    midnight (segments = `12 - minutes_to_midnight`)."""
    n = _doomsday_minutes_to_midnight(needs, total)
    hand = _DOOMSDAY_HAND[n]
    filled = max(0, 12 - n)
    bar = "█" * filled + "░" * (12 - filled)
    word = _DOOMSDAY_WORD[n]
    suffix = "minute" if n == 1 else "minutes"

    if n == 12:
        tagline = random.choice(_DOOMSDAY_TAGLINES_CLEAR)
    elif n <= 2:
        tagline = random.choice(_DOOMSDAY_TAGLINES_CRITICAL)
    else:
        tagline = random.choice(_DOOMSDAY_TAGLINES_TICK)

    return [
        _yellow("  ╭─╮"),
        _yellow(f"  │{hand}│  ") + _yellow(bar) + _yellow(f"  {word} {suffix} to midnight"),
        _yellow("  ╰─╯  ") + _dim(tagline),
    ]


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

    # Collect progress per project first so we can compute the Doomsday Clock
    # before rendering the table — the clock summarizes the table's verdict.
    rows = []
    needs = 0
    for p in tracked:
        progress = state.get_project_progress(p["project_key"])
        rows.append((p, progress))
        if progress.get("needs_analysis"):
            needs += 1

    for line in _doomsday_ascii(needs, len(tracked)):
        print(line)
    print()

    # Rich Table auto-sizes columns to widest cell — fixes the printf
    # alignment drift we used to have when the adapter-counts column overflowed
    # its header. Headers + separators rendered consistently with `doctor`
    # and `metrics`.
    from rich.console import Console
    from rich.table import Table
    console = Console()
    table = Table(show_header=True, header_style="bold", expand=False)
    table.add_column("project")
    table.add_column("state")
    table.add_column("last analyst")
    table.add_column("new", justify="right")
    table.add_column("cc", justify="right")
    table.add_column("cd", justify="right")
    table.add_column("pi", justify="right")
    table.add_column("notes")
    for p, progress in rows:
        last_day = p["last_analyst_day"] or "—"
        new_n = progress.get("new_prompts_since_last_analysis", "?")
        st = "enabled" if p["enabled"] else "[yellow]paused[/]"
        if progress.get("needs_analysis"):
            flag = "[yellow]● needs analysis[/]"
        elif p["last_analyst_day"]:
            flag = "[green]● up to date[/]"
        else:
            flag = ""
        bd = _adapter_breakdown(p["project_key"])
        table.add_row(
            p["project_key"], st, last_day, str(new_n),
            str(bd.get("claude_code", 0)),
            str(bd.get("codex", 0)),
            str(bd.get("pi", 0)),
            flag,
        )
    console.print(table)

    runs = state.recent_runs(limit=5)
    if runs:
        # "Mission log" framing borrows from the comic's Crimebusters/Minutemen
        # tradition of recording field activity in a shared journal.
        console.print()
        console.print("[bold]Mission log:[/]")
        log = Table(show_header=False, box=None, padding=(0, 1, 0, 1))
        log.add_column("when")
        log.add_column("project")
        log.add_column("kind")
        log.add_column("status")
        for r in runs:
            t = (r["started_at"] or "")[:19]
            status = r["status"]
            tag = {"ok": "[green]ok[/]", "running": "[yellow]running[/]"}.get(status, f"[dim]{status}[/]")
            log.add_row(t, r["project_key"], r["kind"], tag)
        console.print(log)
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


# ─── Round 1: inspection + provenance commands ─────────────────────────────


_ADAPTER_SHORT = {"claude_code": "cc", "codex": "cd", "pi": "pi"}


def _kai_claude_dir(project_key: str) -> Path:
    return ROOT / "kai_claude" / project_key


def _tracked_project_keys() -> list[str]:
    """Project keys that have at least a `kai_claude/<key>/` dir on disk —
    used as the universe for `show` and `recent` without a project arg.
    Falls back to state.list_projects() when nothing is on disk yet."""
    base = ROOT / "kai_claude"
    if base.exists():
        keys = sorted(d.name for d in base.iterdir() if d.is_dir() and (d / "skills").exists())
        if keys:
            return keys
    return [p["project_key"] for p in state.list_projects()]


def _adapter_breakdown(project_key: str) -> dict[str, int]:
    """Session counts per adapter from corpus.db, filtered to substantive
    non-subagent sessions matching the project path."""
    import sqlite3
    corpus_db = ROOT / "corpus.db"
    if not corpus_db.exists():
        return {}
    cc = sqlite3.connect(corpus_db)
    rows = cc.execute(
        """SELECT agent, COUNT(*) FROM sessions
           WHERE project_dir LIKE ? AND is_subagent = 0
           GROUP BY agent""",
        (f"%{project_key}%",),
    ).fetchall()
    cc.close()
    return {agent: n for agent, n in rows}


def _format_adapter_count(breakdown: dict[str, int]) -> str:
    """Compact `2053 cc · 417 cd · 0 pi` style line. Always shows all 3 adapters
    so the row width is stable, even when projects don't have sessions in
    every adapter yet."""
    parts = []
    for agent in ("claude_code", "codex", "pi"):
        n = breakdown.get(agent, 0)
        parts.append(f"{n:>4} {_ADAPTER_SHORT[agent]}")
    return " · ".join(parts)


def cmd_show(args) -> int:
    """Terminal-native viewer. Three modes:

      watchmen show                          # list every project + skill count
      watchmen show <project>                # list <project>'s artifacts
      watchmen show <project> <skill|file>   # dump that skill/file

    Disambiguation: second arg ending in `.md` or `.json` is read as a file
    path under kai_claude/<project>/; anything else is treated as a skill slug
    and resolved to kai_claude/<project>/skills/<slug>/SKILL.md."""
    base = ROOT / "kai_claude"
    if not args.project:
        # Mode 1 — overview of every project that has a curated bundle.
        keys = _tracked_project_keys()
        if not keys:
            print(_dim("No projects curated yet. Run `watchmen init` or `watchmen curate <project>`."))
            return 0
        print(_bold("\nCurated projects:\n"))
        print(f"  {'project':<32} {'skills':>7}  {'claude_md':<10}  last commit")
        print(_dim("  " + "─" * 90))
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
        print(_dim("Drill in with `watchmen show <project>` or `watchmen show <project> <skill>`."))
        return 0

    proj_dir = base / args.project
    if not proj_dir.exists():
        print(_yellow(f"no curated bundle for '{args.project}' at {proj_dir}"))
        print(_dim("  run `watchmen curate " + args.project + "` first, or check `watchmen show` for valid keys"))
        return 1

    if not args.target:
        # Mode 2 — project overview: artifacts + skills + last commit.
        print(_bold(f"\n{args.project}\n"))
        # Artifacts at the top level.
        for name in ("CLAUDE.md", "_index.md", "_changelog.md", "_curation_log.md", "_candidates.json", "_manifest.json"):
            p = proj_dir / name
            if p.exists():
                size = p.stat().st_size
                print(f"  {_green('●')} {name:<24} {size:>7,}B")
            else:
                print(f"  {_dim('·')} {_dim(name)}")
        # Skills
        skills_dir = proj_dir / "skills"
        if skills_dir.exists():
            skills = sorted(d for d in skills_dir.iterdir() if d.is_dir())
            print()
            print(_bold(f"  Skills ({len(skills)}):"))
            for s in skills:
                file_count = sum(1 for _ in s.rglob("*") if _.is_file())
                desc = ""
                skill_md = s / "SKILL.md"
                if skill_md.exists():
                    for line in skill_md.read_text().splitlines():
                        if line.startswith("description:"):
                            desc = line.split(":", 1)[1].strip().strip('"').strip("'")[:80]
                            break
                print(f"    {s.name:<32} {file_count:>3} files  {_dim(desc)}")
        print()
        print(_dim("View a file: `watchmen show " + args.project + " CLAUDE.md`"))
        print(_dim("View a skill: `watchmen show " + args.project + " <skill-slug>`"))
        print(_dim("Provenance:   `watchmen why " + args.project + " <skill-slug>`"))
        return 0

    # Mode 3 — dump a single artifact (file or skill bundle).
    target = args.target
    if target.endswith(".md") or target.endswith(".json") or target.endswith(".log"):
        path = proj_dir / target
        if not path.exists():
            print(_yellow(f"file not found: {path}"))
            return 1
        _render_file(path, raw=args.raw)
        return 0

    skill_dir = proj_dir / "skills" / target
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        print(_yellow(f"no skill '{target}' in {args.project} (looked at {skill_dir})"))
        candidates = sorted(d.name for d in (proj_dir / "skills").iterdir() if d.is_dir()) if (proj_dir / "skills").exists() else []
        if candidates:
            print(_dim(f"  available: {', '.join(candidates)}"))
        return 1
    _render_file(skill_md, raw=args.raw)
    files = sorted(p for p in skill_dir.rglob("*") if p.is_file() and p != skill_md)
    if files:
        print()
        print(_bold(f"Bundle ({len(files)} other file(s)):"))
        for f in files:
            rel = f.relative_to(skill_dir)
            print(f"  {rel}  {_dim(f'({f.stat().st_size:,}B)')}")
    return 0


def _render_file(path: Path, raw: bool = False) -> None:
    """Pretty-print a file based on extension. `.md` → Rich Markdown render
    (headers/code/tables stylized). `.json` → Rich JSON with syntax colors.
    Anything else → plain text. `--raw` forces plain text — important when
    piping output to a file (Rich already strips ANSI when stdout isn't a
    tty, but `--raw` is the explicit, scriptable opt-out)."""
    text = path.read_text()
    if raw:
        sys.stdout.write(text)
        return
    print(_dim(f"# {path.name}\n"))
    if path.suffix == ".md":
        # Rich Markdown styles ATX headers, fenced code, lists, blockquotes,
        # tables. Auto-disables styling when stdout isn't a tty.
        from rich.console import Console
        from rich.markdown import Markdown
        Console().print(Markdown(text))
    elif path.suffix == ".json":
        from rich.console import Console
        from rich.json import JSON
        try:
            Console().print(JSON(text))
        except Exception:
            sys.stdout.write(text)
    else:
        sys.stdout.write(text)


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
    import sqlite3, json as _json
    proj_dir = _kai_claude_dir(args.project)
    candidates_path = proj_dir / "_candidates.json"
    if not candidates_path.exists():
        print(_yellow(f"no candidates file at {candidates_path} — has the curator run for this project?"))
        return 1

    cands = _json.loads(candidates_path.read_text())
    match = next((c for c in cands if c.get("slug") == args.skill or c.get("name", "").lower() == args.skill.lower()), None)
    if not match:
        slugs = [c.get("slug", "?") for c in cands]
        print(_yellow(f"no candidate matches '{args.skill}'"))
        print(_dim(f"  available slugs: {', '.join(slugs)}"))
        return 1

    name = match.get("name", args.skill)
    slug = match.get("slug", args.skill)
    description = match.get("description", "")
    when_to_use = match.get("when_to_use", "")
    source_files = match.get("source_files") or []
    session_ids = match.get("session_ids") or []

    print(_bold(f"\n{name}") + _dim(f"  ({slug})\n"))
    if description:
        print(_dim("description:"))
        print(f"  {description}")
        print()
    if when_to_use:
        print(_dim("when_to_use:"))
        # `when_to_use` may be string or list-of-strings.
        triggers = when_to_use if isinstance(when_to_use, list) else [when_to_use]
        for t in triggers[:6]:
            print(f"  • {t}")
        if len(triggers) > 6:
            print(_dim(f"  … {len(triggers) - 6} more"))
        print()
    if source_files:
        print(_dim(f"source_files ({len(source_files)}):"))
        for f in source_files[:10]:
            exists = (Path(f).exists() or (ROOT / f).exists())
            marker = _green("✓") if exists else _yellow("?")
            print(f"  {marker} {f}")
        if len(source_files) > 10:
            print(_dim(f"  … {len(source_files) - 10} more"))
        print()

    # Cross-reference session_ids with corpus.db to surface adapter + first prompt.
    corpus_db = ROOT / "corpus.db"
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
        print(_dim(f"sessions ({len(session_ids)}):"))
        print(f"  {'session_id':<14} {'agent':<11} {'date':<11}  first prompt")
        print(_dim("  " + "─" * 90))
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
                adapter = _ADAPTER_SHORT.get(row["agent"], row["agent"])
                date = (row["started_at"] or "")[:10]
                snippet = (row["first_prompt"] or "").replace("\n", " ")[:60]
                print(f"  {short[:14]:<14} {adapter:<11} {date:<11}  {_dim(snippet)}")
            else:
                # Show the raw label — useful when the analyst cited a session
                # by content rather than ID (common with codex/pi early on).
                print(f"  {short[:14]:<14} {_dim('(not in corpus)')}        {_dim(str(sid)[:60])}")
        cc.close()
        print()

    excerpt = _curation_log_excerpt(proj_dir, slug, name)
    if excerpt:
        print(_dim("curator log excerpt:"))
        for line in excerpt.splitlines()[:30]:
            print(f"  {line}")
        print()
    return 0


def cmd_recent(args) -> int:
    """Git log of kai_claude/ artifact commits in the last N days. Every curator
    run lands as a commit, so this is a fast 'what changed lately' view that
    doesn't require the web viewer."""
    days = args.days
    keys = [args.project] if args.project else _tracked_project_keys()
    base = ROOT / "kai_claude"
    if not keys:
        print(_dim("no curated projects yet."))
        return 0
    print(_bold(f"\nRecent curator activity (last {days}d):\n"))
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
        print(_bold(f"  {key}:"))
        for chunk in chunks[:args.limit]:
            parts = chunk.split("\n", 1)
            head = parts[0].split("|", 2)
            if len(head) < 3:
                continue
            sha, when, subject = head
            stat = (parts[1] if len(parts) > 1 else "").strip()
            print(f"    {sha}  {when[:10]}  {subject}")
            if stat:
                print(f"             {_dim(stat)}")
        print()
    if not any_found:
        print(_dim(f"  no curator commits in the last {days}d."))
    return 0


def cmd_doctor(args) -> int:
    """Health check: API key, OpenRouter reachability, corpus, tracked projects,
    daemon/viewer state, hooks, latest run age, disk free.

    Used to self-diagnose a broken install — single screen of ✓/✗ rows. Returns
    0 if everything is green, 1 if any required check fails.

    Themed after Dr. Manhattan, who in the comic spends a chapter on Mars
    contemplating the deterministic clockwork of human bodies. The bright-blue
    palette + the atomic glyph echo his iconic look without taking over the
    table."""
    from rich.console import Console
    from rich.table import Table
    console = Console()

    # Manhattan-blue atom panel + header. We pick the closing quote later
    # (after we know fails/warns) but the panel itself goes up top.
    console.print()
    for line in _manhattan_atom_panel():
        console.print(f"[bold bright_blue]{line}[/]")
    console.print(f"  [bold bright_blue]Dr. Manhattan's vitals[/]")

    fails = 0
    warns = 0
    table = Table(show_header=True, header_style="bold bright_blue", expand=False, border_style="bright_blue")
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
    # Closing quote is randomly drawn from the pool that matches severity —
    # consecutive runs of `watchmen doctor` feel different even when nothing
    # has changed. Quote stays in Manhattan's voice (detached, observational).
    if fails == 0 and warns == 0:
        console.print(f"\n  [bright_blue italic]{random.choice(_MANHATTAN_QUOTES_OK)}[/]")
    elif fails == 0:
        console.print(f"\n  [yellow italic]{random.choice(_MANHATTAN_QUOTES_WARN)}[/]  [dim]({warns} warning(s))[/]")
    else:
        console.print(f"\n  [red italic]{random.choice(_MANHATTAN_QUOTES_FAIL)}[/]  [dim]({fails} failure(s) / {warns} warning(s))[/]")
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

    # Rorschach-style inkblot prefix — mirror-symmetric, picked at random
    # from a small pool so the line "rotates" between invocations like
    # flipping cards in Walter Kovacs's journal.
    print(_dim(f"  {_rorschach_inkblot()}  ") + f"opening {url}")
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

    if not args.project:
        return _cmd_metrics_global(args)

    rows = _metrics.daily_metrics(args.project, days=args.days)
    if not rows:
        print(f"No data for project '{args.project}'. Run `watchmen ingest` first?")
        return 1
    last7 = _metrics.summarize_window(rows, min(7, args.days))
    last30 = _metrics.summarize_window(rows, args.days)
    console = Console()

    # Sparklines for daily cost + prompts — give an at-a-glance shape of the
    # window. Sparkline width = day count, so it's naturally proportional to
    # --days. `rows` is ordered most-recent-first by metrics.daily_metrics;
    # reverse to read left-to-right as time progresses.
    daily = list(reversed(rows))
    cost_series = [float(r.get("cost_usd", 0) or 0) for r in daily]
    prompts_series = [float(r.get("prompts", 0) or 0) for r in daily]
    if any(cost_series):
        console.print(f"  [yellow]cost[/]    {_sparkline(cost_series)}  [dim]peak ${max(cost_series):.2f}/d · total ${last30['cost_usd']:.2f}[/]")
    if any(prompts_series):
        console.print(f"  [cyan]prompts[/] {_sparkline(prompts_series)}  [dim]peak {int(max(prompts_series))}/d · total {last30['prompts']}[/]")
    console.print()

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
    # Per-adapter breakdown — gives quick visibility into where this project's
    # sessions came from (claude_code vs codex vs pi). Pulled from corpus.db
    # directly since `metrics.daily_metrics` doesn't track adapter today.
    breakdown = _adapter_breakdown(args.project)
    if breakdown:
        adapt = Table(show_header=True, header_style="bold cyan", expand=False)
        adapt.add_column("adapter")
        adapt.add_column("sessions", justify="right")
        for agent in ("claude_code", "codex", "pi"):
            adapt.add_row(agent, str(breakdown.get(agent, 0)))
        console.print(adapt)
    console.print(f"\n  full daily breakdown: {config.viewer_base_url()}/p/{args.project}/metrics")
    return 0


def _cmd_metrics_global(args) -> int:
    """Global rollup across all tracked projects. Aggregates tokens/cost from
    metrics.daily_metrics per project + adapter session counts from corpus.db.
    Shown when `watchmen metrics` is invoked without a project arg."""
    import metrics as _metrics
    from rich.console import Console
    from rich.table import Table
    state.init_db()
    projects = state.list_projects()
    if not projects:
        print(_dim("No projects tracked yet — run `watchmen init`."))
        return 1
    console = Console()

    # Per-project summary table. Sorted by cost descending so the biggest
    # spenders are at the top.
    rows = []
    totals = {"sessions": 0, "prompts": 0, "input": 0, "output": 0, "cost": 0.0}
    for p in projects:
        key = p["project_key"]
        daily = _metrics.daily_metrics(key, days=args.days)
        summary = _metrics.summarize_window(daily, args.days) if daily else None
        if summary:
            totals["sessions"] += summary["sessions"]
            totals["prompts"]  += summary["prompts"]
            totals["input"]    += summary["input_tokens"]
            totals["output"]   += summary["output_tokens"]
            totals["cost"]     += summary["cost_usd"]
            rows.append((key, summary))
    rows.sort(key=lambda r: r[1]["cost_usd"], reverse=True)

    # Global rollup as a bar chart instead of a flat table — eyes track
    # relative spend much faster from horizontal bars than from columns of
    # numbers. Bars sized vs the project with the largest spend so the
    # heaviest user gets a full-width bar.
    header = (
        f"\n[bold]Global rollup — {args.days}d[/]  "
        f"[dim]{len(rows)} projects · {totals['sessions']:,} sessions · "
        f"${totals['cost']:.2f}[/]\n"
    )
    console.print(header)
    max_cost = max((s["cost_usd"] for _, s in rows), default=0.0)
    cost_tbl = Table(title="Cost by project", show_header=True, header_style="bold magenta", box=None, padding=(0, 1, 0, 1))
    cost_tbl.add_column("project", style="bold")
    cost_tbl.add_column("", width=30)  # the bar
    cost_tbl.add_column("cost", justify="right")
    cost_tbl.add_column("share", justify="right")
    cost_tbl.add_column("sessions", justify="right")
    for key, s in rows:
        share = (s["cost_usd"] / totals["cost"] * 100) if totals["cost"] else 0
        bar = _bar(s["cost_usd"], max_cost, width=30)
        cost_tbl.add_row(
            key,
            f"[yellow]{bar}[/]",
            f"${s['cost_usd']:>9.2f}",
            f"{share:>3.0f}%",
            f"{s['sessions']:,}",
        )
    console.print(cost_tbl)

    # Adapter breakdown across all projects — also rendered as bars, sized
    # vs the largest adapter so codex's 88% reads visually at a glance.
    import sqlite3
    corpus_db = ROOT / "corpus.db"
    if corpus_db.exists():
        cc = sqlite3.connect(corpus_db)
        adapter_rows = cc.execute(
            """SELECT agent, COUNT(*) AS n, COUNT(DISTINCT project_dir) AS projects
               FROM sessions WHERE is_subagent = 0 GROUP BY agent ORDER BY n DESC"""
        ).fetchall()
        cc.close()
        if adapter_rows:
            console.print()
            max_n = max(n for _, n, _ in adapter_rows) or 1
            total_n = sum(n for _, n, _ in adapter_rows)
            atbl = Table(title="Sessions by adapter", show_header=True, header_style="bold cyan", box=None, padding=(0, 1, 0, 1))
            atbl.add_column("adapter", style="bold")
            atbl.add_column("", width=28)
            atbl.add_column("sessions", justify="right")
            atbl.add_column("share", justify="right")
            atbl.add_column("projects", justify="right")
            for agent, n, projects_n in adapter_rows:
                pct = (n / total_n * 100) if total_n else 0
                atbl.add_row(
                    agent,
                    f"[cyan]{_bar(n, max_n, width=28)}[/]",
                    f"{n:,}",
                    f"{pct:>3.0f}%",
                    str(projects_n),
                )
            console.print(atbl)
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
        ("show",       "list / view curated bundles (project, skill, file)"),
        ("why",        "provenance for a skill: source sessions + curator rationale"),
        ("recent",     "git log of curator artifact changes (last N days)"),
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

    p_show = sub.add_parser("show", help="list / view curated bundles (no args = all projects)")
    p_show.add_argument("project", nargs="?", help="project key (omit to list all)")
    p_show.add_argument("target", nargs="?", help="skill slug or file name (CLAUDE.md, _curation_log.md, …)")
    p_show.add_argument("--raw", action="store_true", help="plain text output (skip Markdown/JSON pretty-printing)")
    p_show.set_defaults(func=cmd_show)

    p_why = sub.add_parser("why", help="provenance for a skill: source sessions + curator rationale")
    p_why.add_argument("project")
    p_why.add_argument("skill", help="skill slug (kebab-case) or display name")
    p_why.set_defaults(func=cmd_why)

    p_recent = sub.add_parser("recent", help="git log of curator artifact changes (no project = all)")
    p_recent.add_argument("project", nargs="?", help="project key (omit for all curated projects)")
    p_recent.add_argument("--days", type=int, default=7, help="lookback window in days (default 7)")
    p_recent.add_argument("--limit", type=int, default=10, help="max commits per project (default 10)")
    p_recent.set_defaults(func=cmd_recent)

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

    p_metrics = sub.add_parser("metrics", help="daily efficiency rollup (no project = global rollup)")
    p_metrics.add_argument("project", nargs="?", help="project key (omit for global rollup across all projects)")
    p_metrics.add_argument("--days", type=int, default=30, help="window length (default 30)")
    p_metrics.set_defaults(func=cmd_metrics)

    args = parser.parse_args(argv)
    if not args.cmd:
        return _bare_default()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
