"""Shared runtime config — env-file IO + viewer host/port resolution.

`~/.config/watchmen/.env` is the single source of truth for cross-process
settings (OpenRouter key, viewer port). Process env vars override the file.

Helpers here are deliberately simple — no schema, no validation beyond what
the caller does. Adding a real config schema is a P3 item.
"""

import os
from pathlib import Path

ENV_PATH = Path.home() / ".config" / "watchmen" / ".env"

# Bumped 8888 → 8979 in 0.2: 8888 collides with Jupyter, which a lot of
# data-science users have permanently bound. 8979 is uncommon, mnemonic
# (8-9-7-9), and well outside the popular dev-tool port range.
VIEWER_DEFAULT_HOST = "127.0.0.1"
VIEWER_DEFAULT_PORT = 8979


def read_env_var(key: str, default: str | None = None) -> str | None:
    """Look up a config value: process env first, then ~/.config/watchmen/.env."""
    if v := os.environ.get(key):
        return v
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return default


def write_env_var(key: str, value: str) -> Path:
    """Persist a `key=value` line to the global env file, replacing any prior line
    for the same key. Preserves unrelated lines. chmods to 0600 to keep secrets
    off other users' eyes. Returns the path written."""
    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = ENV_PATH.read_text().splitlines() if ENV_PATH.exists() else []
    new_lines = [ln for ln in lines if not ln.startswith(f"{key}=")]
    new_lines.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(new_lines) + "\n")
    ENV_PATH.chmod(0o600)
    return ENV_PATH


def viewer_port() -> int:
    """Current viewer port — WATCHMEN_VIEWER_PORT env / config file / default."""
    raw = read_env_var("WATCHMEN_VIEWER_PORT")
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return VIEWER_DEFAULT_PORT


def viewer_base_url() -> str:
    """Canonical viewer base URL — used by curate.py + onboard.py + cli.doctor
    to construct deep links into the viewer that always reflect the user's
    currently-configured port."""
    return f"http://{VIEWER_DEFAULT_HOST}:{viewer_port()}"
