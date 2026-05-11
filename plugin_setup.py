"""Plugin install/maintenance helpers.

Two operations live here:

  update_marketplace()    — `git pull` the cached marketplace clone so a
                            subsequent /plugin install picks up the latest
                            commit on main. Saves users from doing this by hand
                            every time we push a plugin update.

  install_statusline()    — discover the newest installed plugin version under
                            ~/.claude/plugins/cache/watchmen/watchmen/, then
                            write that path into ~/.claude/settings.json as the
                            statusLine command. Hides the versioned-path
                            fragility from users.

  uninstall_statusline()  — remove the watchmen entry from settings.json.

All settings.json mutations back up the existing file to .json.bak.<ts>.
"""

import json
import re
import subprocess
import time
from pathlib import Path

MARKETPLACE_DIR = Path.home() / ".claude" / "plugins" / "marketplaces" / "watchmen"
PLUGIN_CACHE = Path.home() / ".claude" / "plugins" / "cache" / "watchmen" / "watchmen"
SETTINGS = Path.home() / ".claude" / "settings.json"


def _load_settings() -> dict:
    if not SETTINGS.exists():
        return {}
    return json.loads(SETTINGS.read_text())


def _save_settings(data: dict) -> None:
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(data, indent=2) + "\n")


def _backup_settings() -> Path | None:
    if not SETTINGS.exists():
        return None
    backup = SETTINGS.with_suffix(f".json.bak.{time.strftime('%Y%m%d-%H%M%S')}")
    backup.write_text(SETTINGS.read_text())
    return backup


_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)")


def _version_sort_key(name: str) -> tuple:
    m = _SEMVER.match(name)
    if m:
        return (1, tuple(int(x) for x in m.groups()))
    return (0, name)  # non-semver versions sort first; rare


def _newest_version_dir() -> Path | None:
    if not PLUGIN_CACHE.exists():
        return None
    dirs = [d for d in PLUGIN_CACHE.iterdir() if d.is_dir()]
    if not dirs:
        return None
    return max(dirs, key=lambda d: _version_sort_key(d.name))


# ─── Public commands ─────────────────────────────────────────────────────────


def update_marketplace() -> int:
    if not MARKETPLACE_DIR.exists():
        print(f"watchmen marketplace not found at {MARKETPLACE_DIR}")
        print("Add it first inside Claude Code: /plugin marketplace add firstbatchxyz/watchmen")
        return 1
    r = subprocess.run(
        ["git", "-C", str(MARKETPLACE_DIR), "pull", "--ff-only"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(f"git pull failed:\n{r.stderr.strip() or r.stdout.strip()}")
        return r.returncode
    print(r.stdout.strip() or "(already up to date)")
    print()
    print("Marketplace clone updated. Inside Claude Code, run:")
    print("  /plugin uninstall watchmen@watchmen")
    print("  /plugin install watchmen@watchmen")
    print("  /reload-plugins")
    return 0


def install_statusline(force: bool = False) -> int:
    latest = _newest_version_dir()
    if latest is None:
        print(f"plugin not installed yet. Inside Claude Code, run:")
        print("  /plugin marketplace add firstbatchxyz/watchmen")
        print("  /plugin install watchmen@watchmen")
        return 1
    statusline = latest / "bin" / "statusline.sh"
    if not statusline.exists():
        print(f"statusline.sh not found at {statusline}")
        return 1

    settings = _load_settings()
    existing = settings.get("statusLine")
    target_cmd = str(statusline)

    if isinstance(existing, dict):
        cur = existing.get("command", "")
        if cur == target_cmd:
            print(f"statusLine already points at latest: {target_cmd}")
            return 0
        if cur.startswith(str(PLUGIN_CACHE)):
            print(f"updating watchmen statusLine: {cur} → {target_cmd}")
        else:
            print("WARNING: ~/.claude/settings.json already has a non-watchmen statusLine:")
            print(f"  {cur or existing}")
            if not force:
                print()
                print("To replace it, re-run with --force.")
                return 1
            print("(--force given, replacing)")

    backup = _backup_settings()
    if backup:
        print(f"backed up settings → {backup}")

    settings["statusLine"] = {"type": "command", "command": target_cmd}
    _save_settings(settings)
    print(f"wrote statusLine → {target_cmd}")
    print()
    print("Open a new Claude Code session in a tracked repo to see the 💡 indicator.")
    return 0


def uninstall_statusline() -> int:
    if not SETTINGS.exists():
        print("no settings.json to modify")
        return 0
    settings = _load_settings()
    existing = settings.get("statusLine") or {}
    cmd = existing.get("command", "") if isinstance(existing, dict) else ""
    if not cmd.startswith(str(PLUGIN_CACHE)):
        print("settings.json statusLine isn't pointed at watchmen; nothing to remove.")
        return 0

    backup = _backup_settings()
    if backup:
        print(f"backed up settings → {backup}")
    settings.pop("statusLine", None)
    _save_settings(settings)
    print("removed watchmen statusLine entry")
    return 0


def status() -> int:
    marketplace_present = MARKETPLACE_DIR.exists()
    latest = _newest_version_dir()
    settings = _load_settings()
    sl = settings.get("statusLine") or {}
    sl_cmd = sl.get("command", "") if isinstance(sl, dict) else ""

    print(f"marketplace clone:  {MARKETPLACE_DIR}  ({'present' if marketplace_present else 'missing'})")
    if marketplace_present:
        r = subprocess.run(["git", "-C", str(MARKETPLACE_DIR), "log", "-1", "--oneline"],
                           capture_output=True, text=True)
        print(f"  HEAD: {(r.stdout or '').strip()}")
    print(f"plugin cache dirs:  {PLUGIN_CACHE}")
    if latest:
        print(f"  newest version:   {latest.name}")
    else:
        print("  (no versions installed yet)")
    print(f"statusLine wired:   {'yes — ' + sl_cmd if sl_cmd.startswith(str(PLUGIN_CACHE)) else 'no'}")
    return 0
