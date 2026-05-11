#!/usr/bin/env bash
# Called by the /watchmen:brief skill at render time. Prints the latest watchmen
# state for the CWD's project, then touches an acknowledgment file so the
# statusLine indicator clears.
#
# Emits one of:
#   - "(not a tracked project)"  if CWD doesn't map to any tracked project
#   - "(no state)"               if no state file exists yet for the project
#   - the JSON contents of the state file
set -u

PROJECT_KEY=$(python3 "${CLAUDE_PLUGIN_ROOT}/bin/resolve_project_key.py" "$PWD" 2>/dev/null)
if [ -z "${PROJECT_KEY}" ]; then
  echo "(not a tracked project)"
  exit 0
fi

STATE_DIR="${HOME}/.watchmen/state"
STATE_FILE="${STATE_DIR}/${PROJECT_KEY}.json"
if [ ! -f "${STATE_FILE}" ]; then
  echo "(no state for ${PROJECT_KEY})"
  exit 0
fi

cat "${STATE_FILE}"

# Touch acknowledgment AFTER cat so statusLine clears on next refresh.
mkdir -p "${STATE_DIR}"
touch "${STATE_DIR}/${PROJECT_KEY}.acknowledged"
