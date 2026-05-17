"""Doctor diagnostics + settings glue for the web viewer.

`run_checks()` mirrors the structured rows that `cmd_doctor` builds for
the CLI. The CLI uses Rich for output; we want the same probe results
without the formatting. Both surfaces walk the same sequence of checks
so the web doctor stays in sync as the CLI evolves — and so users get a
consistent story whether they typed `watchmen doctor` or opened
`/doctor` in the browser.

`get_settings()` / `set_api_key()` / `set_port()` wrap the same
`config.read_env_var` / `config.write_env_var` plumbing that the CLI
uses. Keeping these here (rather than in server.py) makes server.py
remain focused on routes; the heavier integration logic lives next to
its sibling helpers in viewer/.
"""

from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from watchmen import config, state
from watchmen.paths import CORPUS_DB

# Severity levels mirror cmd_doctor: "ok" = green ✓, "warn" = yellow !,
# "fail" = red ✗. The web renderer picks the chip color from this.
Severity = str  # "ok" | "warn" | "fail"


def _row(label: str, severity: Severity, detail: str, fix: str | None = None) -> dict:
    return {"label": label, "severity": severity, "detail": detail, "fix": fix}


def _check_openrouter_key(key: str) -> tuple[bool, str]:
    """Same logic as cli._check_openrouter_key, duplicated locally so
    diagnostics doesn't cross-import the CLI module (which would create
    a viewer→cli cycle for what's logically a leaf concern)."""
    import httpx
    try:
        r = httpx.get(
            "https://openrouter.ai/api/v1/auth/key",
            headers={"Authorization": f"Bearer {key}"},
            timeout=10.0,
        )
    except httpx.RequestError as e:
        return False, f"connection error: {type(e).__name__}"
    if r.status_code == 200:
        try:
            info = (r.json() or {}).get("data") or {}
        except ValueError:
            info = {}
        usage = info.get("usage")
        limit = info.get("limit")
        if usage is not None and limit is not None and limit > 0:
            return True, f"valid · credits used ${float(usage):.2f} of ${float(limit):.2f}"
        if usage is not None and limit is None:
            return True, f"valid · credits used ${float(usage):.2f} (no hard limit)"
        return True, "valid"
    if r.status_code == 401:
        try:
            msg = (r.json().get("error") or {}).get("message", "")
        except (ValueError, AttributeError):
            msg = ""
        return False, f"401 · {msg or 'unauthorized'}"
    return False, f"HTTP {r.status_code} · {r.text[:120]}"


