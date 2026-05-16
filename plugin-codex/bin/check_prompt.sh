#!/usr/bin/env bash
# UserPromptSubmit hook — match prompt against indexed skill triggers, write
# a suggestion file the statusLine reads. Self-locates via $0 (CLAUDE_PLUGIN_ROOT
# isn't exported to the runtime env even when substituted in hook configs).
#
# Critical: writes NOTHING to stdout. UserPromptSubmit hook stdout would be
# injected as additional context to the agent, which would violate our
# "inform user, don't manipulate agent" rule.

set -uo pipefail

LOG_DIR="${HOME}/.watchmen/logs"
mkdir -p "${LOG_DIR}" 2>/dev/null || true
LOG_FILE="${LOG_DIR}/check_prompt.log"

input=$(cat)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# Failures go to ~/.watchmen/logs/, not /tmp — visible via `watchmen logs`
# eventually and not subject to the macOS /tmp 3-day reaper.
python3 "$SCRIPT_DIR/check_prompt.py" <<<"$input" >/dev/null 2>>"${LOG_FILE}" || true
exit 0
