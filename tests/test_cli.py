"""Smoke + drift tests for the CLI surface.

cli.py is the largest module in the repo and had no tests — a typo in a
subparser wiring or a command added to the help groups but never registered
(or vice versa) would ship silently. Now that the parser is extracted into
`build_parser()`, we can build it without running a command and assert the
structural invariants that dispatch depends on.
"""

from __future__ import annotations

import argparse

from watchmen import cli


def _subparsers_action(parser: argparse.ArgumentParser) -> argparse._SubParsersAction:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    raise AssertionError("no subparsers action on the root parser")


def test_build_parser_constructs():
    parser = cli.build_parser()
    assert isinstance(parser, argparse.ArgumentParser)


def test_every_subcommand_has_a_handler():
    """Each registered subcommand must set `func` (the dispatch target in
    main()), except pure command *groups* that delegate to a sub-subparser
    (daemon, viewer, hooks, statusline, plugin, launchd, settings)."""
    parser = cli.build_parser()
    sub = _subparsers_action(parser)
    group_only = {"daemon", "viewer", "hooks", "statusline", "plugin", "launchd", "settings"}
    missing = []
    for name, subparser in sub.choices.items():
        has_func = subparser.get_default("func") is not None
        if not has_func and name not in group_only:
            missing.append(name)
    assert not missing, f"subcommands with no func handler: {missing}"


def test_help_groups_reference_only_real_commands():
    """The grouped --help renderer lists commands by name. Every name it
    advertises must be a registered subcommand — otherwise --help promises a
    command that doesn't exist. (Hidden aliases may be registered without
    appearing in a group, so the check is groups ⊆ registered, not equality.)"""
    parser = cli.build_parser()
    registered = set(_subparsers_action(parser).choices)
    advertised = {name for _group, items in cli._HELP_GROUPS for name, _help in items}
    orphan = advertised - registered
    assert not orphan, f"help groups advertise unregistered commands: {orphan}"


def test_known_commands_are_registered():
    """Anchor a few load-bearing commands so an accidental drop is caught."""
    registered = set(_subparsers_action(cli.build_parser()).choices)
    for name in ("init", "doctor", "show", "skills", "subagents", "metrics", "insights"):
        assert name in registered, f"missing subcommand: {name}"


def test_version_flag_exits_zero(capsys):
    """`watchmen --version` should print and exit(0), not dispatch."""
    import pytest
    with pytest.raises(SystemExit) as ei:
        cli.main(["--version"])
    assert ei.value.code == 0
    assert "watchmen" in capsys.readouterr().out
