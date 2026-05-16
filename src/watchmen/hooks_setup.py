"""Hook installer — wires watchmen_observe.sh into ~/.claude/settings.json so every
Claude Code session pipes its hook events to the local observer at 127.0.0.1:8765.

Backs up the existing settings.json before mutating it. Idempotent + self-healing:
re-running install scrubs any existing watchmen entries (matched by script
filename so a reorg or moved checkout doesn't leave orphaned entries that fail
with "No such file or directory" on every event) and then writes the canonical
set fresh.
"""

import json
import time
from pathlib import Path

ROOT = Path(__file__).parent
HOOK_SCRIPT = (ROOT / "hooks" / "watchmen_observe.sh").resolve()
SETTINGS_FILE = Path.home() / ".claude" / "settings.json"

# All hook scripts watchmen installs. Keys used internally; values are absolute paths.
WATCHMEN_SCRIPTS: dict[str, Path] = {
    "observe": HOOK_SCRIPT,
}

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

# Every basename watchmen has ever shipped or shipped-and-retired. Used as a
# whole-set filename match against settings.json entries so install / uninstall
# can clean up stale paths from older installs (e.g. before the src-layout
# move, when HOOK_SCRIPT resolved to ~/dev/watchmen/hooks/watchmen_observe.sh
# rather than ~/dev/watchmen/src/watchmen/hooks/watchmen_observe.sh).
# Add new script names here; mark retired ones with a comment so the list
# doubles as a history of every hook surface we've owned.
WATCHMEN_SCRIPT_NAMES: set[str] = {
    "watchmen_observe.sh",
    "watchmen_brief.sh",  # retired in 0.2.0
}


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
    first_token = cmd.strip().split()[-1] if cmd.strip().split() else ""
    return Path(first_token).name in WATCHMEN_SCRIPT_NAMES


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


def install() -> int:
    missing = [str(p) for p in WATCHMEN_SCRIPTS.values() if not p.exists()]
    if missing:
        print(f"ERROR: hook script(s) not found: {', '.join(missing)}")
        return 1
    for p in WATCHMEN_SCRIPTS.values():
        p.chmod(0o755)

    settings = _load_settings()
    hooks = settings.setdefault("hooks", {})

    backup_path = _backup()
    if backup_path.exists():
        print(f"backed up existing settings → {backup_path}")

    # Scrub any existing watchmen entries first (matched by script basename,
    # not absolute path) so install is both idempotent AND self-healing:
    # stale paths from older releases get cleaned, retired scripts get
    # removed, and the canonical set goes in fresh.
    scrubbed = _scrub_watchmen_hooks(hooks)
    if scrubbed:
        print(f"cleaned {scrubbed} existing watchmen hook entr(ies) "
              f"(stale paths / retired scripts) before reinstall")

    added = 0
    for event, scripts in WATCHMEN_HOOKS.items():
        existing = hooks.setdefault(event, [])
        for script_key, matcher in scripts:
            cmd_str = str(WATCHMEN_SCRIPTS[script_key])
            entry: dict = {"hooks": [{"type": "command", "command": cmd_str}]}
            if matcher is not None:
                entry["matcher"] = matcher
            existing.append(entry)
            added += 1

    _save_settings(settings)
    print(f"installed watchmen hooks: {added} entries across {len(WATCHMEN_HOOKS)} events")
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

    removed = _scrub_watchmen_hooks(hooks)
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
            cmd_str = str(WATCHMEN_SCRIPTS[script_key])
            present = any(
                any(h.get("command") == cmd_str for h in e.get("hooks", []))
                for e in entries
            )
            marker = "✓" if present else "·"
            print(f"  {marker} {event:<18} ({script_key})")
    return 0
