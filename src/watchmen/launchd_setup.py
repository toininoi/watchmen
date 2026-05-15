"""launchd plist generator + installer for watchmen daemon and viewer.

Writes plists to ~/Library/LaunchAgents/, then `launchctl load` them.
User-level agents — no sudo required, runs only when user is logged in.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"
LOG_DIR = Path.home() / "Library" / "Logs"

DAEMON_LABEL = "co.firstbatch.watchmen.daemon"
VIEWER_LABEL = "co.firstbatch.watchmen.viewer"


def _which_uv() -> str | None:
    return shutil.which("uv")


def _plist(label: str, args: list[str], stdout_log: Path, stderr_log: Path, working_dir: Path, run_at_load: bool = True, keep_alive: bool = True) -> str:
    args_xml = "\n".join(f"      <string>{a}</string>" for a in args)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
        "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
{args_xml}
    </array>
    <key>WorkingDirectory</key>
    <string>{working_dir}</string>
    <key>RunAtLoad</key>
    <{"true" if run_at_load else "false"}/>
    <key>KeepAlive</key>
    <{"true" if keep_alive else "false"}/>
    <key>StandardOutPath</key>
    <string>{stdout_log}</string>
    <key>StandardErrorPath</key>
    <string>{stderr_log}</string>
    <key>EnvironmentVariables</key>
    <dict>
      <key>PATH</key>
      <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
      <key>HOME</key>
      <string>{Path.home()}</string>
    </dict>
  </dict>
</plist>
"""


def _check_uv() -> str:
    uv = _which_uv()
    if not uv:
        print("ERROR: uv not found in PATH. Install uv first.")
        sys.exit(1)
    return uv


def _plist_path(label: str) -> Path:
    return LAUNCH_AGENTS / f"{label}.plist"


def _is_loaded(label: str) -> bool:
    r = subprocess.run(["launchctl", "list", label], capture_output=True, text=True)
    return r.returncode == 0


def _bootstrap(label: str, plist_path: Path) -> int:
    """Use launchctl bootstrap if available (modern), fall back to load (older)."""
    uid = os.getuid()
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)],
                       capture_output=True, text=True)
    if r.returncode == 0:
        return 0
    # Fall back
    return subprocess.run(["launchctl", "load", "-w", str(plist_path)]).returncode


def _bootout(label: str, plist_path: Path) -> int:
    uid = os.getuid()
    r = subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"],
                       capture_output=True, text=True)
    if r.returncode == 0:
        return 0
    return subprocess.run(["launchctl", "unload", str(plist_path)]).returncode


# ─── Public commands (called by cli.py) ─────────────────────────────────────


def install_daemon(model: str = "deepseek/deepseek-v4-flash", interval: int = 7200, dry_run: bool = False) -> int:
    LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    uv = _check_uv()

    plist = _plist(
        label=DAEMON_LABEL,
        args=[uv, "run", "watchmen", "daemon", "run", "--interval", str(interval), "--model", model],
        stdout_log=LOG_DIR / "watchmen.daemon.out.log",
        stderr_log=LOG_DIR / "watchmen.daemon.err.log",
        working_dir=ROOT,
        keep_alive=True,
    )

    target = _plist_path(DAEMON_LABEL)
    if dry_run:
        print(f"--- WOULD WRITE TO {target} ---\n{plist}")
        return 0

    if _is_loaded(DAEMON_LABEL):
        print(f"unloading existing {DAEMON_LABEL}")
        _bootout(DAEMON_LABEL, target)

    target.write_text(plist)
    print(f"wrote: {target}")
    rc = _bootstrap(DAEMON_LABEL, target)
    if rc == 0:
        print(f"loaded: {DAEMON_LABEL}")
        print(f"logs: {LOG_DIR}/watchmen.daemon.{{out,err}}.log + ~/Library/Logs/watchmen.log")
    else:
        print(f"WARNING: launchctl load returned {rc}. The plist is on disk but may not be active.")
    return rc


def install_viewer(host: str | None = None, port: int | None = None, dry_run: bool = False) -> int:
    from watchmen import config
    host = host or config.VIEWER_DEFAULT_HOST
    port = port if port is not None else config.viewer_port()
    LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    uv = _check_uv()

    plist = _plist(
        label=VIEWER_LABEL,
        args=[uv, "run", "watchmen", "viewer", "run", "--host", host, "--port", str(port)],
        stdout_log=LOG_DIR / "watchmen.viewer.out.log",
        stderr_log=LOG_DIR / "watchmen.viewer.err.log",
        working_dir=ROOT,
        keep_alive=True,
    )

    target = _plist_path(VIEWER_LABEL)
    if dry_run:
        print(f"--- WOULD WRITE TO {target} ---\n{plist}")
        return 0

    if _is_loaded(VIEWER_LABEL):
        print(f"unloading existing {VIEWER_LABEL}")
        _bootout(VIEWER_LABEL, target)
    target.write_text(plist)
    print(f"wrote: {target}")
    rc = _bootstrap(VIEWER_LABEL, target)
    if rc == 0:
        print(f"loaded: {VIEWER_LABEL}")
        print(f"viewer running at http://{host}:{port}")
    else:
        print(f"WARNING: launchctl load returned {rc}.")
    return rc


def uninstall_daemon() -> int:
    target = _plist_path(DAEMON_LABEL)
    if not target.exists():
        print(f"not installed: {target}")
        return 0
    if _is_loaded(DAEMON_LABEL):
        _bootout(DAEMON_LABEL, target)
        print(f"unloaded: {DAEMON_LABEL}")
    target.unlink()
    print(f"removed: {target}")
    return 0


def uninstall_viewer() -> int:
    target = _plist_path(VIEWER_LABEL)
    if not target.exists():
        print(f"not installed: {target}")
        return 0
    if _is_loaded(VIEWER_LABEL):
        _bootout(VIEWER_LABEL, target)
        print(f"unloaded: {VIEWER_LABEL}")
    target.unlink()
    print(f"removed: {target}")
    return 0


def status() -> int:
    print("watchmen launchd status:")
    for label in (DAEMON_LABEL, VIEWER_LABEL):
        plist_path = _plist_path(label)
        on_disk = plist_path.exists()
        loaded = _is_loaded(label)
        marker = "✓" if loaded else ("○" if on_disk else "·")
        print(f"  {marker} {label}  on_disk={on_disk}  loaded={loaded}  path={plist_path}")
    return 0
