#!/usr/bin/env bash
# StatusLine command — emit one short line for the current workspace.
#
# Priority:
#   1. Live skill suggestion from the UserPromptSubmit hook (most recent signal)
#   2. Pending brief from the last curator run
#   3. Silent
#
# Self-locates via $0 — CLAUDE_PLUGIN_ROOT isn't set for the global statusLine.

set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

PROJECT_KEY=$(python3 "${ROOT}/bin/resolve_project_key.py" "$PWD" 2>/dev/null)
[ -z "${PROJECT_KEY}" ] && exit 0

STATE_DIR="${HOME}/.watchmen/state"
STATE_FILE="${STATE_DIR}/${PROJECT_KEY}.json"
ACK_FILE="${STATE_DIR}/${PROJECT_KEY}.acknowledged"
SUGGESTION_FILE="${STATE_DIR}/${PROJECT_KEY}.suggestion.json"

# 1. Live suggestion from the prompt hook (highest priority).
if [ -f "${SUGGESTION_FILE}" ]; then
  SKILL=$(python3 -c "
import json, sys
try:
    d = json.load(open('${SUGGESTION_FILE}'))
    print(d.get('skill_slug', ''))
except Exception:
    pass
" 2>/dev/null)
  if [ -n "${SKILL}" ]; then
    printf '\033[33m💡 you could have used /%s to save time & tokens on this task\033[0m\n' "${SKILL}"
    exit 0
  fi
fi

# 2. Pending brief from curator (unless already acknowledged).
[ ! -f "${STATE_FILE}" ] && exit 0
if [ -f "${ACK_FILE}" ]; then
  state_mtime=$(stat -f %m "${STATE_FILE}" 2>/dev/null || echo 0)
  ack_mtime=$(stat -f %m "${ACK_FILE}" 2>/dev/null || echo 0)
  [ "${ack_mtime}" -gt "${state_mtime}" ] && exit 0
fi

SUMMARY=$(python3 -c "
import json
try:
    d = json.load(open('${STATE_FILE}'))
    print(d.get('summary', '').strip())
except Exception:
    pass
" 2>/dev/null)
[ -z "${SUMMARY}" ] && SUMMARY="updates available"

printf '\033[33m💡 watchmen · %s · %s · /watchmen:brief\033[0m\n' "${PROJECT_KEY}" "${SUMMARY}"
