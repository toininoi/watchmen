"""User-facing presentation helpers — ANSI colors, sparklines, Rich wrappers.

These were originally inline in cli.py and pulled out during the Phase 3
split (handover refactor). The contract is intentionally narrow:

  - No imports from other watchmen modules except paths (for the runtime
    state error message).
  - No I/O side effects in pure helpers — callers pass in consoles, paths,
    or buffers and the helpers return strings.
  - Returns plain ANSI-escaped strings for the simple color helpers so
    they compose with `print()`. Rich-formatted variants are separate.

The thematic Watchmen helpers (Doomsday Clock, Manhattan, Rorschach) stay
in cli.py because they reference command-specific constants and dispatch
state. They could move later but the seam isn't worth fighting for today.
"""

from __future__ import annotations

from pathlib import Path


# ─── ANSI color helpers ────────────────────────────────────────────────────


def bold(s: str) -> str:
    return f"\033[1m{s}\033[0m"


def dim(s: str) -> str:
    return f"\033[90m{s}\033[0m"


def green(s: str) -> str:
    return f"\033[32m{s}\033[0m"


def yellow(s: str) -> str:
    return f"\033[33m{s}\033[0m"


def red(s: str) -> str:
    return f"\033[31m{s}\033[0m"


def bright_blue(s: str) -> str:
    return f"\033[94m{s}\033[0m"


def cyan(s: str) -> str:
    return f"\033[36m{s}\033[0m"


# ─── Path display ──────────────────────────────────────────────────────────


def short_path(path: str | Path) -> str:
    """Render a path with $HOME collapsed to ~. Purely cosmetic — used in
    diagnostics and table output so absolute paths don't blow the terminal
    width."""
    text = str(path)
    home = str(Path.home())
    return text.replace(home, "~", 1) if text.startswith(home) else text


# ─── TUI visualization (no external chart deps) ────────────────────────────
# Unicode block characters used for compact bar charts + sparklines. Both
# helpers degrade gracefully when their input is empty or all-zero, returning
# empty strings rather than ZeroDivisionError — callers can render empty
# cells without an extra null-check.


_SPARK_BLOCKS = "▁▂▃▄▅▆▇█"


def sparkline(values: list[float]) -> str:
    """Compact one-character-per-data-point trend line. Auto-scales to the
    max value in the series — useful for showing daily cost or session counts
    over a 30-day window in 30 visible characters."""
    if not values:
        return ""
    peak = max(values)
    if peak <= 0:
        return _SPARK_BLOCKS[0] * len(values)
    return "".join(_SPARK_BLOCKS[min(7, int((v / peak) * 7))] for v in values)


def bar(value: float, max_value: float, width: int = 30) -> str:
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


# ─── Rich-based status / header helpers ────────────────────────────────────


def ui_header(console, command: str, subtitle: str | None = None) -> None:
    """Standard `watchmen <command>` header used at the top of long-running
    operations (status, doctor, init). Console is a rich.console.Console."""
    console.print(f"[bold]watchmen[/] [dim]{command}[/]")
    if subtitle:
        console.print(f"[dim]{subtitle}[/]")


def rich_status(status: str) -> str:
    """Map a state.runs.status string to a Rich-formatted badge."""
    if status == "ok":
        return "[green]ok[/]"
    if status == "running":
        return "[yellow]running[/]"
    if status in {"failed", "error"}:
        return "[red]failed[/]"
    return f"[dim]{status or '-'}[/]"


def print_runtime_state_error(state_db_path: Path, exc: BaseException, *, stderr: bool = True) -> None:
    """Friendly diagnostic when state.db can't be opened. Common cause: the
    user installed watchmen system-wide but `~/.watchmen/` isn't writable
    (root install, sandboxed env, etc.). We point at the env var escape
    hatch rather than letting argparse dump a raw sqlite3 traceback."""
    from rich.console import Console

    console = Console(stderr=stderr)
    console.print("[red]watchmen cannot open its local state database.[/]")
    console.print(f"[dim]path:[/] {state_db_path}")
    console.print(f"[dim]error:[/] {type(exc).__name__}: {exc}")
    console.print()
    console.print("[dim]Try setting a writable data directory:[/]")
    console.print("  WATCHMEN_HOME=/path/to/watchmen-data watchmen status")


def render_file(path: Path, raw: bool = False) -> None:
    """Pretty-print a file based on extension. `.md` → Rich Markdown render
    (headers/code/tables stylized). `.json` → Rich JSON with syntax colors.
    Anything else → plain text. `--raw` forces plain text — important when
    piping output to a file (Rich already strips ANSI when stdout isn't a
    tty, but `--raw` is the explicit, scriptable opt-out)."""
    import sys

    text = path.read_text()
    if raw:
        sys.stdout.write(text)
        return
    print(dim(f"# {path.name}\n"))
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
