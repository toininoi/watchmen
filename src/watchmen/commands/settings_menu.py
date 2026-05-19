"""Interactive `watchmen settings` menu — arrow-key navigable.

The flat subcommands (`settings provider`, `settings api-key`, etc.) are kept
as scriptable entry points. This module is the friendlier UX for when a user
just types `watchmen settings` and wants to discover what's configurable
without reading `--help`.

Navigation model
----------------
- Each page is a `questionary.select` with the actions for that scope plus
  a `Back` entry at the bottom (or `Quit` at the top level). Enter selects,
  arrows navigate, Ctrl+C bails out of the whole menu.
- Page functions return one of three sentinels: keep iterating, go back one
  level, or quit entirely. The main loop interprets them.
- No mutable state is threaded between pages — each page re-reads config so
  changes made in a sub-flow are immediately reflected when you return.

Why not Rich's `Prompt.ask`?
----------------------------
Rich's prompt supports `choices=[...]` but it's keyword/type-the-name, not
arrow navigation. `questionary` is built on the same `prompt_toolkit` that
Rich already depends on transitively, so the install footprint is
essentially nil and the UX gain is significant.
"""

from __future__ import annotations

import sys
from enum import Enum

from rich.console import Console

from watchmen import config, state


# ─── Navigation sentinels ──────────────────────────────────────────────────


class _Nav(str, Enum):
    """What the current page wants the outer loop to do next."""
    STAY = "stay"   # re-render this page (e.g. after an action that changed state)
    BACK = "back"   # pop one level
    QUIT = "quit"   # exit the menu entirely


# Sentinel values for menu Choice entries. Questionary 2.x treats
# `Choice("Back", value=None)` as "use the title as the value", which would
# leak the literal string "Back" into downstream code that expects a real
# selection. Use unambiguous sentinels instead.
_BACK = "__back__"
_CANCEL = "__cancel__"


# ─── Entry point ──────────────────────────────────────────────────────────


def run_interactive_settings() -> int:
    """Open the interactive settings menu. Returns a CLI exit code.

    Falls back to printing a one-liner pointer at the flat subcommands when:
    - stdin/stdout aren't TTYs (CI, piped runs)
    - questionary isn't importable (e.g. running an older install before
      the dep landed)
    - the user Ctrl+Cs anywhere in the flow

    None of those cases should ever lose state — the page actions persist
    immediately on confirm; navigating away is always safe."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        _print_noninteractive_hint()
        return 0

    try:
        import questionary  # noqa: F401  — guard import, real use in submenus
    except ImportError:
        _print_noninteractive_hint(
            "Install the `questionary` extra (or upgrade watchmen) to use the interactive menu."
        )
        return 0

    console = Console()
    breadcrumb: list[str] = []
    try:
        return _main_menu(console, breadcrumb)
    except KeyboardInterrupt:
        console.print()
        console.print("[dim]exited settings — no changes lost.[/]")
        return 0


def _print_noninteractive_hint(extra: str | None = None) -> None:
    """Surface the flat subcommands when interactive mode isn't an option.
    This is the path CI + piped invocations will hit, so it must give the
    full equivalent surface so users aren't stuck."""
    print("watchmen settings — non-interactive shell detected.")
    if extra:
        print(extra)
    print()
    print("Use the flat subcommands instead:")
    print("  watchmen settings list                          list tracked projects")
    print("  watchmen settings show <project>                show one project's settings")
    print("  watchmen settings set <project> <key> <value>   update a per-project setting")
    print("  watchmen settings api-key [--provider NAME]     set / check an API key")
    print("  watchmen settings provider [NAME]               get / set active LLM provider")
    print("  watchmen settings model [NAME|--clear]          get / set default model")
    print("  watchmen settings port [N]                      get / set viewer port")


# ─── Main menu ─────────────────────────────────────────────────────────────


