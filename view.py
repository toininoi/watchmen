import json
import re
import sqlite3
import sys
from pathlib import Path

from paths import EVENTS_DB

DB_PATH = EVENTS_DB

GRAY = "\033[90m"
YELLOW = "\033[33m"
BOLD = "\033[1m"
RESET = "\033[0m"


def main() -> None:
    args = sys.argv[1:]
    n = 30
    event_filter = None
    session_filter = None
    cwd_filter = None
    grep_pattern = None
    show_payload = False

    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-n", "--limit") and i + 1 < len(args):
            n = int(args[i + 1])
            i += 2
        elif a in ("-e", "--event") and i + 1 < len(args):
            event_filter = args[i + 1]
            i += 2
        elif a in ("-s", "--session") and i + 1 < len(args):
            session_filter = args[i + 1]
            i += 2
        elif a in ("-c", "--cwd") and i + 1 < len(args):
            cwd_filter = args[i + 1]
            i += 2
        elif a in ("-g", "--grep") and i + 1 < len(args):
            grep_pattern = args[i + 1]
            i += 2
        elif a in ("-v", "--verbose"):
            show_payload = True
            i += 1
        elif a in ("-h", "--help"):
            print(
                "usage: view.py [-n N] [-e EVENT] [-s SESSION_PREFIX] [-c CWD_SUBSTR] [-g REGEX] [-v]\n"
                "  -g/--grep   regex over the event payload (case-insensitive); shows snippet around match\n"
                "  -c/--cwd    filter by cwd substring (e.g. tally-weijl-images)"
            )
            return
        else:
            i += 1

    where = []
    params: list = []
    if event_filter:
        where.append("event_type = ?")
        params.append(event_filter)
    if session_filter:
        where.append("session_id LIKE ?")
        params.append(f"{session_filter}%")
    if cwd_filter:
        where.append("cwd LIKE ?")
        params.append(f"%{cwd_filter}%")
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    if grep_pattern:
        pattern = re.compile(grep_pattern, re.IGNORECASE)
        sql = f"SELECT * FROM events{where_sql} ORDER BY id DESC LIMIT 50000"
        candidate_rows = conn.execute(sql, params)
        matched: list = []
        for r in candidate_rows:
            payload = r["payload_json"] or ""
            ms = list(pattern.finditer(payload))
            if ms:
                matched.append((r, ms))
            if len(matched) >= n:
                break
        matched.reverse()

        for r, ms in matched:
            sid = (r["session_id"] or "")[:8]
            tool = r["tool_name"] or "-"
            cwd = r["cwd"] or ""
            print(
                f"{r['received_at']}  {r['event_type'] or '?':<20} sess={sid}  tool={tool:<18} cwd={cwd}"
            )
            for m in ms[:5]:
                start = max(0, m.start() - 80)
                end = min(len(r["payload_json"]), m.end() + 80)
                pre = r["payload_json"][start : m.start()].replace("\n", " ")
                hit = r["payload_json"][m.start() : m.end()].replace("\n", " ")
                post = r["payload_json"][m.end() : end].replace("\n", " ")
                print(f"  {GRAY}…{pre}{RESET}{BOLD}{YELLOW}{hit}{RESET}{GRAY}{post}…{RESET}")
            if show_payload:
                try:
                    parsed = json.loads(r["payload_json"])
                    print(json.dumps(parsed, indent=2, ensure_ascii=False))
                except json.JSONDecodeError:
                    print(r["payload_json"])
                print("---")
        conn.close()
        return

    sql = f"SELECT * FROM events{where_sql} ORDER BY id DESC LIMIT ?"
    rows = list(conn.execute(sql, [*params, n]))
    rows.reverse()

    for r in rows:
        sid = (r["session_id"] or "")[:8]
        tool = r["tool_name"] or "-"
        cwd = r["cwd"] or ""
        print(f"{r['received_at']}  {r['event_type'] or '?':<20} sess={sid}  tool={tool:<18} cwd={cwd}")
        if show_payload:
            try:
                payload = json.loads(r["payload_json"])
                print(json.dumps(payload, indent=2, ensure_ascii=False))
            except json.JSONDecodeError:
                print(r["payload_json"])
            print("---")

    conn.close()


if __name__ == "__main__":
    main()
