"""Tests for opt-in auto-install: state schema flag + curate hook + settings parse."""

from __future__ import annotations

from pathlib import Path

import pytest

from watchmen import curate, skill_install as si, state


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    bundles = tmp_path / "bundles"
    claude = tmp_path / "claude" / "skills"
    codex = tmp_path / "codex" / "skills"
    bundles.mkdir(parents=True)
    monkeypatch.setattr(si, "BUNDLES_DIR", bundles)
    monkeypatch.setattr(si, "MANIFEST_PATH", tmp_path / "install_manifest.json")
    monkeypatch.setattr(si, "HARNESS_SKILL_DIRS", {"claude_code": claude, "codex": codex})
    monkeypatch.setattr(state, "STATE_DB", tmp_path / "state.db")
    state.init_db()
    # one curated skill on disk
    sd = bundles / "proj" / "skills" / "alpha"
    sd.mkdir(parents=True)
    (sd / "SKILL.md").write_text("---\nname: alpha\ndescription: x\n---\nbody\n", encoding="utf-8")
    return tmp_path


def test_schema_has_auto_install_column(env):
    state.track_project("proj", str(env / "repo"))
    p = state.get_project("proj")
    assert p["auto_install"] == 0  # defaults off


def test_maybe_auto_install_noop_when_flag_off(env):
    state.track_project("proj", str(env / "repo"))
    curate._maybe_auto_install("proj")
    assert not (env / "claude" / "skills" / "alpha").exists()


def test_maybe_auto_install_installs_when_flag_on(env):
    state.track_project("proj", str(env / "repo"))
    state.update_project("proj", auto_install=1)
    curate._maybe_auto_install("proj")
    assert (env / "claude" / "skills" / "alpha").is_symlink()
    assert (env / "codex" / "skills" / "alpha").is_symlink()


def test_maybe_auto_install_untracked_project_noop(env):
    # No project row at all → no crash, no install.
    curate._maybe_auto_install("ghost")
    assert not (env / "claude" / "skills" / "alpha").exists()


def test_settings_parse_auto_install_bool():
    from watchmen.cli import _parse_setting
    assert _parse_setting("auto_install", "true") == ("auto_install", 1)
    assert _parse_setting("auto_install", "off") == ("auto_install", 0)
    with pytest.raises(ValueError):
        _parse_setting("auto_install", "maybe")
