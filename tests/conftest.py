"""Shared test setup — runs once per pytest session.

Inserts `src/` onto sys.path so the test loop works even without a prior
`uv sync` or editable install. Exposes ROOT + SRC as module-level constants
so individual tests can read package files (plist/unit templates, README
hygiene) without re-deriving the path.

Also pins the active LLM provider to "openrouter" for the entire test
session. Without this, a developer who has run `watchmen settings provider
anthropic` (writes to ~/.config/watchmen/.env) would see tests fail —
config.active_provider() reads that file at call time, so any test that
builds an Agent without an explicit provider would pick up whatever the
developer last configured. Pinning here means tests behave the same in
CI, on a fresh checkout, and on a machine with multiple keys configured.
"""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "watchmen"

# The editable install puts `watchmen` on sys.path via uv's .pth file. We
# still nudge `src/` into place so a fresh checkout `pytest tests/` works
# with zero ceremony — useful for CI and one-shot invocations.
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))


@pytest.fixture(autouse=True)
def _pin_provider_to_openrouter(monkeypatch):
    """Default test session to OpenRouter as the active provider so tests
    that build an Agent without explicit `provider=` get the expected
    headers / endpoint regardless of the developer's local .env.

    Tests that need a different provider should set their own
    `monkeypatch.setenv("WATCHMEN_PROVIDER", ...)` — the per-test monkeypatch
    scope wins over this autouse fixture for the duration of that test."""
    monkeypatch.setenv("WATCHMEN_PROVIDER", "openrouter")
