"""Walk every coding-agent transcript on this device and load it into a queryable SQLite corpus.

Dispatches to per-agent adapters in `adapters/` (claude_code, codex, …). Each
adapter knows its own install path and JSONL schema; this module just iterates
discover() → scan() and stores the normalized rows.

  uv run corpus.py scan       # one-time ingest from all detected agents
  uv run corpus.py overview   # high-level stats per project, prompt length, tool usage
"""

import sqlite3
import sys
from pathlib import Path

from adapters import ADAPTERS

ROOT = Path(__file__).parent
DB_PATH = ROOT / "corpus.db"


def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(
        """
        DROP TABLE IF EXISTS sessions;
        DROP TABLE IF EXISTS prompts;
        DROP TABLE IF EXISTS tool_calls;

        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY,
            project_dir TEXT,
            transcript_path TEXT,
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

        CREATE TABLE prompts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            timestamp TEXT,
            text TEXT,
            word_count INTEGER,
            char_count INTEGER,
            is_first_in_session INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX idx_prompts_session ON prompts(session_id);

        CREATE TABLE tool_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            timestamp TEXT,
            tool_name TEXT,
            is_error INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX idx_tool_calls_session ON tool_calls(session_id);
        CREATE INDEX idx_tool_calls_tool ON tool_calls(tool_name);
        """
    )
    conn.commit()
    return conn


def scan_all() -> None:
    conn = init_db()
    per_adapter: list[tuple[str, list[dict]]] = []
    for adapter in ADAPTERS:
        files = list(adapter.discover())
        per_adapter.append((adapter.NAME, files))
        print(f"  {adapter.NAME}: {len(files)} transcripts", flush=True)
    total = sum(len(f) for _, f in per_adapter)
    print(f"Found {total} transcripts across {len(per_adapter)} adapter(s). Scanning...", flush=True)

    failed = 0
    seen = 0
    for adapter, files in zip(ADAPTERS, [f for _, f in per_adapter]):
        for entry in files:
            seen += 1
            if seen % 200 == 0:
                print(f"  [{seen}/{total}]", flush=True)
            try:
                session, prompts, tools = adapter.scan(entry)
            except Exception as ex:
                failed += 1
                if failed < 5:
                    print(f"  ! failed {entry['path'].name}: {ex}", flush=True)
                continue

            conn.execute(
                """INSERT OR REPLACE INTO sessions
                   (session_id, project_dir, transcript_path, started_at, ended_at, duration_seconds,
                    is_subagent, parent_session_id, message_count, user_prompt_count,
                    assistant_text_count, assistant_thinking_count, tool_use_count, tool_error_count, models,
                    input_tokens, cache_creation_tokens, cache_read_tokens, output_tokens, model_dominant, cost_usd,
                    agent)
                   VALUES (:session_id, :project_dir, :transcript_path, :started_at, :ended_at, :duration_seconds,
                           :is_subagent, :parent_session_id, :message_count, :user_prompt_count,
                           :assistant_text_count, :assistant_thinking_count, :tool_use_count, :tool_error_count, :models,
                           :input_tokens, :cache_creation_tokens, :cache_read_tokens, :output_tokens, :model_dominant, :cost_usd,
                           :agent)""",
                session,
            )
            if prompts:
                conn.executemany(
                    """INSERT INTO prompts (session_id, timestamp, text, word_count, char_count, is_first_in_session)
                       VALUES (:session_id, :timestamp, :text, :word_count, :char_count, :is_first_in_session)""",
                    prompts,
                )
            if tools:
                conn.executemany(
                    """INSERT INTO tool_calls (session_id, timestamp, tool_name, is_error)
                       VALUES (:session_id, :timestamp, :tool_name, :is_error)""",
                    tools,
                )

    conn.commit()
    conn.close()
    print(f"Done. {failed} transcripts failed. DB at {DB_PATH}")


