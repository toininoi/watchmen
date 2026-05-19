"""Hook installer — wires the watchmen observer into the hook config of every
supported coding agent so each session pipes its hook events to the local
observer at 127.0.0.1:8765.

Supported targets:

  - Claude Code  → ~/.claude/settings.json  (under a top-level "hooks" key)
  - Codex CLI    → ~/.codex/hooks.json      (also under a top-level "hooks" key)

Both formats share the same per-event schema (matcher groups containing
{type, command} handlers), so one installer covers both. Codex supports a
subset of Claude Code's events (no SessionEnd / SubagentStop / Notification /
PreCompact) — entries for unsupported events are filtered per target.

Two hook scripts ship — `watchmen_observe.sh` for POSIX shells and
`watchmen_observe.ps1` for PowerShell — and the installer picks the one
that matches the host's native shell.

Backs up the existing config before mutating. Idempotent + self-healing:
re-running install scrubs any existing watchmen entries (matched by script
filename so a reorg or moved checkout doesn't leave orphaned entries that fail
with "No such file or directory" on every event) and then writes the canonical
set fresh.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).parent

# Each platform invokes the settings.json "command" field through its native
# shell (bash/zsh on POSIX, cmd.exe on Windows), so the hook script matches.
_HOOK_SCRIPTS = {
    "win32":  ROOT / "hooks" / "watchmen_observe.ps1",
    "posix":  ROOT / "hooks" / "watchmen_observe.sh",
}
HOOK_SCRIPT = (_HOOK_SCRIPTS["win32"] if sys.platform == "win32" else _HOOK_SCRIPTS["posix"]).resolve()

# Kept for backwards compatibility with callers/tests that referenced the
# Claude-Code-only path directly. The canonical surface is HOSTS.
SETTINGS_FILE = Path.home() / ".claude" / "settings.json"

# All hook scripts watchmen installs. Keys used internally; values are absolute paths.
WATCHMEN_SCRIPTS: dict[str, Path] = {
    "observe": HOOK_SCRIPT,
}


def _settings_command_for(script_path: Path) -> str:
    """Render the `command` string that goes into settings.json.

    For .sh scripts the shebang dispatches to bash, so the path alone is
    enough. For .ps1 scripts we wrap with `powershell -NoProfile
    -ExecutionPolicy Bypass -File "..."` so cmd.exe can launch them without
    the user having to lower their execution policy globally. The path is
    quoted because user-profile dirs commonly contain spaces."""
    if script_path.suffix.lower() == ".ps1":
        return f'powershell -NoProfile -ExecutionPolicy Bypass -File "{script_path}"'
    return str(script_path)

# Per-event: list of (script_key, matcher_or_None). matcher=None omits the matcher key
# entirely (Claude Code treats absent matcher as "all"); matcher="" means an explicit
# empty matcher (kept for PreToolUse/PostToolUse, which canonically use the matcher field).
WATCHMEN_HOOKS: dict[str, list[tuple[str, str | None]]] = {
    "PreToolUse":       [("observe", "")],
    "PostToolUse":      [("observe", "")],
    "SessionStart":     [("observe", None)],
    "SessionEnd":       [("observe", None)],
    "UserPromptSubmit": [("observe", None)],
    "Stop":             [("observe", None)],
    "SubagentStop":     [("observe", None)],
    "Notification":     [("observe", None)],
    "PreCompact":       [("observe", None)],
}

# Codex CLI only exposes a subset of lifecycle events (per OpenAI docs:
# SessionStart, PreToolUse, PostToolUse, UserPromptSubmit, Stop, PermissionRequest).
# Anything else we'd try to write would be silently ignored, so filter at install
# time to avoid the appearance of wiring something we don't actually own.
_CODEX_SUPPORTED_EVENTS: frozenset[str] = frozenset({
    "SessionStart", "PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop",
})

# Every basename watchmen has ever shipped or shipped-and-retired. Used as a
# whole-set filename match against settings.json entries so install / uninstall
# can clean up stale paths from older installs (e.g. before the src-layout
# move, when HOOK_SCRIPT resolved to ~/dev/watchmen/hooks/watchmen_observe.sh
# rather than ~/dev/watchmen/src/watchmen/hooks/watchmen_observe.sh).
# Add new script names here; mark retired ones with a comment so the list
# doubles as a history of every hook surface we've owned.
WATCHMEN_SCRIPT_NAMES: set[str] = {
    "watchmen_observe.sh",
    "watchmen_observe.ps1",
    "watchmen_brief.sh",  # retired in 0.2.0
}


@dataclass(frozen=True)
class _Host:
    """One install target. Two today — Claude Code + Codex — both share the
    same per-event schema, only the file path and supported-event set differ."""
    name: str          # short display name used in print()
    settings_path_attr: str  # module attribute name → Path (so tests can monkeypatch one host)
    supported_events: frozenset[str] | None  # None = all WATCHMEN_HOOKS events


# Module-level attributes (not just dataclass fields) so tests can rebind one
# host's path without rebuilding the whole HOSTS tuple.
CLAUDE_SETTINGS_FILE = SETTINGS_FILE
CODEX_SETTINGS_FILE = Path.home() / ".codex" / "hooks.json"

HOSTS: tuple[_Host, ...] = (
    _Host(name="Claude Code", settings_path_attr="CLAUDE_SETTINGS_FILE",
          supported_events=None),
    _Host(name="Codex",       settings_path_attr="CODEX_SETTINGS_FILE",
          supported_events=_CODEX_SUPPORTED_EVENTS),
)


def _host_path(host: _Host) -> Path:
    return globals()[host.settings_path_attr]


def _is_watchmen_hook_cmd(cmd: str) -> bool:
    """True if `cmd` invokes one of our hook scripts, regardless of where it
    lives on disk. Compares basenames so an entry from an older watchmen
    install (different absolute path) is still recognized — that's what
    lets `watchmen hooks install` self-heal stale entries instead of
    leaving them to fail with "No such file or directory" on every event."""
    if not cmd:
        return False
    # Hook commands in settings.json are usually the absolute path alone but
    # tolerate users (or older releases) that prepended `sh `, `bash `, etc.
    # On Windows the .ps1 form is wrapped with `powershell -NoProfile
    # -ExecutionPolicy Bypass -File "..."`, so the script path lands as the
    # final whitespace-separated token; the basename check below still hits.
    first_token = cmd.strip().split()[-1] if cmd.strip().split() else ""
    # Strip surrounding quotes that the powershell -File wrapper adds.
    first_token = first_token.strip('"').strip("'")
    return Path(first_token).name in WATCHMEN_SCRIPT_NAMES


def _load_settings(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        # Corrupt config — back up and start fresh rather than crashing.
        # Don't silently drop it; the user will see the .bak file.
        return {}


def _save_settings(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    backup = path.with_suffix(f"{path.suffix}.bak.{time.strftime('%Y%m%d-%H%M%S')}")
    backup.write_text(path.read_text())
    return backup


def _scrub_watchmen_hooks(hooks: dict) -> int:
    """Remove every entry whose command invokes one of our hook scripts,
    matched by basename (`watchmen_observe.sh`, `watchmen_brief.sh`, …).

    Mutates `hooks` in place. Returns the number of inner hook entries
    removed (not the count of event keys touched).

    Matching by basename — not full path — is what lets a routine
    `watchmen hooks install` self-heal: an entry written by an older
    watchmen release with a different absolute path (pre-reorg, moved
    checkout, different uv tool venv) is still recognized and cleaned.
    """
    removed = 0
    for event in list(hooks.keys()):
        entries = hooks[event]
        new_entries = []
        for e in entries:
            inner = e.get("hooks", []) if isinstance(e, dict) else []
            kept = [h for h in inner if not _is_watchmen_hook_cmd((h or {}).get("command", ""))]
            removed += len(inner) - len(kept)
            if kept:
                new_e = dict(e)
                new_e["hooks"] = kept
                new_entries.append(new_e)
            # else: the whole entry was watchmen-only — drop it
        if new_entries:
            hooks[event] = new_entries
        else:
            hooks.pop(event, None)
    return removed


def _install_one(host: _Host) -> tuple[bool, int]:
    """Wire watchmen hooks into one host's config file. Returns (touched, added).
    `touched` is False if the host's config is missing AND no scripts exist —
    we don't create a config file for an agent that isn't installed."""
    path = _host_path(host)

    # Skip cleanly if the host clearly isn't installed (no parent dir AND no
    # existing config). Auto-creating ~/.codex/ on a machine without Codex
    # would be presumptuous — leave the user's home tree alone.
    if not path.exists() and not path.parent.exists():
        print(f"  {host.name}: not installed (no {path.parent}) — skipped")
        return False, 0

    settings = _load_settings(path)
    hooks = settings.setdefault("hooks", {})

    backup_path = _backup(path)
    if backup_path:
        print(f"  {host.name}: backed up → {backup_path}")

    scrubbed = _scrub_watchmen_hooks(hooks)
    if scrubbed:
        print(f"  {host.name}: cleaned {scrubbed} existing watchmen entr(ies) "
              "(stale paths / retired scripts) before reinstall")

    supported = host.supported_events
    added = 0
    for event, scripts in WATCHMEN_HOOKS.items():
        if supported is not None and event not in supported:
            continue
        existing = hooks.setdefault(event, [])
        for script_key, matcher in scripts:
            cmd_str = _settings_command_for(WATCHMEN_SCRIPTS[script_key])
            entry: dict = {"hooks": [{"type": "command", "command": cmd_str}]}
            if matcher is not None:
                entry["matcher"] = matcher
            existing.append(entry)
            added += 1

    _save_settings(path, settings)
    print(f"  {host.name}: installed {added} entries → {path}")
    return True, added


def _uninstall_one(host: _Host) -> tuple[bool, int]:
    """Scrub watchmen entries from one host's config. Returns (touched, removed)."""
    path = _host_path(host)
    if not path.exists():
        print(f"  {host.name}: no config at {path} — skipped")
        return False, 0

    settings = _load_settings(path)
    hooks = settings.get("hooks") or {}
    if not hooks:
        print(f"  {host.name}: no hooks block at {path} — skipped")
        return False, 0

    backup_path = _backup(path)
    if backup_path:
        print(f"  {host.name}: backed up → {backup_path}")

    removed = _scrub_watchmen_hooks(hooks)
    if not hooks:
        settings.pop("hooks", None)
    else:
        settings["hooks"] = hooks
    _save_settings(path, settings)
    print(f"  {host.name}: removed {removed} watchmen entr(ies) from {path}")
    return True, removed


