#!/usr/bin/env python3
"""Cold-start smoke tests.

Catches the class of bugs that only appear on a totally fresh install — the
ones the developer never hits because their dev machine already has corpus.db,
state.db, etc. populated.

Specifically covers:
  - state.init_db() must be called before list_projects (regression test for
    the bug Eren hit on first install).
  - Model price lookup matches real API model names ('claude-opus-4-7', dash
    format, not 'opus-4.7' dot format) and routes to the right tier.
  - turn_cost_usd math matches Anthropic's published worked example.

Run via: `python tests/smoke.py`  (no pytest dep). CI runs the same command.
Exit code 0 = all pass, 1 = any failure.
"""

import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS = 0
FAIL = 0


def check(name: str, fn):
    global PASS, FAIL
    try:
        fn()
        print(f"  ✓ {name}")
        PASS += 1
    except Exception:
        print(f"  ✗ {name}")
        traceback.print_exc()
        FAIL += 1


# ─── State / onboard tests ──────────────────────────────────────────────────


def _with_tmp_state(fn):
    """Run fn() with state.STATE_DB redirected at a fresh temp file."""
    import state
    with tempfile.TemporaryDirectory() as td:
        orig = state.STATE_DB
        state.STATE_DB = Path(td) / "state.db"
        try:
            fn()
        finally:
            state.STATE_DB = orig


def test_state_init_idempotent():
    import state
    def go():
        state.init_db()
        state.init_db()  # second call must not error
        # And list_projects after should be empty.
        assert state.list_projects() == []
    _with_tmp_state(go)


def test_list_projects_without_init_raises():
    """The exact failure mode Eren hit: querying projects on a freshly-created
    state.db before init_db() runs must raise 'no such table'. If this changes
    (e.g. someone makes the conn auto-init), the test catches the silent shift."""
    import state
    def go():
        try:
            state.list_projects()
        except Exception as e:
            assert "no such table" in str(e), f"expected 'no such table' error, got: {e}"
            return
        raise AssertionError("expected OperationalError, got none")
    _with_tmp_state(go)


def test_onboard_calls_init_db():
    """The actual fix for Eren's bug: onboard.run must call state.init_db
    before doing anything that touches the projects table."""
    src = (ROOT / "onboard.py").read_text()
    assert "state.init_db()" in src, "onboard.run must call state.init_db()"
    # Ordering: init_db must appear before project_candidates is invoked.
    init_pos = src.index("state.init_db()")
    pc_pos = src.index("project_candidates(console)")
    assert init_pos < pc_pos, "state.init_db() must run before project_candidates"


# ─── Metrics / pricing tests ────────────────────────────────────────────────


def test_price_for_dash_api_names():
    """Real Anthropic API model names use dashes between version components.
    The normalizer must map these to our dot-separated price keys."""
    import metrics
    cases = [
        ("claude-opus-4-7",          (5.0,  6.25,  10.0, 0.5,  25.0)),
        ("claude-opus-4-6",          (5.0,  6.25,  10.0, 0.5,  25.0)),
        ("claude-opus-4-1",          (15.0, 18.75, 30.0, 1.5,  75.0)),
        ("claude-opus-4",            (15.0, 18.75, 30.0, 1.5,  75.0)),
        ("claude-sonnet-4-6",        (3.0,  3.75,  6.0,  0.3,  15.0)),
        ("claude-sonnet-4-20250514", (3.0,  3.75,  6.0,  0.3,  15.0)),
        ("claude-haiku-4-5",         (1.0,  1.25,  2.0,  0.1,  5.0)),
    ]
    for model, expected in cases:
        got = metrics.price_for_model(model)
        assert got == expected, f"{model}: expected {expected}, got {got}"


def test_price_for_unknown_falls_back():
    import metrics
    assert metrics.price_for_model(None) == metrics.DEFAULT_PRICE
    assert metrics.price_for_model("") == metrics.DEFAULT_PRICE
    # Unknown future Claude model falls back via family pattern.
    assert metrics.price_for_model("claude-opus-99-99") == metrics.MODEL_PRICES["opus-4.7"]


def test_turn_cost_worked_example():
    """Anthropic's pricing page worked example, Opus 4.7. The docs total ($0.705,
    $0.525) includes Managed Agents session runtime ($0.08/hr), which we don't
    bill — so we compare against the pure token portion.

    Source: https://platform.claude.com/docs/en/about-claude/pricing#worked-example"""
    import metrics
    # 50k input + 15k output, no caching → docs $0.705 incl. runtime; tokens-only $0.625
    cost = metrics.turn_cost_usd("claude-opus-4-7", 50_000, 0, 0, 0, 15_000)
    expected = 0.625
    assert abs(cost - expected) < 0.0001, f"50k+15k: expected {expected}, got {cost}"

    # 10k uncached input + 40k cache reads + 15k output → docs $0.525 incl. runtime;
    # tokens-only $0.445 = 0.05 (input) + 0.02 (cache read) + 0.375 (output)
    cost = metrics.turn_cost_usd("claude-opus-4-7", 10_000, 0, 0, 40_000, 15_000)
    expected = 0.445
    assert abs(cost - expected) < 0.0001, f"10k+40k cached+15k: expected {expected}, got {cost}"


def test_cache_5m_vs_1h_are_different():
    """5m cache write = 1.25× input. 1h cache write = 2× input. They must not
    be priced the same."""
    import metrics
    cost_5m = metrics.turn_cost_usd("claude-opus-4-7", 0, 1_000_000, 0, 0, 0)
    cost_1h = metrics.turn_cost_usd("claude-opus-4-7", 0, 0, 1_000_000, 0, 0)
    assert cost_5m < cost_1h, f"5m must cost less than 1h, got 5m=${cost_5m} 1h=${cost_1h}"
    assert abs(cost_5m - 6.25) < 0.01
    assert abs(cost_1h - 10.00) < 0.01


# ─── Driver ─────────────────────────────────────────────────────────────────


def main() -> int:
    print(f"watchmen smoke tests · python {sys.version.split()[0]}")
    print(f"repo: {ROOT}")
    print()
    print("State / onboard:")
    check("state.init_db is idempotent",          test_state_init_idempotent)
    check("list_projects raises before init",     test_list_projects_without_init_raises)
    check("onboard.run calls state.init_db first", test_onboard_calls_init_db)
    print()
    print("Metrics / pricing:")
    check("price_for_model handles API dash names", test_price_for_dash_api_names)
    check("price_for_model falls back cleanly",     test_price_for_unknown_falls_back)
    check("turn_cost matches docs worked example",  test_turn_cost_worked_example)
    check("5m vs 1h cache writes priced differently", test_cache_5m_vs_1h_are_different)

    print()
    if FAIL:
        print(f"  ✗ {FAIL} failed, {PASS} passed")
        return 1
    print(f"  ✓ all {PASS} passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
