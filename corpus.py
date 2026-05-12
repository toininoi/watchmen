"""Walk every Claude Code transcript on this device and load it into a queryable SQLite corpus.

  uv run corpus.py scan       # one-time ingest of ~/.claude/projects
  uv run corpus.py overview   # high-level stats per project, prompt length, tool usage
"""

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

PROJECTS_DIR = Path.home() / ".claude" / "projects"
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
            model_dominant TEXT
        );
        CREATE INDEX idx_sessions_project ON sessions(project_dir);
        CREATE INDEX idx_sessions_subagent ON sessions(is_subagent);

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


def parse_iso(ts: str | None):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def scan_transcript(path: Path, project_dir: str, is_subagent: bool, parent_sid: str | None):
    session = {
        "session_id": path.stem,
        "project_dir": project_dir,
        "transcript_path": str(path),
        "started_at": None,
        "ended_at": None,
        "duration_seconds": None,
        "is_subagent": int(is_subagent),
        "parent_session_id": parent_sid,
        "message_count": 0,
        "user_prompt_count": 0,
        "assistant_text_count": 0,
        "assistant_thinking_count": 0,
        "tool_use_count": 0,
        "tool_error_count": 0,
        "models": "[]",
        "input_tokens": 0,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
        "output_tokens": 0,
        "model_dominant": None,
    }
    prompts: list = []
    tool_calls: list = []
    models: set[str] = set()
    model_output_tokens: dict[str, int] = {}  # for picking dominant by output volume
    is_first = True

    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue

            ts = e.get("timestamp")
            if ts:
                if not session["started_at"] or ts < session["started_at"]:
                    session["started_at"] = ts
                if not session["ended_at"] or ts > session["ended_at"]:
                    session["ended_at"] = ts

            etype = e.get("type")
            if etype not in ("user", "assistant"):
                continue

            session["message_count"] += 1
            msg = e.get("message", {}) or {}
            content = msg.get("content")

            if etype == "user":
                if isinstance(content, str):
                    text = content
                    prompts.append(
                        {
                            "session_id": session["session_id"],
                            "timestamp": ts,
                            "text": text,
                            "word_count": len(text.split()),
                            "char_count": len(text),
                            "is_first_in_session": int(is_first),
                        }
                    )
                    is_first = False
                    session["user_prompt_count"] += 1
                elif isinstance(content, list):
                    text_parts: list[str] = []
                    saw_tool_result = False
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type")
                        if btype == "text":
                            text_parts.append(block.get("text", ""))
                        elif btype == "tool_result":
                            saw_tool_result = True
                            if block.get("is_error"):
                                session["tool_error_count"] += 1
                    if text_parts and not saw_tool_result:
                        text = "\n".join(text_parts)
                        prompts.append(
                            {
                                "session_id": session["session_id"],
                                "timestamp": ts,
                                "text": text,
                                "word_count": len(text.split()),
                                "char_count": len(text),
                                "is_first_in_session": int(is_first),
                            }
                        )
                        is_first = False
                        session["user_prompt_count"] += 1

            elif etype == "assistant":
                model = msg.get("model")
                if model:
                    models.add(model)
                usage = msg.get("usage") or {}
                if isinstance(usage, dict):
                    in_t = int(usage.get("input_tokens") or 0)
                    cc_t = int(usage.get("cache_creation_input_tokens") or 0)
                    cr_t = int(usage.get("cache_read_input_tokens") or 0)
                    out_t = int(usage.get("output_tokens") or 0)
                    session["input_tokens"] += in_t
                    session["cache_creation_tokens"] += cc_t
                    session["cache_read_tokens"] += cr_t
                    session["output_tokens"] += out_t
                    if model:
                        model_output_tokens[model] = model_output_tokens.get(model, 0) + out_t
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type")
                        if btype == "text":
                            session["assistant_text_count"] += 1
                        elif btype == "thinking":
                            session["assistant_thinking_count"] += 1
                        elif btype == "tool_use":
                            session["tool_use_count"] += 1
                            tool_calls.append(
                                {
                                    "session_id": session["session_id"],
                                    "timestamp": ts,
                                    "tool_name": block.get("name", "?"),
                                    "is_error": 0,
                                }
                            )

    session["models"] = json.dumps(sorted(models))
    if model_output_tokens:
        session["model_dominant"] = max(model_output_tokens.items(), key=lambda kv: kv[1])[0]
    a = parse_iso(session["started_at"])
    b = parse_iso(session["ended_at"])
    if a and b:
        session["duration_seconds"] = (b - a).total_seconds()
    return session, prompts, tool_calls


def collect_files():
    main_files: list = []
    sub_files: list = []
    for project_dir in PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        for jsonl in project_dir.glob("*.jsonl"):
            main_files.append((jsonl, project_dir.name, False, None))
        for sub_dir in project_dir.iterdir():
            if not sub_dir.is_dir():
                continue
            parent_sid = sub_dir.name
            sub_path = sub_dir / "subagents"
            if not sub_path.exists():
                continue
            for sub_jsonl in sub_path.glob("*.jsonl"):
                sub_files.append((sub_jsonl, project_dir.name, True, parent_sid))
    return main_files, sub_files


def scan_all() -> None:
    conn = init_db()
    main_files, sub_files = collect_files()
    total = len(main_files) + len(sub_files)
    print(f"Found {len(main_files)} main + {len(sub_files)} subagent = {total} transcripts. Scanning...", flush=True)

    failed = 0
    for i, (path, project_dir, is_sub, parent) in enumerate(main_files + sub_files):
        if i and i % 200 == 0:
            print(f"  [{i}/{total}]", flush=True)
        try:
            session, prompts, tools = scan_transcript(path, project_dir, is_sub, parent)
        except Exception as ex:
            failed += 1
            if failed < 5:
                print(f"  ! failed {path.name}: {ex}", flush=True)
            continue

        conn.execute(
            """INSERT OR REPLACE INTO sessions
               (session_id, project_dir, transcript_path, started_at, ended_at, duration_seconds,
                is_subagent, parent_session_id, message_count, user_prompt_count,
                assistant_text_count, assistant_thinking_count, tool_use_count, tool_error_count, models,
                input_tokens, cache_creation_tokens, cache_read_tokens, output_tokens, model_dominant)
               VALUES (:session_id, :project_dir, :transcript_path, :started_at, :ended_at, :duration_seconds,
                       :is_subagent, :parent_session_id, :message_count, :user_prompt_count,
                       :assistant_text_count, :assistant_thinking_count, :tool_use_count, :tool_error_count, :models,
                       :input_tokens, :cache_creation_tokens, :cache_read_tokens, :output_tokens, :model_dominant)""",
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