def _main_menu(console: Console, breadcrumb: list[str]) -> int:
    import questionary

    while True:
        _render_header(console, breadcrumb)

        active = config.active_provider()
        provider_key_set = bool(config.provider_key(active))
        provider_summary = f"{active} ({'key set' if provider_key_set else 'no key'})"

        model_override = config.read_env_var("WATCHMEN_DEFAULT_MODEL")
        model_summary = (
            f"{model_override} (override)"
            if model_override
            else f"{config.default_model()} (provider default)"
        )

        port_summary = str(config.viewer_port())

        try:
            n_projects = len(state.list_projects())
        except Exception:
            n_projects = 0
        projects_summary = f"{n_projects} tracked" if n_projects else "none tracked"

        choice = questionary.select(
            "What would you like to configure?",
            choices=[
                questionary.Choice(f"Provider & API key   · {provider_summary}", value="provider"),
                questionary.Choice(f"Default model        · {model_summary}",    value="model"),
                questionary.Choice(f"Viewer port          · {port_summary}",      value="port"),
                questionary.Choice(f"Per-project settings · {projects_summary}",  value="projects"),
                questionary.Separator(),
                questionary.Choice("Quit", value="quit"),
            ],
            use_indicator=True,
        ).ask()

        if choice in (None, "quit"):
            return 0

        breadcrumb.append({
            "provider": "Provider & API key",
            "model":    "Default model",
            "port":     "Viewer port",
            "projects": "Per-project settings",
        }[choice])
        try:
            nav = {
                "provider": _provider_page,
                "model":    _model_page,
                "port":     _port_page,
                "projects": _projects_root_page,
            }[choice](console, breadcrumb)
        finally:
            breadcrumb.pop()

        if nav is _Nav.QUIT:
            return 0
        # _Nav.BACK / _Nav.STAY both loop the main menu


# ─── Provider & API key ────────────────────────────────────────────────────


def _provider_page(console: Console, breadcrumb: list[str]) -> _Nav:
    import questionary

    while True:
        _render_header(console, breadcrumb)
        _render_provider_status(console)
        choice = questionary.select(
            "What would you like to do?",
            choices=[
                questionary.Choice("Switch active provider", value="switch"),
                questionary.Choice("Set / update an API key", value="set_key"),
                questionary.Separator(),
                questionary.Choice("Back", value="back"),
            ],
            use_indicator=True,
        ).ask()

        if choice in (None, "back"):
            return _Nav.BACK
        if choice == "switch":
            _switch_provider(console)
        elif choice == "set_key":
            _set_api_key(console)


def _render_provider_status(console: Console) -> None:
    """Compact provider table — same shape `watchmen settings provider`
    prints to stdout, rendered ahead of the prompt so the user has the
    context they need to pick."""
    from rich.table import Table
    active = config.active_provider()
    table = Table(show_header=False, box=None, padding=(0, 2, 0, 0))
    for name, env_var in config.PROVIDER_KEY_VARS.items():
        key = config.provider_key(name)
        marker = "[green]✓[/] set" if key else "[dim]—[/] not set"
        active_marker = "[bold cyan]→[/]" if name == active else " "
        table.add_row(active_marker, name, marker, f"[dim]{env_var}[/]")
    for name in config.OAUTH_PROVIDERS:
        available = config.provider_key(name) is not None
        marker = "[green]✓[/] OAuth" if available else "[dim]—[/] not signed in"
        source = "Claude Code keychain" if name == "claude-pro" else "Codex auth.json"
        active_marker = "[bold cyan]→[/]" if name == active else " "
        table.add_row(active_marker, name, marker, f"[dim]{source}[/]")
    console.print(table)
    console.print()


