"""Seed a synthetic WATCHMEN_HOME with 16 weeks of believable session data.

Renders a clean impact-card screenshot for marketing without touching the
developer's real corpus. The story:

- One tracked project, `kestrel-api`, with a first-curator-run dated 8 weeks ago.
- ~10 sessions per week for the full 16 weeks of history.
- Pre-treatment: high tool-error rate (median ~4/session) — a noisy repo.
- Post-treatment: error rate decays week-over-week toward ~0.8/session.
- Other per-session signals (prompt count, cost) also drift down post-treatment
  so the pre/post stats table reads as "fewer prompts to converge, less spend
  per session" without being implausibly clean.

Usage:
    WATCHMEN_HOME=/tmp/watchmen-mock uv run python scripts/seed_mock_corpus.py
    WATCHMEN_HOME=/tmp/watchmen-mock uv run watchmen viewer run
    # then open http://127.0.0.1:8979/p/kestrel-api

Re-runs are safe: drops + recreates both DBs from scratch each invocation.
"""

from __future__ import annotations

import os
import random
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path


PROJECT_KEY = "kestrel-api"
PROJECT_DIR = "/Users/demo/Development/kestrel-api"
WEEKS_TOTAL = 16
WEEKS_PRE_TREATMENT = 8
SESSIONS_PER_WEEK = 10
SEED = 42  # deterministic so screenshot looks the same on re-runs