def overview() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    print("=== SESSION COUNTS ===")
    rows = conn.execute("SELECT is_subagent, COUNT(*) AS n FROM sessions GROUP BY is_subagent").fetchall()
    for r in rows:
        kind = "subagent" if r["is_subagent"] else "main"
        print(f"  {kind:<10} {r['n']}")

    print("\n=== BY AGENT (main sessions) ===")
    rows = conn.execute(
        """SELECT agent, COUNT(*) AS sessions,
                  COALESCE(SUM(user_prompt_count), 0) AS prompts,
                  ROUND(COALESCE(SUM(cost_usd), 0), 2) AS cost
           FROM sessions WHERE is_subagent = 0
           GROUP BY agent ORDER BY sessions DESC"""
    ).fetchall()
    for r in rows:
        print(f"  {r['agent']:<14} sessions={r['sessions']:>5}  prompts={r['prompts']:>6}  cost=${r['cost']}")

    print("\n=== TOP 20 PROJECTS BY MAIN SESSION COUNT ===")
    rows = conn.execute(
        """
        SELECT project_dir,
               COUNT(*) AS sessions,
               COALESCE(SUM(user_prompt_count), 0) AS prompts,
               COALESCE(SUM(tool_use_count), 0) AS tools,
               COALESCE(SUM(tool_error_count), 0) AS errors,
               ROUND(COALESCE(SUM(duration_seconds), 0)/3600.0, 1) AS hours
        FROM sessions WHERE is_subagent = 0
        GROUP BY project_dir
        ORDER BY sessions DESC, prompts DESC
        LIMIT 20
        """
    ).fetchall()
    print(f"  {'project':<70}  {'sess':>4} {'prompts':>8} {'tools':>7} {'errors':>7} {'hours':>6}")
    for r in rows:
        print(
            f"  {r['project_dir'][:70]:<70}  {r['sessions']:>4} {r['prompts']:>8} {r['tools']:>7} {r['errors']:>7} {r['hours']:>6}"
        )

    print("\n=== MAIN-SESSION SIZE DISTRIBUTION (prompts, tools, duration) ===")
    r = conn.execute(
        """
        SELECT COUNT(*) AS n,
               ROUND(AVG(user_prompt_count), 1) AS avg_prompts,
               MAX(user_prompt_count) AS max_prompts,
               ROUND(AVG(tool_use_count), 1) AS avg_tools,
               MAX(tool_use_count) AS max_tools,
               ROUND(AVG(duration_seconds)/60.0, 1) AS avg_min,
               ROUND(MAX(duration_seconds)/60.0, 1) AS max_min
        FROM sessions WHERE is_subagent = 0
        """
    ).fetchone()
    print(f"  sessions={r['n']}  avg_prompts={r['avg_prompts']}  max_prompts={r['max_prompts']}")
    print(f"  avg_tools={r['avg_tools']}  max_tools={r['max_tools']}")
    print(f"  avg_minutes={r['avg_min']}  max_minutes={r['max_min']}")

    print("\n=== PROMPT LENGTH (main sessions) ===")
    r = conn.execute(
        """
        SELECT COUNT(*) AS n,
               ROUND(AVG(word_count), 1) AS avg_words,
               MIN(word_count) AS min_words,
               MAX(word_count) AS max_words
        FROM prompts p JOIN sessions s ON p.session_id = s.session_id
        WHERE s.is_subagent = 0
        """
    ).fetchone()
    print(f"  total_prompts={r['n']}  avg={r['avg_words']}  min={r['min_words']}  max={r['max_words']}")

    print("\n=== TOOL USAGE (main sessions) ===")
    rows = conn.execute(
        """
        SELECT t.tool_name, COUNT(*) AS n,
               ROUND(100.0 * COUNT(*) / (SELECT COUNT(*) FROM tool_calls tc JOIN sessions s2 ON tc.session_id = s2.session_id WHERE s2.is_subagent = 0), 1) AS pct
        FROM tool_calls t JOIN sessions s ON t.session_id = s.session_id
        WHERE s.is_subagent = 0
        GROUP BY t.tool_name
        ORDER BY n DESC
        """
    ).fetchall()
    for r in rows:
        print(f"  {r['tool_name']:<25} {r['n']:>6}  ({r['pct']}%)")

    print("\n=== MODELS (main sessions) ===")
    rows = conn.execute(
        "SELECT models, COUNT(*) AS n FROM sessions WHERE is_subagent = 0 GROUP BY models ORDER BY n DESC"
    ).fetchall()
    for r in rows:
        print(f"  {r['models']:<70}  {r['n']}")

    conn.close()


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "overview"
    if cmd == "scan":
        scan_all()
    elif cmd == "overview":
        overview()
    else:
        print("usage: corpus.py [scan|overview]")


if __name__ == "__main__":
    main()