def _switch_provider(console: Console) -> None:
    import questionary

    active = config.active_provider()
    choices = []
    for name in config.ALL_PROVIDERS:
        cred = config.provider_key(name)
        if name in config.OAUTH_PROVIDERS:
            suffix = "(OAuth ready)" if cred else "(not signed in — `claude` / `codex login` first)"
        else:
            suffix = "(key set)" if cred else "(no key — set one first)"
        label = f"{name} {suffix}"
        # Don't disable empty-credential providers — let the user switch
        # first and add a credential after; we just warn after the fact.
        choices.append(questionary.Choice(label, value=name, checked=(name == active)))
    choices += [questionary.Separator(), questionary.Choice("Cancel", value=_CANCEL)]

    new_provider = questionary.select(
        "Choose active provider:",
        choices=choices,
        use_indicator=True,
    ).ask()

    if new_provider is None or new_provider == _CANCEL or new_provider == active:
        return

    config.set_active_provider(new_provider)
    if not config.provider_key(new_provider):
        if new_provider in config.OAUTH_PROVIDERS:
            login_cmd = "claude" if new_provider == "claude-pro" else "codex login"
            console.print(
                f"[yellow]![/] active provider → [bold]{new_provider}[/] "
                f"(sign in with `{login_cmd}` to enable)"
            )
        else:
            console.print(
                f"[yellow]![/] active provider → [bold]{new_provider}[/] "
                f"(no key configured yet — set one with the menu's next option)"
            )
    else:
        console.print(f"[green]✓[/] active provider → [bold]{new_provider}[/]")
    from watchmen import service as _service
    _service.notify_settings_changed("provider", interactive=True)


def _set_api_key(console: Console) -> None:
    import questionary

    choices = []
    # Only env-var-based providers are listed here — OAuth credentials are
    # managed by `claude` / `codex login`, not by pasting a key.
    for name in config.PROVIDER_KEY_VARS:
        key = config.provider_key(name)
        suffix = "(set)" if key else "(unset)"
        choices.append(questionary.Choice(f"{name} {suffix}", value=name))
    # Show OAuth providers as informational-only entries so the user doesn't
    # wonder why they're missing from the picker.
    for name in config.OAUTH_PROVIDERS:
        cred = config.provider_key(name)
        suffix = "OAuth ready" if cred else "not signed in"
        login_cmd = "claude" if name == "claude-pro" else "codex login"
        choices.append(questionary.Choice(
            f"{name} ({suffix}, use `{login_cmd}` to manage)",
            value=name,
            disabled="OAuth — no key to set",
        ))
    choices += [questionary.Separator(), questionary.Choice("Cancel", value=_CANCEL)]

    target = questionary.select(
        "Which provider's key?",
        choices=choices,
        use_indicator=True,
    ).ask()
    if target is None or target == _CANCEL:
        return
    if target in config.OAUTH_PROVIDERS:
        # Disabled entries shouldn't ever return, but guard anyway.
        return

    new_key = questionary.password(
        f"Paste new {target} API key (enter to cancel):",
    ).ask()
    if not new_key:
        return
    new_key = new_key.strip()
    if not new_key:
        return

    # Live-validate before persisting so a typo gets caught now, not on the
    # next analyst run. Mirrors the validation in `cmd_settings_api_key`.
    from watchmen import providers as _providers
    res = _providers.get_provider(target).probe(new_key)
    if res.ok:
        config.set_provider_key(target, new_key)
        console.print(f"[green]✓[/] {target}: {res.detail}")
        # If this is the first key the user has set, promote it to active —
        # otherwise they'd silently still be on a different provider.
        if config.active_provider() != target and not any(
            config.provider_key(p) for p in config.PROVIDER_KEY_VARS if p != target
        ):
            config.set_active_provider(target)
            console.print(f"[green]✓[/] active provider → [bold]{target}[/]")
        return

    console.print(f"[red]✗[/] {target}: {res.detail}")
    save_anyway = questionary.confirm(
        "Save anyway?", default=False
    ).ask()
    if save_anyway:
        config.set_provider_key(target, new_key)
        console.print("[yellow]![/] saved despite rejection")


# ─── Default model ─────────────────────────────────────────────────────────


