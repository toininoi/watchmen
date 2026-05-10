"""Hook installer — wires watchmen_observe.sh into ~/.claude/settings.json so every
Claude Code session pipes its hook events to the local observer at 127.0.0.1:8765.

Backs up the existing settings.json before mutating it. Idempotent — re-running just
ensures the watchmen entries are present without duplicating them.
"""

import json
import time
from pathlib import Path

ROOT = Path(__file__).parent
HOOK_SCRIPT = (ROOT / "hooks" / "watchmen_observe.sh").resolve()
SETTINGS_FILE = Path.home() / ".claude" / "settings.json"

# Events watchmen wires up. Each entry can include a matcher (PreToolUse/PostToolUse have one).
WATCHMEN_HOOK_ENTRIES = {
    "PreToolUse": [{"matcher": "", "hooks": [{"type": "command", "command": str(HOOK_SCRIPT)}]}],
    "PostToolUse": [{"matcher": "", "hooks": [{"type": "command", "command": str(HOOK_SCRIPT)}]}],
    "SessionStart": [{"hooks": [{"type": "command", "command": str(HOOK_SCRIPT)}]}],
    "SessionEnd": [{"hooks": [{"type": "command", "command": str(HOOK_SCRIPT)}]}],
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": str(HOOK_SCRIPT)}]}],
    "Stop": [{"hooks": [{"type": "command", "command": str(HOOK_SCRIPT)}]}],
    "SubagentStop": [{"hooks": [{"type": "command", "command": str(HOOK_SCRIPT)}]}],
    "Notification": [{"hooks": [{"type": "command", "command": str(HOOK_SCRIPT)}]}],
    "PreCompact": [{"hooks": [{"type": "command", "command": str(HOOK_SCRIPT)}]}],
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


def install() -> int:
    if not HOOK_SCRIPT.exists():
        print(f"ERROR: hook script not found at {HOOK_SCRIPT}")
        return 1
    HOOK_SCRIPT.chmod(0o755)

    settings = _load_settings()
    hooks = settings.setdefault("hooks", {})

    backup_path = _backup()
    if backup_path.exists():
        print(f"backed up existing settings → {backup_path}")

    added = 0
    for event, entries in WATCHMEN_HOOK_ENTRIES.items():
        existing = hooks.setdefault(event, [])
        for entry in entries:
            # check if our hook command is already present in any entry for this event
            already = any(
                any(h.get("command") == str(HOOK_SCRIPT) for h in e.get("hooks", []))
                for e in existing
            )
            if not already:
                existing.append(entry)
                added += 1

    _save_settings(settings)
    print(f"installed watchmen hooks: {added} new entries across {len(WATCHMEN_HOOK_ENTRIES)} events")
    print(f"hook script: {HOOK_SCRIPT}")
    print("Note: start the local hooks server with `uv run python server.py` in a terminal so events are captured.")
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

    removed = 0
    for event, entries in list(hooks.items()):
        new_entries = []
        for e in entries:
            inner = [h for h in e.get("hooks", []) if h.get("command") != str(HOOK_SCRIPT)]
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
    print(f"watchmen hook script: {HOOK_SCRIPT}")
    print(f"settings.json:        {SETTINGS_FILE}")
    print()
    for event in WATCHMEN_HOOK_ENTRIES.keys():
        entries = hooks.get(event) or []
        present = any(
            any(h.get("command") == str(HOOK_SCRIPT) for h in e.get("hooks", []))
            for e in entries
        )
        marker = "✓" if present else "·"
        print(f"  {marker} {event}")
    return 0
