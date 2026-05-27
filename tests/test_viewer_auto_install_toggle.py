"""HTTP test for the viewer's per-project auto_install toggle endpoint."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from watchmen import state
from watchmen.viewer import server


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    db = tmp_path / "state.db"
    # Both the state module and the viewer's own DB reader resolve STATE_DB.
    monkeypatch.setattr(state, "STATE_DB", db)
    monkeypatch.setattr(server, "STATE_DB", db)
    state.init_db()
    state.track_project("proj", str(tmp_path / "repo"))
    return TestClient(server.app)


def test_toggle_turns_auto_install_on_then_off(client):
    assert state.get_project("proj")["auto_install"] == 0

    r = client.post("/p/proj/auto-install/toggle", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/p/proj"
    assert state.get_project("proj")["auto_install"] == 1

    client.post("/p/proj/auto-install/toggle", follow_redirects=False)
    assert state.get_project("proj")["auto_install"] == 0


def test_toggle_unknown_project_404(client):
    r = client.post("/p/ghost/auto-install/toggle", follow_redirects=False)
    assert r.status_code == 404