def main() -> int:
    rng = random.Random(SEED)
    home = Path(os.environ.get("WATCHMEN_HOME", "/tmp/watchmen-mock")).expanduser()
    home.mkdir(parents=True, exist_ok=True)
    print(f"Seeding mock corpus into {home}")

    # Treatment date: 8 weeks ago, anchored to a Monday so the weekly buckets
    # split cleanly on the chart.
    now = datetime.now(timezone.utc)
    treatment = (now - timedelta(weeks=WEEKS_PRE_TREATMENT)).replace(
        hour=12, minute=0, second=0, microsecond=0,
    )

    # ── corpus.db ───────────────────────────────────────────────────────────
    corpus_path = home / "corpus.db"
    if corpus_path.exists():
        corpus_path.unlink()
    cc = sqlite3.connect(corpus_path)
    cc.executescript("""
    CREATE TABLE sessions (
        session_id TEXT PRIMARY KEY,
        project_dir TEXT,
        transcript_path TEXT,
        file_mtime REAL,
        started_at TEXT,
        ended_at TEXT,
        duration_seconds REAL,
        is_subagent INTEGER NOT NULL DEFAULT 0,
        parent_session_id TEXT,
        message_count INTEGER NOT NULL DEFAULT 0,
        user_prompt_count INTEGER NOT NULL DEFAULT 0,
        assistant_text_count INTEGER NOT NULL DEFAULT 0,
        assistant_thinking_count INTEGER NOT NULL DEFAULT 0,
        tool_use_count INTEGER NOT NULL DEFAULT 0,
        tool_error_count INTEGER NOT NULL DEFAULT 0,
        models TEXT,
        input_tokens INTEGER NOT NULL DEFAULT 0,
        cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
        cache_read_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        model_dominant TEXT,
        cost_usd REAL NOT NULL DEFAULT 0,
        agent TEXT NOT NULL DEFAULT 'claude_code'
    );
    CREATE INDEX idx_sessions_project ON sessions(project_dir);
    CREATE INDEX idx_sessions_subagent ON sessions(is_subagent);
    CREATE INDEX idx_sessions_agent ON sessions(agent);
    CREATE INDEX idx_sessions_path ON sessions(transcript_path);

    CREATE TABLE prompts (
        session_id TEXT,
        timestamp TEXT,
        content TEXT,
        FOREIGN KEY (session_id) REFERENCES sessions(session_id)
    );
    CREATE TABLE tool_calls (
        session_id TEXT,
        tool_name TEXT,
        is_error INTEGER NOT NULL DEFAULT 0,
        FOREIGN KEY (session_id) REFERENCES sessions(session_id)
    );
    """)

    total_sessions = 0
    for week_offset in range(-WEEKS_PRE_TREATMENT, WEEKS_TOTAL - WEEKS_PRE_TREATMENT):
        # week_offset < 0 → pre-treatment, week_offset >= 0 → post-treatment
        week_anchor = treatment + timedelta(weeks=week_offset)

        # Target tool-errors-per-session curve:
        #   pre-treatment (weeks -8..-1): noisy, hovers ~3.5–4.5
        #   post-treatment (weeks 0..7): decays exponentially toward 0.8
        if week_offset < 0:
            target_errors = 4.0 + 0.5 * rng.gauss(0, 1)
        else:
            # Exponential decay with a floor — feels organic, not too clean.
            decay_steps = week_offset
            target_errors = 0.8 + (4.0 - 0.8) * (0.65 ** decay_steps)
            target_errors += 0.3 * rng.gauss(0, 1)  # weekly jitter

        # Same shape for prompts: pre is noisy (~20/session), post drops (~14)
        target_prompts = 20.0 if week_offset < 0 else max(11.0, 20.0 - 1.1 * week_offset)
        target_cost = 2.4 if week_offset < 0 else max(0.9, 2.4 - 0.18 * week_offset)

        for i in range(SESSIONS_PER_WEEK + rng.randint(-2, 2)):
            # Spread sessions across the week, weighted toward weekdays
            day_offset = rng.choice([0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 6])
            hour = rng.randint(9, 22)
            minute = rng.randint(0, 59)
            started = week_anchor + timedelta(days=day_offset, hours=hour - 12, minutes=minute)
            duration = rng.uniform(180, 2400)  # 3–40 min
            ended = started + timedelta(seconds=duration)

            errs = max(0, int(round(rng.gauss(target_errors, 0.8))))
            prompts = max(2, int(round(rng.gauss(target_prompts, 4.0))))
            tool_uses = prompts * rng.randint(2, 5)
            cost = max(0.05, rng.gauss(target_cost, 0.4))
            input_tokens = int(prompts * rng.uniform(2500, 4500))
            output_tokens = int(prompts * rng.uniform(800, 1500))
            cache_read = int(input_tokens * rng.uniform(0.35, 0.7))

            sid = str(uuid.UUID(int=rng.getrandbits(128)))
            cc.execute(
                "INSERT INTO sessions (session_id, project_dir, transcript_path, file_mtime, "
                "started_at, ended_at, duration_seconds, is_subagent, message_count, "
                "user_prompt_count, assistant_text_count, tool_use_count, tool_error_count, "
                "models, model_dominant, input_tokens, cache_creation_tokens, cache_read_tokens, "
                "output_tokens, cost_usd, agent) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'claude_code')",
                (
                    sid, PROJECT_DIR, f"/fake/transcripts/{sid}.jsonl", started.timestamp(),
                    started.isoformat(), ended.isoformat(), duration,
                    prompts * 3, prompts, prompts, tool_uses, errs,
                    "claude-sonnet-4-6", "claude-sonnet-4-6",
                    input_tokens, 0, cache_read, output_tokens, cost,
                ),
            )
            total_sessions += 1

    cc.commit()
    cc.close()
    print(f"  corpus.db: {total_sessions} sessions across {WEEKS_TOTAL} weeks")

    # ── state.db ───────────────────────────────────────────────────────────
    state_path = home / "state.db"
    if state_path.exists():
        state_path.unlink()
    sc = sqlite3.connect(state_path)
    # Init via the package's own helper so we get the exact schema the viewer expects
    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from watchmen import state as _state
    _state.STATE_DB = state_path  # type: ignore[attr-defined]
    sc.close()
    # Re-open via the state module so init_db lays out everything
    import importlib
    from watchmen import paths as _paths
    _paths.WATCHMEN_HOME = home  # type: ignore[attr-defined]
    _paths.STATE_DB = state_path  # type: ignore[attr-defined]
    importlib.reload(_state)
    # The reload reset STATE_DB; force it again to the temp path.
    _state.STATE_DB = state_path  # type: ignore[attr-defined]
    _state.init_db()
    _state.track_project(PROJECT_KEY, PROJECT_DIR, threshold=30)
    # Backdate the project's update_at + curator markers
    _state.update_project(
        PROJECT_KEY,
        last_curator_run=treatment.isoformat(),
        last_curator_skill_count=8,
        last_analyst_day=(treatment + timedelta(days=2)).date().isoformat(),
        last_analyst_run=(treatment + timedelta(days=2)).isoformat(),
    )
    # And the runs row that `_treatment_date_for_project` actually queries
    with _state.conn() as conn:
        conn.execute(
            "INSERT INTO runs (project_key, kind, started_at, ended_at, status, notes) "
            "VALUES (?, ?, ?, ?, 'ok', 'mock-seed: initial curator')",
            (PROJECT_KEY, "curator", treatment.isoformat(),
             (treatment + timedelta(minutes=45)).isoformat()),
        )
        # A few subsequent runs so /runs has something to render too
        for n in range(1, 5):
            t = treatment + timedelta(weeks=n)
            conn.execute(
                "INSERT INTO runs (project_key, kind, started_at, ended_at, status) "
                "VALUES (?, ?, ?, ?, 'ok')",
                (PROJECT_KEY, "analyst", t.isoformat(),
                 (t + timedelta(minutes=8)).isoformat()),
            )
        conn.commit()
    print(f"  state.db: tracked '{PROJECT_KEY}', first curator at {treatment.date()}")

    # ── bundles/ — a tiny stub so the project page has something to show
    # next to the impact card (CLAUDE.md + a sample skill).
    bundles = home / "bundles" / PROJECT_KEY
    skills = bundles / "skills" / "ship-pr"
    skills.mkdir(parents=True, exist_ok=True)
    (bundles / "CLAUDE.md").write_text(
        "# kestrel-api\n\nSample workspace brief — generated for screenshots.\n\n"
        "## When working in this repo\n\n"
        "- Run the test suite via `uv run pytest tests/`\n"
        "- Type-check with `uv run mypy src/`\n"
        "- Format on save; pre-commit handles the rest\n"
    )
    (skills / "SKILL.md").write_text(
        "---\nname: ship-pr\ndescription: Open a PR from the current branch with the\n"
        "standard test plan + summary template.\ntrigger_phrases: [open a PR, ship this]\n---\n\n"
        "# ship-pr\n\nFollow the team's PR conventions when shipping changes from this repo.\n"
    )
    print("  bundles: stubbed CLAUDE.md + one skill")

    print()
    print("Done. Next steps:")
    print(f"  export WATCHMEN_HOME={home}")
    print("  uv run watchmen viewer run")
    print(f"  open http://127.0.0.1:8979/p/{PROJECT_KEY}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
