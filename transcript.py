"""Read a Claude Code session transcript JSONL and print a human-readable conversation.

  uv run transcript.py                    # latest session seen by the observer
  uv run transcript.py <session_prefix>   # session by prefix (e.g. dd78515a)
  uv run transcript.py --list             # list captured sessions
  uv run transcript.py --thinking         # include thinking blocks
  uv run transcript.py --full             # no truncation
"""

import glob
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from textwrap import shorten

DB_PATH = Path(__file__).parent / "events.db"
PROJECTS_DIR = Path.home() / ".claude" / "projects"

GRAY = "\033[90m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
MAGENTA = "\033[35m"
RED = "\033[31m"
BOLD = "\033[1m"
RESET = "\033[0m"


def find_transcript(session_prefix: str | None):
    if DB_PATH.exists():
        conn = sqlite3.connect(DB_PATH)
        if session_prefix:
            row = conn.execute(
                "SELECT session_id, transcript_path FROM events "
                "WHERE session_id LIKE ? AND transcript_path IS NOT NULL "
                "ORDER BY id DESC LIMIT 1",
                (f"{session_prefix}%",),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT session_id, transcript_path FROM events "
                "WHERE transcript_path IS NOT NULL "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
        conn.close()
        if row and row[1] and Path(row[1]).exists():
            return row[1], row[0]

    if session_prefix:
        matches = glob.glob(str(PROJECTS_DIR / "*" / f"{session_prefix}*.jsonl"))
        if matches:
            sid = Path(matches[0]).stem
            return matches[0], sid
    return None, None


def list_sessions(n: int = 20) -> None:
    if not DB_PATH.exists():
        print("No events.db yet — start a Claude Code session first.")
        return
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        """
        SELECT session_id, MIN(received_at), MAX(received_at), COUNT(*), MAX(cwd)
        FROM events
        WHERE session_id IS NOT NULL AND session_id != 'smoke-1234'
        GROUP BY session_id
        ORDER BY MAX(id) DESC
        LIMIT ?
        """,
        (n,),
    ).fetchall()
    conn.close()
    for sid, start, end, count, cwd in rows:
        print(f"{sid[:8]}  events={count:<5}  {start[:19]} → {end[:19]}  cwd={cwd or ''}")


def fmt_ts(ts: str | None) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).strftime("%H:%M:%S")
    except ValueError:
        return ts[:19]


def truncate(s: str, n: int) -> str:
    return shorten(s.replace("\n", " ").strip(), width=n, placeholder="…")


def render_block(block: dict, *, full: bool, show_thinking: bool):
    btype = block.get("type")
    if btype == "text":
        return ("text", block.get("text", ""))
    if btype == "thinking":
        if not show_thinking:
            return None
        return ("thinking", block.get("thinking", ""))
    if btype == "tool_use":
        name = block.get("name", "?")
        inp = block.get("input", {})
        if full:
            return ("tool_use", f"{name}({json.dumps(inp, ensure_ascii=False)})")
        if isinstance(inp, dict):
            preview = ", ".join(
                f"{k}={truncate(json.dumps(v, ensure_ascii=False), 60)}"
                for k, v in list(inp.items())[:3]
            )
            return ("tool_use", f"{name}({preview})")
        return ("tool_use", f"{name}(…)")
    if btype == "tool_result":
        content = block.get("content")
        if isinstance(content, list):
            content = " ".join(
                c.get("text", "") if isinstance(c, dict) else str(c) for c in content
            )
        elif not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        is_error = block.get("is_error", False)
        body = content if full else truncate(content, 250)
        return ("tool_error" if is_error else "tool_result", body)
    return None


def render(path: str, *, show_thinking: bool, full: bool) -> None:
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = entry.get("type")
            if etype not in ("user", "assistant"):
                continue

            ts = fmt_ts(entry.get("timestamp"))
            msg = entry.get("message", {}) or {}
            content = msg.get("content")

            if isinstance(content, str):
                body = content if full else truncate(content, 1500)
                print(f"{GRAY}[{ts}]{RESET} {CYAN}{BOLD}user:{RESET} {body}\n")
                continue

            if not isinstance(content, list):
                continue

            blocks = [render_block(b, full=full, show_thinking=show_thinking) for b in content]
            blocks = [b for b in blocks if b]
            if not blocks:
                continue

            had_text = False
            for kind, body in blocks:
                if kind == "text" and etype == "user":
                    body = body if full else truncate(body, 1500)
                    print(f"{GRAY}[{ts}]{RESET} {CYAN}{BOLD}user:{RESET} {body}")
                    had_text = True
                elif kind == "text" and etype == "assistant":
                    body = body if full else truncate(body, 1500)
                    print(f"{GRAY}[{ts}]{RESET} {GREEN}{BOLD}assistant:{RESET} {body}")
                    had_text = True
                elif kind == "thinking":
                    body = body if full else truncate(body, 500)
                    print(f"{GRAY}[{ts}]{RESET} {MAGENTA}thinking:{RESET} {body}")
                elif kind == "tool_use":
                    print(f"{GRAY}[{ts}]{RESET} {YELLOW}→ tool:{RESET} {body}")
                elif kind == "tool_result":
                    print(f"{GRAY}[{ts}]{RESET} {YELLOW}← result:{RESET} {body}")
                elif kind == "tool_error":
                    print(f"{GRAY}[{ts}]{RESET} {RED}← error:{RESET} {body}")
            if had_text:
                print()


def main() -> None:
    args = sys.argv[1:]
    show_thinking = any(a in ("--thinking", "-t") for a in args)
    full = any(a in ("--full", "-f") for a in args)
    list_only = any(a in ("--list", "-l") for a in args)
    positional = [a for a in args if not a.startswith("-")]
    session_prefix = positional[0] if positional else None

    if list_only:
        list_sessions()
        return

    path, sid = find_transcript(session_prefix)
    if not path:
        print(f"No transcript found for session: {session_prefix or '(latest)'}")
        sys.exit(1)

    print(f"{GRAY}# session={sid}  path={path}{RESET}\n")
    render(path, show_thinking=show_thinking, full=full)


if __name__ == "__main__":
    main()
