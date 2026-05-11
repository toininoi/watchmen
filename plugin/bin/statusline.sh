#!/usr/bin/env bash
# StatusLine command — emits one short line if there are pending watchmen
# updates for the current workspace, silent otherwise.
#
# Wire in ~/.claude/settings.json:
#   "statusLine": {
#     "type": "command",
#     "command": "/path/to/watchmen-plugin/bin/statusline.sh"
#   }
#
# (CLAUDE_PLUGIN_ROOT isn't set for the global statusLine, so this script
# resolves its own location via $0 rather than env vars.)

set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

PROJECT_KEY=$(python3 "${ROOT}/bin/resolve_project_key.py" "$PWD" 2>/dev/null)
[ -z "${PROJECT_KEY}" ] && exit 0

STATE_FILE="${HOME}/.watchmen/state/${PROJECT_KEY}.json"
ACK_FILE="${HOME}/.watchmen/state/${PROJECT_KEY}.acknowledged"
[ ! -f "${STATE_FILE}" ] && exit 0

# Suppress if ack is newer than state.
if [ -f "${ACK_FILE}" ]; then
  state_mtime=$(stat -f %m "${STATE_FILE}" 2>/dev/null || echo 0)
  ack_mtime=$(stat -f %m "${ACK_FILE}" 2>/dev/null || echo 0)
  if [ "${ack_mtime}" -gt "${state_mtime}" ]; then
    exit 0
  fi
fi

SUMMARY=$(python3 -c "
import json, sys
try:
    d = json.load(open('${STATE_FILE}'))
    print(d.get('summary', '').strip())
except Exception:
    pass
" 2>/dev/null)

[ -z "${SUMMARY}" ] && SUMMARY="updates available"

# ANSI yellow bulb + project + summary + invocation hint.
printf '\033[33m💡 watchmen · %s · %s · /watchmen:brief\033[0m\n' "${PROJECT_KEY}" "${SUMMARY}"
