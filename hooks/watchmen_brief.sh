#!/usr/bin/env bash
# SessionStart brief — fires a macOS notification with what changed in
# kai_claude/<project>/_changelog.md since the user last started a session here.
#
# Critical: never blocks the session. Reads stdin, forks brief.py into the
# background with nohup, returns 0 immediately. All output is osascript
# notifications — nothing is written to stdout (no agent injection).
input=$(cat)
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
nohup python3 "$ROOT/brief.py" <<<"$input" >/dev/null 2>>/tmp/watchmen_brief.err &
disown
exit 0
