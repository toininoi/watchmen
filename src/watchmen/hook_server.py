"""Local hook-capture server — receives agent hook POSTs and logs them.

NOT the dashboard. This is the lightweight event sink that coding-agent hooks
fire at (`127.0.0.1:8765/hook`); the human-facing mission-control UI is the
separate FastAPI app in `watchmen.viewer.server` (`127.0.0.1:8979`). Run it
standalone with `python -m watchmen.hook_server`.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from watchmen.paths import EVENTS_DB, EVENTS_JSONL

ROOT = Path(__file__).parent
DB_PATH = EVENTS_DB
JSONL_PATH = EVENTS_JSONL


def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            received_at TEXT NOT NULL,
            event_type TEXT,
            session_id TEXT,
            transcript_path TEXT,
            cwd TEXT,
            tool_name TEXT,
            payload_json TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type)")
    conn.commit()
    conn.close()


app = FastAPI()


@app.post("/hook")
async def receive_hook(request: Request):
    body_bytes = await request.body()
    try:
        payload = json.loads(body_bytes)
    except json.JSONDecodeError:
        payload = {
            "_parse_error": True,
            "_raw": body_bytes.decode("utf-8", errors="replace"),
        }

    received_at = datetime.now(timezone.utc).isoformat()
    event_type = payload.get("hook_event_name")
    session_id = payload.get("session_id")
    transcript_path = payload.get("transcript_path")
    cwd = payload.get("cwd")
    tool_name = payload.get("tool_name")
    payload_str = json.dumps(payload, ensure_ascii=False)

    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO events (received_at, event_type, session_id, transcript_path, cwd, tool_name, payload_json) VALUES (?,?,?,?,?,?,?)",
        (received_at, event_type, session_id, transcript_path, cwd, tool_name, payload_str),
    )
    conn.commit()
    conn.close()

    line = json.dumps({"received_at": received_at, **payload}, ensure_ascii=False)
    JSONL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with JSONL_PATH.open("a") as f:
        f.write(line + "\n")

    print(
        f"[{received_at}] {event_type or '?':<20} session={(session_id or '')[:8]} tool={tool_name or '-'}",
        flush=True,
    )

    return {}


@app.get("/health")
def health():
    return {"ok": True}


if __name__ == "__main__":
    init_db()
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
