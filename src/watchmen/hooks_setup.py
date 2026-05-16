"""Hook installer — wires the watchmen observer into ~/.claude/settings.json
so every Claude Code session pipes its hook events to the local observer at
127.0.0.1:8765.

Two hook scripts ship — `watchmen_observe.sh` for POSIX shells and
`watchmen_observe.ps1` for PowerShell — and the installer picks the one
that matches the host's native shell. Backs up settings.json before mutating
it. Idempotent — re-running just ensures the watchmen entries are present
without duplicating them.
"""

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent

# Each platform invokes the settings.json "command" field through its native
# shell (bash/zsh on POSIX, cmd.exe on Windows), so the hook script matches.
_HOOK_SCRIPTS = {
    "win32":  ROOT / "hooks" / "watchmen_observe.ps1",
    "posix":  ROOT / "hooks" / "watchmen_observe.sh",
}
HOOK_SCRIPT = (_HOOK_SCRIPTS["win32"] if sys.platform == "win32" else _HOOK_SCRIPTS["posix"]).resolve()
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

# Paths watchmen used to install but no longer ships. install/uninstall scrub
# any stale entries here from the user's settings.json so a pull + reinstall
# cleanly removes deprecated hooks without manual JSON editing.
# Each entry is the absolute path the older release used to wire in.
_LEGACY_HOOK_PATHS: set[str] = {
    # Removed in 0.2.0 — macOS notification briefs.
    str((ROOT / "hooks" / "watchmen_brief.sh").resolve()),
}


def _load_settings() -> dict:
    if not SETTINGS_FILE.exists():
        return {}
    return json.loads(SETTINGS_FILE.read_text())


def _save_settings(data: dict) -> None:
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(data, indent=2) + "\n")


def _backup() -> Path:
    backup = SETTINGS_FILE.with_suffix(f".json.bak.{time.strftime('%Y%m%d-%H%M%S')}")
    if SETTINGS_FILE.exists():
        backup.write_text(SETTINGS_FILE.read_text())
    return backup


def _scrub_legacy_hooks(hooks: dict) -> int:
    """Remove entries pointing at scripts watchmen no longer ships. Mutates
    `hooks` in place. Returns the number of entries removed. Lets a
    routine `watchmen hooks install` clean up after a retired script
    (e.g., watchmen_brief.sh in 0.2.0) without the user needing to know."""
    removed = 0
    for event, entries in list(hooks.items()):
        new_entries = []
        for e in entries:
            inner = [
                h for h in e.get("hooks", [])
                if h.get("command") not in _LEGACY_HOOK_PATHS
            ]
            if len(inner) != len(e.get("hooks", [])):
                removed += (len(e.get("hooks", [])) - len(inner))
            if inner:
                new_e = dict(e)
                new_e["hooks"] = inner
                new_entries.append(new_e)
            # else: drop the entry entirely if its only command was legacy
        if new_entries:
            hooks[event] = new_entries
        else:
            hooks.pop(event, None)
    return removed


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

    settings = _load_settings()
    hooks = settings.setdefault("hooks", {})

    backup_path = _backup()
    if backup_path.exists():
        print(f"backed up existing settings → {backup_path}")

    # Scrub stale entries for hooks we used to ship but no longer do. Lets a
    # pull + `watchmen hooks install` cleanly retire deprecated scripts.
    legacy_removed = _scrub_legacy_hooks(hooks)
    if legacy_removed:
        print(f"removed {legacy_removed} stale hook entr(ies) for retired scripts")

    added = 0
    for event, scripts in WATCHMEN_HOOKS.items():
        existing = hooks.setdefault(event, [])
        for script_key, matcher in scripts:
            cmd_str = _settings_command_for(WATCHMEN_SCRIPTS[script_key])
            already = any(
                any(h.get("command") == cmd_str for h in e.get("hooks", []))
                for e in existing
            )
            if already:
                continue
            entry: dict = {"hooks": [{"type": "command", "command": cmd_str}]}
            if matcher is not None:
                entry["matcher"] = matcher
            existing.append(entry)
            added += 1

    _save_settings(settings)
    print(f"installed watchmen hooks: {added} new entries across {len(WATCHMEN_HOOKS)} events")
    for key, p in WATCHMEN_SCRIPTS.items():
        print(f"  - {key}: {p}")
    print("Note: start the local hooks server with `uv run python -m watchmen.server` in a terminal so events are captured.")
    return 0


def uninstall() -> int:
    if not SETTINGS_FILE.exists():
        print("no settings.json — nothing to uninstall")
        return 0

    settings = _load_settings()
    hooks = settings.get("hooks") or {}
    if not hooks:
        print("no hooks block — nothing to uninstall")
        return 0

    backup_path = _backup()
    print(f"backed up existing settings → {backup_path}")

    # Scrub both current scripts AND retired-but-still-referenced ones, so
    # uninstall fully cleans up after older releases.
    watchmen_cmds = (
        {str(p) for p in WATCHMEN_SCRIPTS.values()}
        | {_settings_command_for(p) for p in WATCHMEN_SCRIPTS.values()}
        | _LEGACY_HOOK_PATHS
    )
    removed = 0
    for event, entries in list(hooks.items()):
        new_entries = []
        for e in entries:
            inner = [h for h in e.get("hooks", []) if h.get("command") not in watchmen_cmds]
            if inner:
                new_e = dict(e)
                new_e["hooks"] = inner
                new_entries.append(new_e)
            else:
                removed += 1
        if new_entries:
            hooks[event] = new_entries
        else:
            del hooks[event]

    if not hooks:
        settings.pop("hooks", None)
    else:
        settings["hooks"] = hooks
    _save_settings(settings)
    print(f"uninstalled {removed} watchmen hook entries")
    return 0


def status() -> int:
    if not SETTINGS_FILE.exists():
        print(f"settings.json not found at {SETTINGS_FILE}")
        return 0
    settings = _load_settings()
    hooks = settings.get("hooks") or {}
    for key, p in WATCHMEN_SCRIPTS.items():
        print(f"watchmen {key}: {p}")
    print(f"settings.json:    {SETTINGS_FILE}")
    print()
    for event, scripts in WATCHMEN_HOOKS.items():
        entries = hooks.get(event) or []
        for script_key, _matcher in scripts:
            cmd_str = _settings_command_for(WATCHMEN_SCRIPTS[script_key])
            present = any(
                any(h.get("command") == cmd_str for h in e.get("hooks", []))
                for e in entries
            )
            marker = "✓" if present else "·"
            print(f"  {marker} {event:<18} ({script_key})")
    return 0
