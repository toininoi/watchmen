#!/usr/bin/env bash
# Pipes the hook stdin JSON to the local observer and exits 0.
# Never blocks the Claude Code session — short timeout, errors swallowed
# but logged to ~/.watchmen/logs/hooks.log so debugging isn't a /tmp scavenger hunt.

set -uo pipefail

LOG_DIR="${HOME}/.watchmen/logs"
mkdir -p "${LOG_DIR}" 2>/dev/null || true
LOG_FILE="${LOG_DIR}/hooks.log"

input=$(cat)
# -f makes curl exit nonzero on 4xx/5xx (default is to "succeed" with the
# error body). -m 2 caps total time. We tee the response into the log on
# failure but always exit 0 so a dead hook server can't break a session.
if ! curl -fsS -m 2 \
    -X POST \
    -H "Content-Type: application/json" \
    --data "$input" \
    http://127.0.0.1:8765/hook >/dev/null 2>>"${LOG_FILE}"; then
  printf '[%s] watchmen_observe: POST failed (server down?)\n' "$(date -u +%FT%TZ)" >>"${LOG_FILE}" || true
fi
exit 0
