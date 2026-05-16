"""Task Scheduler unit generator + installer for watchmen daemon and viewer.

Writes two XML task definitions (`Watchmen\\Daemon`, `Watchmen\\Viewer`) and
registers them via `schtasks /Create /XML`. Both trigger on user logon,
restart on failure (3 attempts, 1-minute backoff), and run hidden — same
lifecycle guarantees as the launchd plists on macOS and the systemd --user
units on Linux.

The `schtasks` CLI ships with Windows itself, so this stays pure stdlib
like the other two backends — no pywin32 dependency.

Logs land in `%LOCALAPPDATA%\\watchmen\\logs\\`.
"""

from __future__ import annotations

import getpass
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent

LOG_DIR = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "watchmen" / "logs"

DAEMON_LABEL = "Watchmen\\Daemon"
VIEWER_LABEL = "Watchmen\\Viewer"


def _which_uv() -> str | None:
    return shutil.which("uv") or shutil.which("uv.exe")


def _check_uv() -> str:
    uv = _which_uv()
    if not uv:
        print("ERROR: uv not found in PATH. Install uv first: https://docs.astral.sh/uv/")
        sys.exit(1)
    return uv


def _check_schtasks() -> None:
    if shutil.which("schtasks") is None and shutil.which("schtasks.exe") is None:
        print("ERROR: schtasks not found. Windows daemon support requires Task Scheduler.")
        sys.exit(1)


def _xml_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _user_id() -> str:
    """Identifier acceptable to schtasks XML <UserId> field — DOMAIN\\user or
    just the username. Falls back to %USERNAME% env var if the API call
    fails (e.g., running as SYSTEM with no console attached)."""
    domain = os.environ.get("USERDOMAIN")
    user = os.environ.get("USERNAME") or getpass.getuser()
    if domain and user:
        return f"{domain}\\{user}"
    return user


def _task_xml(description: str, command_line: str, working_dir: Path) -> str:
    """Build a Task Scheduler XML payload.

    - Triggers on logon for the current user.
    - Restarts on failure with a 60-second backoff, up to 3 attempts.
    - Runs hidden — no console window pops up.
    - Uses InteractiveToken so it has the user's environment (HOME etc).
    """
    user = _xml_escape(_user_id())
    desc = _xml_escape(description)
    cmd = _xml_escape(command_line)
    wd = _xml_escape(str(working_dir))
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{desc}</Description>
    <URI>\\Watchmen\\Task</URI>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{user}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>true</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <DisallowStartOnRemoteAppSession>false</DisallowStartOnRemoteAppSession>
    <UseUnifiedSchedulingEngine>true</UseUnifiedSchedulingEngine>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>cmd.exe</Command>
      <Arguments>{cmd}</Arguments>
      <WorkingDirectory>{wd}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _wrap_cmd(exec_args: list[str], stdout_log: Path, stderr_log: Path) -> str:
    """Wrap the watchmen invocation in a cmd /c line that redirects output.

    Quoting: each arg is wrapped in double-quotes if it contains a space.
    Task Scheduler invokes cmd.exe with the <Arguments> string verbatim, so
    the redirection (`>>` / `2>>`) must live inside the cmd /c payload."""
    def q(a: str) -> str:
        if any(c in a for c in (' ', '\t')):
            return f'"{a}"'
        return a
    cmd_payload = " ".join(q(a) for a in exec_args)
    # /d skips AutoRun, /s + extra quotes lets us nest redirection safely.
    return f'/d /s /c "{cmd_payload} >>"{stdout_log}" 2>>"{stderr_log}""'


def _schtasks_run(args: list[str], *, capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["schtasks"] + args, capture_output=capture, text=True)


def _task_exists(label: str) -> bool:
    r = _schtasks_run(["/Query", "/TN", label])
    return r.returncode == 0


def _task_running(label: str) -> bool:
    r = _schtasks_run(["/Query", "/TN", label, "/FO", "LIST", "/V"])
    if r.returncode != 0:
        return False
    for line in r.stdout.splitlines():
        if line.strip().lower().startswith("status:"):
            value = line.split(":", 1)[1].strip().lower()
            return value == "running"
    return False


def is_daemon_loaded() -> bool:
    return _task_exists(DAEMON_LABEL)


def is_viewer_loaded() -> bool:
    return _task_exists(VIEWER_LABEL)


