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


def install_daemon(model: str | None = None, interval: int = 7200, dry_run: bool = False) -> int:
    # Provider-aware default — the daemon plist bakes whatever model is
    # current for the user's active provider at install time. Reinstall
    # after switching provider to refresh.
    if model is None:
        from watchmen import config
        model = config.default_model()
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


def notify_settings_changed(what: str, *, interactive: bool = False) -> bool:
    """Tell the user the daemon's baked configuration is now stale.

    Daemon scheduler units (launchd plist / systemd service / Task
    Scheduler XML) embed the model name at install time. Changing provider
    or model via `watchmen settings ...` doesn't propagate to the daemon
    until the user reinstalls it. Easy to forget; this surfaces the gap.

    `interactive=True` (the CLI + menu paths) offers a y/N reinstall right
    now. `interactive=False` (web viewer, programmatic callers) just prints
    a one-line note. Returns True iff a reinstall was actually performed.
    """
    import sys
    if not is_daemon_loaded():
        return False

    msg = (
        f"[yellow]![/] daemon was installed with a different {what}. "
        f"It keeps using the previous {what} until reinstalled."
    )
    if not interactive or not sys.stdin.isatty():
        # Programmatic path: leave a breadcrumb, let the user act later.
        try:
            from rich.console import Console
            Console().print(msg)
            Console().print("  [dim]reinstall with: watchmen daemon install[/]")
        except Exception:
            # Even if rich isn't available, fall back to plain print so the
            # notice still surfaces. The auto-prompt notice is too useful
            # to suppress silently on an import-time hiccup.
            print(f"! daemon was installed with a different {what}. Run: watchmen daemon install")
        return False

    from rich.console import Console
    console = Console()
    console.print(msg)
    try:
        choice = input("  Reinstall daemon now to pick it up? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if choice not in ("y", "yes"):
        console.print("  [dim]skipped — `watchmen daemon install` when ready[/]")
        return False

    rc = install_daemon()  # picks up new model via config.default_model()
    if rc == 0:
        console.print("[green]✓[/] daemon reinstalled")
        return True
    console.print(f"[red]✗[/] daemon reinstall failed (exit {rc})")
    return False