def install() -> int:
    missing = [str(p) for p in WATCHMEN_SCRIPTS.values() if not p.exists()]
    if missing:
        print(f"ERROR: hook script(s) not found: {', '.join(missing)}")
        return 1
    # The executable bit only matters for the .sh scripts — .ps1 files don't
    # carry one, and Windows ignores chmod silently anyway.
    for p in WATCHMEN_SCRIPTS.values():
        if p.suffix == ".sh":
            p.chmod(0o755)

    print("wiring watchmen hooks into supported agents:")
    any_touched = False
    for host in HOSTS:
        touched, _added = _install_one(host)
        any_touched = any_touched or touched

    if not any_touched:
        print("no supported agent detected (~/.claude or ~/.codex) — nothing wired.")
        return 1

    for key, p in WATCHMEN_SCRIPTS.items():
        print(f"hook script: {key} → {p}")
    print("Note: start the local hooks server with `uv run python -m watchmen.server` in a terminal so events are captured.")
    return 0


def uninstall() -> int:
    print("scrubbing watchmen hook entries from supported agents:")
    any_touched = False
    for host in HOSTS:
        touched, _removed = _uninstall_one(host)
        any_touched = any_touched or touched
    if not any_touched:
        print("no agent config files found — nothing to uninstall.")
    return 0


