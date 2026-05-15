"""systemd --user unit generator + installer for watchmen daemon and viewer.

Writes units to $XDG_CONFIG_HOME/systemd/user/ (default: ~/.config/systemd/user/),
then `systemctl --user enable --now` them. No sudo — user-level units.

Note: by default user services only run while the user is logged in. To keep
the daemon running after logout, run `loginctl enable-linger $USER` once.
"""

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent

SYSTEMD_USER_DIR = (
    Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "systemd" / "user"
)
LOG_DIR = Path.home() / ".watchmen" / "logs"

DAEMON_LABEL = "watchmen-daemon.service"
VIEWER_LABEL = "watchmen-viewer.service"


def _which_uv() -> str | None:
    return shutil.which("uv")


def _check_uv() -> str:
    uv = _which_uv()
    if not uv:
        print("ERROR: uv not found in PATH. Install uv first: https://docs.astral.sh/uv/")
        sys.exit(1)
    return uv


def _check_systemctl() -> None:
    if shutil.which("systemctl") is None:
        print("ERROR: systemctl not found. Linux daemon support requires systemd.")
        sys.exit(1)


def _unit(description: str, exec_start: list[str], stdout_log: Path, stderr_log: Path, working_dir: Path) -> str:
    exec_quoted = " ".join(shlex.quote(a) for a in exec_start)
    extra_path = f"{Path.home()}/.local/bin"
    return f"""[Unit]
Description={description}
After=network.target

[Service]
Type=simple
ExecStart={exec_quoted}
WorkingDirectory={working_dir}
Restart=on-failure
RestartSec=10
StandardOutput=append:{stdout_log}
StandardError=append:{stderr_log}
Environment=PATH=/usr/local/bin:/usr/bin:/bin:{extra_path}
Environment=HOME={Path.home()}

[Install]
WantedBy=default.target
"""


def _unit_path(label: str) -> Path:
    return SYSTEMD_USER_DIR / label


def _is_active(label: str) -> bool:
    r = subprocess.run(
        ["systemctl", "--user", "is-active", label],
        capture_output=True,
        text=True,
    )
    return r.stdout.strip() == "active"


def _is_enabled(label: str) -> bool:
    r = subprocess.run(
        ["systemctl", "--user", "is-enabled", label],
        capture_output=True,
        text=True,
    )
    return r.stdout.strip() in {"enabled", "static"}


def _is_loaded(label: str) -> bool:
    """Alias of _is_active — matches launchd_setup naming so call sites can be
    symmetric across backends."""
    return _is_active(label)


def is_daemon_loaded() -> bool:
    return _is_active(DAEMON_LABEL)


def is_viewer_loaded() -> bool:
    return _is_active(VIEWER_LABEL)


def _daemon_reload() -> None:
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)


def _enable_and_start(label: str) -> int:
    _daemon_reload()
    r = subprocess.run(
        ["systemctl", "--user", "enable", "--now", label],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip()
        print(f"WARNING: `systemctl --user enable --now {label}` returned {r.returncode}: {msg}")
    return r.returncode


def _disable_and_stop(label: str) -> int:
    r = subprocess.run(
        ["systemctl", "--user", "disable", "--now", label],
        capture_output=True,
        text=True,
    )
    return r.returncode


def _linger_hint() -> str:
    user = os.environ.get("USER") or ""
    if not user:
        try:
            user = os.getlogin()
        except OSError:
            user = "$USER"
    return f"to keep running after logout: sudo loginctl enable-linger {user}"


# ─── Public commands (called via watchmen.service) ─────────────────────────


def install_daemon(model: str = "deepseek/deepseek-v4-flash", interval: int = 7200, dry_run: bool = False) -> int:
    uv = _which_uv() or "uv" if dry_run else _check_uv()

    unit_text = _unit(
        description="Watchmen daemon — analyzes Claude Code sessions",
        exec_start=[uv, "run", "watchmen", "daemon", "run", "--interval", str(interval), "--model", model],
        stdout_log=LOG_DIR / "daemon.out.log",
        stderr_log=LOG_DIR / "daemon.err.log",
        working_dir=ROOT,
    )

    target = _unit_path(DAEMON_LABEL)
    if dry_run:
        print(f"--- WOULD WRITE TO {target} ---\n{unit_text}")
        return 0

    _check_systemctl()
    SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    if _is_active(DAEMON_LABEL):
        print(f"stopping existing {DAEMON_LABEL}")
        _disable_and_stop(DAEMON_LABEL)

    target.write_text(unit_text)
    print(f"wrote: {target}")
    rc = _enable_and_start(DAEMON_LABEL)
    if rc == 0:
        print(f"loaded: {DAEMON_LABEL}")
        print(f"logs: {LOG_DIR}/daemon.{{out,err}}.log")
        print(_linger_hint())
    return rc


def install_viewer(host: str | None = None, port: int | None = None, dry_run: bool = False) -> int:
    from watchmen import config
    host = host or config.VIEWER_DEFAULT_HOST
    port = port if port is not None else config.viewer_port()
    uv = _which_uv() or "uv" if dry_run else _check_uv()

    unit_text = _unit(
        description="Watchmen viewer — local insights UI",
        exec_start=[uv, "run", "watchmen", "viewer", "run", "--host", host, "--port", str(port)],
        stdout_log=LOG_DIR / "viewer.out.log",
        stderr_log=LOG_DIR / "viewer.err.log",
        working_dir=ROOT,
    )

    target = _unit_path(VIEWER_LABEL)
    if dry_run:
        print(f"--- WOULD WRITE TO {target} ---\n{unit_text}")
        return 0

    _check_systemctl()
    SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    if _is_active(VIEWER_LABEL):
        print(f"stopping existing {VIEWER_LABEL}")
        _disable_and_stop(VIEWER_LABEL)

    target.write_text(unit_text)
    print(f"wrote: {target}")
    rc = _enable_and_start(VIEWER_LABEL)
    if rc == 0:
        print(f"loaded: {VIEWER_LABEL}")
        print(f"viewer running at http://{host}:{port}")
        print(_linger_hint())
    return rc


def uninstall_daemon() -> int:
    _check_systemctl()
    target = _unit_path(DAEMON_LABEL)
    if not target.exists() and not _is_enabled(DAEMON_LABEL):
        print(f"not installed: {target}")
        return 0
    if _is_active(DAEMON_LABEL) or _is_enabled(DAEMON_LABEL):
        _disable_and_stop(DAEMON_LABEL)
        print(f"unloaded: {DAEMON_LABEL}")
    if target.exists():
        target.unlink()
        print(f"removed: {target}")
    _daemon_reload()
    return 0


def uninstall_viewer() -> int:
    _check_systemctl()
    target = _unit_path(VIEWER_LABEL)
    if not target.exists() and not _is_enabled(VIEWER_LABEL):
        print(f"not installed: {target}")
        return 0
    if _is_active(VIEWER_LABEL) or _is_enabled(VIEWER_LABEL):
        _disable_and_stop(VIEWER_LABEL)
        print(f"unloaded: {VIEWER_LABEL}")
    if target.exists():
        target.unlink()
        print(f"removed: {target}")
    _daemon_reload()
    return 0


def status() -> int:
    _check_systemctl()
    print("watchmen systemd --user status:")
    for label in (DAEMON_LABEL, VIEWER_LABEL):
        path = _unit_path(label)
        on_disk = path.exists()
        active = _is_active(label)
        enabled = _is_enabled(label)
        marker = "✓" if active else ("○" if on_disk else "·")
        print(f"  {marker} {label}  on_disk={on_disk}  active={active}  enabled={enabled}  path={path}")
    return 0
