#!/usr/bin/env bash
# Pipes the hook stdin JSON to the local observer and exits 0.
# Never blocks the Claude Code session — short timeout, errors swallowed.
input=$(cat)
curl -sS -m 2 \
  -X POST \
  -H "Content-Type: application/json" \
  --data "$input" \
  http://127.0.0.1:8765/hook >/dev/null 2>&1 || true
exit 0