def is_installed_summary() -> dict[str, bool]:
    """Return per-host installation state for use in `watchmen status`.

    A host is considered "installed" iff its settings file exists AND at
    least one of the expected hook events has a watchmen command registered.
    Quieter than `status()` (no stdout), which is what the unified status
    screen needs.
    """
    out: dict[str, bool] = {}
    for host in HOSTS:
        path = _host_path(host)
        if not path.exists():
            out[host.name] = False
            continue
        try:
            settings = _load_settings(path)
        except Exception:
            out[host.name] = False
            continue
        hooks = settings.get("hooks") or {}
        # Walk every event we install into and look for a watchmen command.
        # Use `_is_watchmen_hook_cmd` so stale absolute paths from a prior
        # install location still count as "installed" — what we care about
        # at the status screen is "is there *any* watchmen hook wired up",
        # not "is the path on disk identical to today's script path".
        present = False
        for event, scripts in WATCHMEN_HOOKS.items():
            if host.supported_events is not None and event not in host.supported_events:
                continue
            entries = hooks.get(event) or []
            for e in entries:
                for h in e.get("hooks", []):
                    if _is_watchmen_hook_cmd(h.get("command") or ""):
                        present = True
                        break
                if present:
                    break
            if present:
                break
        out[host.name] = present
    return out


def status() -> int:
    for key, p in WATCHMEN_SCRIPTS.items():
        print(f"watchmen {key}: {p}")
    print()
    for host in HOSTS:
        path = _host_path(host)
        if not path.exists():
            print(f"{host.name}: {path} (not present)")
            continue
        settings = _load_settings(path)
        hooks = settings.get("hooks") or {}
        print(f"{host.name}: {path}")
        for event, scripts in WATCHMEN_HOOKS.items():
            if host.supported_events is not None and event not in host.supported_events:
                continue
            entries = hooks.get(event) or []
            for script_key, _matcher in scripts:
                cmd_str = _settings_command_for(WATCHMEN_SCRIPTS[script_key])
                present = any(
                    any(h.get("command") == cmd_str for h in e.get("hooks", []))
                    for e in entries
                )
                marker = "✓" if present else "·"
                print(f"  {marker} {event:<18} ({script_key})")
        print()
    return 0