def _install_task(label: str, xml: str) -> int:
    # schtasks /Create wants the XML in a file. UTF-16 LE with BOM, per the
    # XML declaration above. tempfile gives us a unique path; we keep
    # delete=False so schtasks can re-open it on Windows.
    fd, tmp_path = tempfile.mkstemp(suffix=".xml", prefix="watchmen-")
    os.close(fd)
    try:
        Path(tmp_path).write_text(xml, encoding="utf-16")
        r = _schtasks_run(["/Create", "/TN", label, "/XML", tmp_path, "/F"])
        if r.returncode != 0:
            msg = (r.stderr or r.stdout or "").strip()
            print(f"WARNING: `schtasks /Create /TN {label}` returned {r.returncode}: {msg}")
            return r.returncode
        print(f"loaded: {label}")
        # Kick it off now so the user doesn't have to log out and back in.
        run = _schtasks_run(["/Run", "/TN", label])
        if run.returncode != 0:
            msg = (run.stderr or run.stdout or "").strip()
            print(f"  (could not start immediately: {msg.splitlines()[0] if msg else ''})")
        return 0
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _uninstall_task(label: str) -> int:
    if not _task_exists(label):
        print(f"not installed: {label}")
        return 0
    # Stop first so a hung process doesn't block deletion.
    _schtasks_run(["/End", "/TN", label])
    r = _schtasks_run(["/Delete", "/TN", label, "/F"])
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip()
        print(f"WARNING: `schtasks /Delete /TN {label}` returned {r.returncode}: {msg}")
        return r.returncode
    print(f"unloaded: {label}")
    return 0


# ─── Public commands (called via watchmen.service) ─────────────────────────


def install_daemon(model: str = "deepseek/deepseek-v4-flash", interval: int = 7200, dry_run: bool = False) -> int:
    uv = _which_uv() or "uv.exe"
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    cmd_args = [uv, "run", "watchmen", "daemon", "run", "--interval", str(interval), "--model", model]
    wrapped = _wrap_cmd(cmd_args, LOG_DIR / "daemon.out.log", LOG_DIR / "daemon.err.log")
    xml = _task_xml(
        description="Watchmen daemon — analyzes Claude Code sessions",
        command_line=wrapped,
        working_dir=ROOT,
    )

    if dry_run:
        print(f"--- WOULD INSTALL TASK {DAEMON_LABEL} ---\n{xml}")
        return 0

    _check_schtasks()
    _check_uv()
    rc = _install_task(DAEMON_LABEL, xml)
    if rc == 0:
        print(f"logs: {LOG_DIR}\\daemon.{{out,err}}.log")
    return rc


def install_viewer(host: str | None = None, port: int | None = None, dry_run: bool = False) -> int:
    from watchmen import config
    host = host or config.VIEWER_DEFAULT_HOST
    port = port if port is not None else config.viewer_port()
    uv = _which_uv() or "uv.exe"
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    cmd_args = [uv, "run", "watchmen", "viewer", "run", "--host", host, "--port", str(port)]
    wrapped = _wrap_cmd(cmd_args, LOG_DIR / "viewer.out.log", LOG_DIR / "viewer.err.log")
    xml = _task_xml(
        description="Watchmen viewer — local insights UI",
        command_line=wrapped,
        working_dir=ROOT,
    )

    if dry_run:
        print(f"--- WOULD INSTALL TASK {VIEWER_LABEL} ---\n{xml}")
        return 0

    _check_schtasks()
    _check_uv()
    rc = _install_task(VIEWER_LABEL, xml)
    if rc == 0:
        print(f"viewer running at http://{host}:{port}")
        print(f"logs: {LOG_DIR}\\viewer.{{out,err}}.log")
    return rc


def uninstall_daemon() -> int:
    _check_schtasks()
    return _uninstall_task(DAEMON_LABEL)


def uninstall_viewer() -> int:
    _check_schtasks()
    return _uninstall_task(VIEWER_LABEL)


def status() -> int:
    _check_schtasks()
    print("watchmen Task Scheduler status:")
    for label in (DAEMON_LABEL, VIEWER_LABEL):
        installed = _task_exists(label)
        running = _task_running(label) if installed else False
        marker = "✓" if running else ("○" if installed else "·")
        print(f"  {marker} {label}  installed={installed}  running={running}")
    print(f"logs: {LOG_DIR}")
    return 0
