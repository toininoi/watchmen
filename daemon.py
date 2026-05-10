"""watchmen daemon — continuous scheduling loop.

Cycle (default 30 min):
  1. Re-ingest corpus (scan ~/.claude/projects → corpus.db)
  2. For each tracked + enabled project:
     a. Check needs_analysis (new prompts > threshold)
     b. If yes: run incremental analyst
     c. If analyst ran successfully AND last CLAUDE.md regen > 24h: regen stage 3 (CLAUDE.md only)

Stage 1+2 (skill bundles) is intentionally NOT periodic — too expensive to run unattended,
needs human review. Run manually via `watchmen curate <project>`.

Logs to a file (default ~/Library/Logs/watchmen.log) so launchd output survives across runs.
Graceful shutdown on SIGTERM/SIGINT.

Usage:
  uv run watchmen daemon                  # run forever
  uv run watchmen daemon --once           # single iteration (testing)
  uv run watchmen daemon --interval 600   # check every 10 min
"""

import argparse
import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import state

ROOT = Path(__file__).parent
DEFAULT_INTERVAL = 1800       # 30 min
DEFAULT_CURATOR_AGE = 86400   # 24 h — regen CLAUDE.md if last one is older than this
DEFAULT_MODEL = "deepseek/deepseek-v4-flash"
DEFAULT_LOG = Path.home() / "Library" / "Logs" / "watchmen.log"

_shutdown = False


def setup_logging(log_path: Path) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("watchmen.daemon")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    fh = logging.handlers.RotatingFileHandler(str(log_path), maxBytes=10_000_000, backupCount=3)
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


def handle_signal(signum, frame):
    global _shutdown
    _shutdown = True


def _ingest_corpus(log: logging.Logger) -> None:
    log.info("ingest: rescanning ~/.claude/projects")
    r = subprocess.run(
        [sys.executable, str(ROOT / "corpus.py"), "scan"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=600,
    )
    if r.returncode != 0:
        log.error("ingest failed: %s", (r.stderr or r.stdout)[:500])
    else:
        last_line = (r.stdout or "").strip().splitlines()[-1] if r.stdout else ""
        log.info("ingest done: %s", last_line)


def _run_analyst(project_key: str, model: str, log: logging.Logger) -> bool:
    log.info("analyst[%s] starting (incremental)", project_key)
    progress = state.get_project_progress(project_key)
    from_day = progress.get("last_analyst_day")
    cmd = [sys.executable, str(ROOT / "analyze.py"), "-p", project_key, "--model", model]
    if from_day:
        cmd.extend(["--from-day", from_day])

    run_id = state.start_run(project_key, "analyst", notes=f"daemon:from_day={from_day}")
    r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=14400)
    if r.returncode != 0:
        log.error("analyst[%s] failed: %s", project_key, (r.stderr or r.stdout)[:500])
        state.finish_run(run_id, "failed", notes=f"exit {r.returncode}")
        return False

    analyses_dir = ROOT / "analyses" / project_key
    if analyses_dir.exists():
        day_files = sorted(p.stem for p in analyses_dir.glob("20*.md"))
        if day_files:
            state.update_project(project_key, last_analyst_day=day_files[-1], last_analyst_run=state.now_iso())
    state.finish_run(run_id, "ok")
    last_line = (r.stdout or "").strip().splitlines()[-1] if r.stdout else ""
    log.info("analyst[%s] done: %s", project_key, last_line)
    return True


def _regen_claude_md(project_key: str, model: str, log: logging.Logger) -> bool:
    proj = state.get_project(project_key)
    if not proj:
        return False
    log.info("regen-claude[%s] starting (stage 3 only)", project_key)
    cmd = [sys.executable, str(ROOT / "curate.py"),
           "--project", project_key, "--repo", proj["source_repo"],
           "--model", model, "--skip-finder", "--skip-skills"]
    run_id = state.start_run(project_key, "curator-claude-only", notes="daemon")
    r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=3600)
    if r.returncode != 0:
        log.error("regen-claude[%s] failed: %s", project_key, (r.stderr or r.stdout)[:500])
        state.finish_run(run_id, "failed", notes=f"exit {r.returncode}")
        return False
    state.update_project(project_key, last_curator_run=state.now_iso())
    state.finish_run(run_id, "ok", notes="claude.md regen")
    log.info("regen-claude[%s] done", project_key)
    return True


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def cycle_once(log: logging.Logger, model: str, curator_age_seconds: int) -> None:
    state.init_db()
    log.info("─── cycle start ───")
    _ingest_corpus(log)

    projects = [p for p in state.list_projects() if p.get("enabled", 1)]
    if not projects:
        log.info("no enabled tracked projects, nothing to do")
        return

    for p in projects:
        key = p["project_key"]
        progress = state.get_project_progress(key)
        if progress.get("error"):
            log.warning("[%s] %s — skipping", key, progress["error"])
            continue

        new_prompts = progress.get("new_prompts_since_last_analysis", 0)
        threshold = p.get("threshold_new_prompts", 30)
        log.info("[%s] new_prompts=%s threshold=%s", key, new_prompts, threshold)

        analyst_ran = False
        if new_prompts >= threshold:
            analyst_ran = _run_analyst(key, model, log)
        else:
            log.info("[%s] below threshold — skipping analyst", key)

        # Regen CLAUDE.md if analyst ran successfully OR if last regen is older than threshold
        last_curator = _parse_iso(p.get("last_curator_run"))
        too_old = (
            last_curator is None
            or (datetime.now(timezone.utc) - last_curator).total_seconds() > curator_age_seconds
        )
        # Only regen if there's already a skills/ directory (otherwise it'll fail in stage 3)
        skills_dir = ROOT / "kai_claude" / key / "skills"
        has_bundles = skills_dir.exists() and any(d.is_dir() for d in skills_dir.iterdir())
        if (analyst_ran or too_old) and has_bundles:
            _regen_claude_md(key, model, log)
        elif (analyst_ran or too_old) and not has_bundles:
            log.info("[%s] no skill bundles yet — skip regen-claude (run `watchmen curate %s` first)", key, key)

    log.info("─── cycle end ───\n")


def run(args) -> int:
    log = setup_logging(Path(args.log_file).expanduser())
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    log.info("watchmen daemon starting (interval=%ss, model=%s, curator_age=%ss, log=%s)",
             args.interval, args.model, args.curator_age, args.log_file)
    log.info("pid=%d cwd=%s", os.getpid(), os.getcwd())

    if args.once:
        try:
            cycle_once(log, args.model, args.curator_age)
        except Exception as e:
            log.exception("cycle failed: %s", e)
            return 1
        return 0

    while not _shutdown:
        try:
            cycle_once(log, args.model, args.curator_age)
        except Exception as e:
            log.exception("cycle failed: %s", e)

        # Sleep with shutdown polling
        slept = 0
        while slept < args.interval and not _shutdown:
            time.sleep(min(5, args.interval - slept))
            slept += 5

    log.info("watchmen daemon shutting down")
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="run one cycle and exit (testing)")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL, help=f"seconds between cycles (default {DEFAULT_INTERVAL})")
    parser.add_argument("--curator-age", type=int, default=DEFAULT_CURATOR_AGE, help="regen CLAUDE.md if last one older than this many seconds")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--log-file", default=str(DEFAULT_LOG))
    args = parser.parse_args()
    sys.exit(run(args))


if __name__ == "__main__":
    main()