def _model_page(console: Console, breadcrumb: list[str]) -> _Nav:
    import questionary

    while True:
        _render_header(console, breadcrumb)
        override = config.read_env_var("WATCHMEN_DEFAULT_MODEL")
        from watchmen import providers as _providers
        active = config.active_provider()
        provider_default = _providers.get_provider(active).default_model
        if override:
            console.print(f"  Current: [bold cyan]{override}[/] [dim](WATCHMEN_DEFAULT_MODEL override)[/]")
        else:
            console.print(f"  Current: [bold cyan]{provider_default}[/] [dim]({active} provider default)[/]")
        console.print()

        choice = questionary.select(
            "What would you like to do?",
            choices=[
                questionary.Choice("Set custom model", value="set"),
                questionary.Choice("Use provider default (clear override)", value="clear",
                                   disabled=None if override else "no override active"),
                questionary.Separator(),
                questionary.Choice("Back", value="back"),
            ],
            use_indicator=True,
        ).ask()

        if choice in (None, "back"):
            return _Nav.BACK
        if choice == "set":
            new_model = questionary.text(
                "New model identifier (e.g. gpt-5, claude-sonnet-4-6, deepseek/deepseek-v4-flash):",
                default=override or provider_default,
            ).ask()
            if new_model and new_model.strip():
                config.write_env_var("WATCHMEN_DEFAULT_MODEL", new_model.strip())
                console.print(f"[green]✓[/] default model → [bold]{new_model.strip()}[/]")
                from watchmen import service as _service
                _service.notify_settings_changed("model", interactive=True)
        elif choice == "clear":
            if config.clear_env_var("WATCHMEN_DEFAULT_MODEL"):
                console.print(f"[green]✓[/] override cleared — now using provider default ([bold]{provider_default}[/])")
                from watchmen import service as _service
                _service.notify_settings_changed("model", interactive=True)
            else:
                console.print("[dim]no override was set[/]")


# ─── Viewer port ───────────────────────────────────────────────────────────


def _port_page(console: Console, breadcrumb: list[str]) -> _Nav:
    import questionary

    while True:
        _render_header(console, breadcrumb)
        current = config.viewer_port()
        is_default = current == config.VIEWER_DEFAULT_PORT
        source = "default" if (is_default and not config.read_env_var("WATCHMEN_VIEWER_PORT")) else "config"
        console.print(f"  Current: [bold cyan]{current}[/] [dim]({source})[/]")
        console.print()

        choice = questionary.select(
            "What would you like to do?",
            choices=[
                questionary.Choice("Set viewer port", value="set"),
                questionary.Separator(),
                questionary.Choice("Back", value="back"),
            ],
            use_indicator=True,
        ).ask()
        if choice in (None, "back"):
            return _Nav.BACK
        if choice == "set":
            new_port = questionary.text(
                "New port (1024–65535):",
                default=str(current),
                validate=_validate_port,
            ).ask()
            if new_port and new_port.strip():
                config.write_env_var("WATCHMEN_VIEWER_PORT", new_port.strip())
                console.print(f"[green]✓[/] viewer port → [bold]{new_port.strip()}[/]")
                # Service may already be running on the old port — match the
                # behavior of `watchmen settings port`.
                try:
                    from watchmen import service
                    if service.is_viewer_loaded():
                        console.print(
                            "  [yellow]![/] viewer is loaded on the old port — "
                            "run [bold]watchmen viewer install[/] to move it"
                        )
                except Exception:
                    pass


def _validate_port(value: str) -> bool | str:
    try:
        n = int(value.strip())
    except ValueError:
        return "must be an integer"
    if not (1024 <= n <= 65535):
        return "must be in 1024–65535"
    return True


# ─── Per-project settings ──────────────────────────────────────────────────


