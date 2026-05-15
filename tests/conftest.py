"""Shared test setup — runs once per pytest session.

Inserts `src/` onto sys.path so the test loop works even without a prior
`uv sync` or editable install. Exposes ROOT + SRC as module-level constants
so individual tests can read package files (plist/unit templates, README
hygiene) without re-deriving the path.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "watchmen"

# The editable install puts `watchmen` on sys.path via uv's .pth file. We
# still nudge `src/` into place so a fresh checkout `pytest tests/` works
# with zero ceremony — useful for CI and one-shot invocations.
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
