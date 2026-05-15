"""Tests for watchmen.paths runtime path migration helpers.

Covers the `kai_claude` → `bundles` rename across the three states a user's
~/.watchmen/ can be in: only-legacy (fresh upgrade), only-new (already
migrated), and both-coexisting (the orphan case fixed in Phase 7.0).
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


@pytest.fixture
def reload_paths(tmp_path: Path, monkeypatch):
    """Reimport watchmen.paths with WATCHMEN_HOME pointed at a temp dir so
    the module-level path constants resolve against fresh state. monkeypatch
    restores the original env var when the test exits."""
    def _reload():
        monkeypatch.setenv("WATCHMEN_HOME", str(tmp_path))
        sys.modules.pop("watchmen.paths", None)
        return importlib.import_module("watchmen.paths")
    return _reload


def test_legacy_alias_migrates_to_new_name(tmp_path: Path, reload_paths):
    """Only kai_claude exists → it's renamed to bundles in place."""
    legacy = tmp_path / "kai_claude"
    (legacy / "kai-frontend").mkdir(parents=True)
    (legacy / "kai-frontend" / "marker.txt").write_text("hello")

    paths = reload_paths()
    result = paths.runtime_path("bundles", legacy_alias="kai_claude")

    assert result == tmp_path / "bundles"
    assert (tmp_path / "bundles" / "kai-frontend" / "marker.txt").read_text() == "hello"
    assert not (tmp_path / "kai_claude").exists()


def test_only_new_name_is_left_alone(tmp_path: Path, reload_paths):
    """Only bundles exists → no-op."""
    (tmp_path / "bundles" / "kai-frontend").mkdir(parents=True)
    (tmp_path / "bundles" / "kai-frontend" / "marker.txt").write_text("hello")

    paths = reload_paths()
    result = paths.runtime_path("bundles", legacy_alias="kai_claude")

    assert result == tmp_path / "bundles"
    assert (tmp_path / "bundles" / "kai-frontend" / "marker.txt").read_text() == "hello"
    assert not (tmp_path / "kai_claude").exists()
    assert not (tmp_path / "kai_claude.legacy").exists()


def test_both_present_archives_legacy(tmp_path: Path, reload_paths, capsys):
    """Both kai_claude and bundles exist → kai_claude gets archived to
    kai_claude.legacy; bundles content is preserved untouched."""
    (tmp_path / "kai_claude" / "stale-proj").mkdir(parents=True)
    (tmp_path / "kai_claude" / "stale-proj" / "old.txt").write_text("legacy")
    (tmp_path / "bundles" / "live-proj").mkdir(parents=True)
    (tmp_path / "bundles" / "live-proj" / "fresh.txt").write_text("live")

    paths = reload_paths()
    result = paths.runtime_path("bundles", legacy_alias="kai_claude")

    assert result == tmp_path / "bundles"
    assert (tmp_path / "bundles" / "live-proj" / "fresh.txt").read_text() == "live"
    assert not (tmp_path / "kai_claude").exists()
    assert (tmp_path / "kai_claude.legacy" / "stale-proj" / "old.txt").read_text() == "legacy"

    err = capsys.readouterr().err
    assert "archived stale" in err
    assert "kai_claude.legacy" in err


def test_archive_is_one_shot(tmp_path: Path, reload_paths):
    """If kai_claude.legacy already exists, don't overwrite it. The stale
    kai_claude/ stays in place so the user can resolve it manually rather
    than us silently nuking earlier archived state."""
    (tmp_path / "bundles").mkdir()
    (tmp_path / "kai_claude" / "x").mkdir(parents=True)
    (tmp_path / "kai_claude.legacy").mkdir(parents=True)
    (tmp_path / "kai_claude.legacy" / "earlier-archive.txt").write_text("from a prior run")

    paths = reload_paths()
    paths.runtime_path("bundles", legacy_alias="kai_claude")

    assert (tmp_path / "kai_claude" / "x").exists()
    assert (tmp_path / "kai_claude.legacy" / "earlier-archive.txt").read_text() == "from a prior run"


def test_neither_exists_creates_dest(tmp_path: Path, reload_paths):
    """Cold start — neither dest nor legacy alias exists."""
    paths = reload_paths()
    result = paths.runtime_path("bundles", legacy_alias="kai_claude")

    assert result == tmp_path / "bundles"
    assert not (tmp_path / "kai_claude").exists()