def _projects_root_page(console: Console, breadcrumb: list[str]) -> _Nav:
    import questionary

    while True:
        _render_header(console, breadcrumb)
        try:
            state.init_db()
            projects = state.list_projects()
        except Exception as e:
            console.print(f"[red]✗[/] could not load projects: {e}")
            return _Nav.BACK
        if not projects:
            console.print("[dim]No projects tracked yet.[/]")
            console.print("[dim]Run `watchmen onboard` or `watchmen track <key> --repo <path>`.[/]")
            console.print()
            questionary.press_any_key_to_continue("Press any key to go back").ask()
            return _Nav.BACK

        choices = []
        for p in projects:
            enabled = "enabled" if p["enabled"] else "paused"
            thr = p["threshold_new_prompts"]
            choices.append(questionary.Choice(
                f"{p['project_key']:<30} · {enabled:<7} · threshold {thr}",
                value=p["project_key"],
            ))
        choices += [questionary.Separator(), questionary.Choice("Back", value=_BACK)]

        target = questionary.select(
            "Choose project:",
            choices=choices,
            use_indicator=True,
        ).ask()
        if target is None or target == _BACK:
            return _Nav.BACK

        breadcrumb.append(target)
        try:
            _project_page(console, breadcrumb, target)
        finally:
            breadcrumb.pop()


def _project_page(console: Console, breadcrumb: list[str], project_key: str) -> _Nav:
    import questionary

    while True:
        _render_header(console, breadcrumb)
        p = state.get_project(project_key)
        if not p:
            console.print(f"[red]✗[/] project {project_key!r} no longer tracked")
            return _Nav.BACK

        enabled = bool(p["enabled"])
        thr = p["threshold_new_prompts"]
        approval = bool(p.get("approval_required") or 0)
        skip_overlap = bool(p.get("skip_overlapping_skills") or 0)
        notes = (p.get("notes") or "").strip()

        choices = [
            questionary.Choice(f"Enabled                  · {'yes' if enabled else 'no'}",       value="enabled"),
            questionary.Choice(f"Threshold (new prompts)  · {thr}",                              value="threshold"),
            questionary.Choice(f"Approval required        · {'yes' if approval else 'no'}",     value="approval"),
            questionary.Choice(f"Skip overlapping skills  · {'yes' if skip_overlap else 'no'}", value="skip_overlap"),
            questionary.Choice(f"Notes                    · {notes or '(empty)'}",               value="notes"),
            questionary.Separator(),
            questionary.Choice("Back", value=_BACK),
        ]

        choice = questionary.select(
            "Toggle / edit setting:",
            choices=choices,
            use_indicator=True,
        ).ask()
        if choice is None or choice == _BACK:
            return _Nav.BACK

        if choice == "enabled":
            state.update_project(project_key, enabled=0 if enabled else 1)
            console.print(f"[green]✓[/] enabled → {'no' if enabled else 'yes'}")
        elif choice == "approval":
            state.update_project(project_key, approval_required=0 if approval else 1)
            console.print(f"[green]✓[/] approval_required → {'no' if approval else 'yes'}")
        elif choice == "skip_overlap":
            state.update_project(project_key, skip_overlapping_skills=0 if skip_overlap else 1)
            console.print(f"[green]✓[/] skip_overlapping_skills → {'no' if skip_overlap else 'yes'}")
        elif choice == "threshold":
            new_thr = questionary.text(
                "New threshold (≥1):",
                default=str(thr),
                validate=lambda v: (v.strip().isdigit() and int(v) >= 1) or "must be a positive integer",
            ).ask()
            if new_thr and new_thr.strip():
                state.update_project(project_key, threshold_new_prompts=int(new_thr))
                console.print(f"[green]✓[/] threshold_new_prompts → {int(new_thr)}")
        elif choice == "notes":
            new_notes = questionary.text(
                "Notes (free-text, enter to clear):",
                default=notes,
            ).ask()
            if new_notes is not None:
                state.update_project(project_key, notes=new_notes.strip() or None)
                console.print("[green]✓[/] notes updated")


# ─── Header rendering ──────────────────────────────────────────────────────


def _render_header(console: Console, breadcrumb: list[str]) -> None:
    """Breadcrumb + rule above each page — keeps the user oriented when they
    drill multiple levels deep into per-project settings."""
    console.print()
    trail = " › ".join(["watchmen settings", *breadcrumb])
    console.rule(f"[bold]{trail}[/]")
    console.print()