def run_checks(*, check_openrouter: bool = True) -> dict:
    """Run all doctor probes; return {rows, summary} for template render.

    `check_openrouter=False` skips the HTTP probe (handy in tests + when
    rendering the page should be fast/offline). The key-set check still
    runs either way."""
    rows: list[dict] = []

    # 1. OpenRouter API key
    current = config.read_env_var("OPENROUTER_API_KEY")
    if not current:
        rows.append(_row(
            "OpenRouter key", "fail",
            "not set",
            fix="Set the API key in /settings, or run `watchmen settings api-key`.",
        ))
    elif not check_openrouter:
        rows.append(_row(
            "OpenRouter key", "ok",
            "set (HTTP probe skipped)",
        ))
    else:
        ok, info = _check_openrouter_key(current)
        rows.append(_row(
            "OpenRouter key", "ok" if ok else "fail", info,
            fix=None if ok else "Update in /settings or run `watchmen settings api-key`.",
        ))

    # 2. corpus.db
    if not CORPUS_DB.exists():
        rows.append(_row(
            "corpus.db", "fail",
            "missing",
            fix="Run `watchmen ingest` to bootstrap the corpus.",
        ))
    else:
        try:
            cc = sqlite3.connect(CORPUS_DB)
            n_sessions = cc.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            n_prompts = cc.execute("SELECT COUNT(*) FROM prompts").fetchone()[0]
            cc.close()
        except sqlite3.Error as e:
            rows.append(_row("corpus.db", "fail", f"sqlite error: {e}"))
        else:
            if n_sessions == 0:
                rows.append(_row(
                    "corpus.db", "fail",
                    "no sessions ingested yet",
                    fix="Run `watchmen ingest` to populate.",
                ))
            else:
                rows.append(_row(
                    "corpus.db", "ok",
                    f"{n_sessions:,} sessions / {n_prompts:,} prompts",
                ))

    # 3. tracked projects
    try:
        state.init_db()
        projects = state.list_projects()
    except Exception:
        projects = []
    if not projects:
        rows.append(_row(
            "tracked projects", "fail",
            "0 tracked",
            fix="Run `watchmen init` or `watchmen track <key> --repo <path>`.",
        ))
    else:
        rows.append(_row(
            "tracked projects", "ok",
            f"{len(projects)} project{'s' if len(projects) != 1 else ''}",
        ))

    # 4. service backend (daemon + viewer agent load state)
    try:
        from watchmen import service
        daemon_loaded = service.is_daemon_loaded()
        viewer_loaded = service.is_viewer_loaded()
        backend = service.BACKEND_NAME
    except Exception:
        daemon_loaded = viewer_loaded = False
        backend = "service"
    rows.append(_row(
        f"daemon ({backend})", "ok" if daemon_loaded else "warn",
        "loaded" if daemon_loaded else "not loaded",
        fix=None if daemon_loaded else "`watchmen daemon install`",
    ))
    rows.append(_row(
        f"viewer ({backend})", "ok" if viewer_loaded else "warn",
        "loaded" if viewer_loaded else "not loaded",
        fix=None if viewer_loaded else "`watchmen viewer install`",
    ))

    # 5. hooks for installed agents
    try:
        import json as _json
        from watchmen import hooks_setup
        for label, path in (
            ("Claude Code hooks", hooks_setup.CLAUDE_SETTINGS_FILE),
            ("Codex hooks",       hooks_setup.CODEX_SETTINGS_FILE),
        ):
            if not path.exists():
                # Agent isn't installed on this machine — skip silently.
                continue
            try:
                settings = _json.loads(path.read_text())
            except _json.JSONDecodeError:
                rows.append(_row(label, "warn", "settings file invalid JSON",
                                 fix="`watchmen hooks install`"))
                continue
            wired = sum(
                1 for entries in (settings.get("hooks") or {}).values()
                for e in entries
                for h in e.get("hooks") or []
                if "watchmen" in (h.get("command") or "")
            )
            rows.append(_row(
                label, "ok" if wired else "warn",
                f"{wired} watchmen entries wired" if wired else "not wired",
                fix=None if wired else "`watchmen hooks install`",
            ))
    except Exception as e:
        rows.append(_row("hooks", "warn", f"could not read settings ({type(e).__name__})"))

    # 6. latest run age
    try:
        runs = state.recent_runs(limit=1)
    except Exception:
        runs = []
    if runs:
        last = runs[0]
        try:
            t = datetime.fromisoformat(last["started_at"])
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            age = datetime.now(timezone.utc) - t
            hours = age.total_seconds() / 3600
            age_str = f"{hours:.1f}h ago" if hours < 48 else f"{age.days}d ago"
            rows.append(_row(
                "latest run", "ok",
                f"{last['kind']} for {last['project_key']} · {age_str} ({last['status']})",
            ))
        except Exception:
            rows.append(_row("latest run", "ok",
                             f"{last['kind']} for {last['project_key']}"))
    else:
        rows.append(_row("latest run", "warn", "no runs recorded yet",
                         fix="Curator or daemon runs will populate this."))

    # 7. disk free
    try:
        free = shutil.disk_usage(Path.home()).free
        free_gb = free / 1024**3
        rows.append(_row(
            "disk free (~)", "ok" if free_gb > 1.0 else "fail",
            f"{free_gb:.1f} GiB",
            fix=None if free_gb > 1.0 else "Free disk space for corpus growth.",
        ))
    except Exception as e:
        rows.append(_row("disk free (~)", "warn", f"{type(e).__name__}"))

    fails = sum(1 for r in rows if r["severity"] == "fail")
    warns = sum(1 for r in rows if r["severity"] == "warn")
    if fails == 0 and warns == 0:
        verdict = "healthy"
        mood = "Everything's connected. The pattern holds."
    elif fails == 0:
        verdict = f"{warns} warning{'s' if warns != 1 else ''}"
        mood = "A pattern frays. Observable, not yet consequential."
    else:
        verdict = f"{fails} failure{'s' if fails != 1 else ''}"
        mood = "A discontinuity. Required for the rest to function."

    return {
        "rows": rows,
        "summary": {
            "fails": fails, "warns": warns,
            "verdict": verdict, "mood": mood,
        },
    }


# ── Settings ──────────────────────────────────────────────────────────


def _mask(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "•" * len(key)
    return f"{key[:4]}…{key[-4:]} ({len(key)} chars)"


def get_settings() -> dict:
    """Snapshot for the /settings template. API key is masked unless the
    user explicitly opts to reveal (URL query, future enhancement)."""
    key = config.read_env_var("OPENROUTER_API_KEY") or ""
    port = config.viewer_port()
    port_source = (
        "explicit (env or .env)"
        if config.read_env_var("WATCHMEN_VIEWER_PORT")
        else "default"
    )
    try:
        state.init_db()
        projects = state.list_projects()
    except Exception:
        projects = []
    return {
        "api_key_set": bool(key),
        "api_key_masked": _mask(key),
        "viewer_port": port,
        "viewer_port_source": port_source,
        "viewer_port_default": config.VIEWER_DEFAULT_PORT,
        "projects": projects,
    }


def set_api_key(value: str) -> Path:
    value = (value or "").strip()
    if not value:
        raise ValueError("api key cannot be empty")
    return config.write_env_var("OPENROUTER_API_KEY", value)


def set_viewer_port(value: str) -> tuple[Path, int]:
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise ValueError("port must be an integer")
    if not (1024 <= port <= 65535):
        raise ValueError("port must be in 1024–65535")
    path = config.write_env_var("WATCHMEN_VIEWER_PORT", str(port))
    return path, port


def update_project_settings(
    project_key: str,
    *,
    enabled: bool | None = None,
    threshold_new_prompts: int | None = None,
) -> dict:
    """Apply per-project settings edits. Mirrors `cmd_settings_set` for the
    two fields we expose in the web UI; richer settings stay CLI-only."""
    state.init_db()
    if not state.get_project(project_key):
        raise ValueError(f"project not tracked: {project_key}")
    update: dict[str, object] = {}
    if enabled is not None:
        update["enabled"] = 1 if enabled else 0
    if threshold_new_prompts is not None:
        if threshold_new_prompts < 1:
            raise ValueError("threshold_new_prompts must be ≥ 1")
        update["threshold_new_prompts"] = threshold_new_prompts
    if update:
        state.update_project(project_key, **update)
    return state.get_project(project_key) or {}
