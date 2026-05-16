"""Cross-platform daemon/viewer service installer.

Dispatches to the platform-native scheduler. Each backend is named after its
underlying component, not after the OS:

  - macOS   → launchd_setup    (~/Library/LaunchAgents/*.plist)
  - Linux   → systemd_setup    (~/.config/systemd/user/*.service)
  - Windows → schtasks_setup   (Task Scheduler XML registered via schtasks)

Public API mirrors what cli.py needs:
  install_daemon / install_viewer / uninstall_daemon / uninstall_viewer
  status / is_daemon_loaded / is_viewer_loaded

`BACKEND_NAME` is exported so UI text can say "(launchd)", "(systemd)", or
"(schtasks)" contextually without each caller doing its own platform sniff.
"""

import platform
from typing import Any


def _backend() -> Any:
    system = platform.system()
    if system == "Darwin":
        from watchmen import launchd_setup
        return launchd_setup
    if system == "Linux":
        from watchmen import systemd_setup
        return systemd_setup
    if system == "Windows":
        from watchmen import schtasks_setup
        return schtasks_setup
    raise RuntimeError(
        f"watchmen daemon/viewer install is not supported on {system}. "
        "Supported schedulers: launchd (macOS), systemd --user (Linux), Task Scheduler (Windows)."
    )


def _backend_name() -> str:
    system = platform.system()
    if system == "Darwin":
        return "launchd"
    if system == "Linux":
        return "systemd"
    if system == "Windows":
        return "schtasks"
    return system.lower() or "unknown"


BACKEND_NAME = _backend_name()


def install_daemon(model: str = "deepseek/deepseek-v4-flash", interval: int = 7200, dry_run: bool = False) -> int:
    return _backend().install_daemon(model=model, interval=interval, dry_run=dry_run)


def install_viewer(host: str | None = None, port: int | None = None, dry_run: bool = False) -> int:
    return _backend().install_viewer(host=host, port=port, dry_run=dry_run)


def uninstall_daemon() -> int:
    return _backend().uninstall_daemon()


def uninstall_viewer() -> int:
    return _backend().uninstall_viewer()


def status() -> int:
    return _backend().status()


def is_daemon_loaded() -> bool:
    try:
        return bool(_backend().is_daemon_loaded())
    except Exception:
        return False


def is_viewer_loaded() -> bool:
    try:
        return bool(_backend().is_viewer_loaded())
    except Exception:
        return False
