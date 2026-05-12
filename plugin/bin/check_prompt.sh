#!/usr/bin/env bash
# UserPromptSubmit hook — match prompt against indexed skill triggers, write
# a suggestion file the statusLine reads. Self-locates via $0 (CLAUDE_PLUGIN_ROOT
# isn't exported to the runtime env even when substituted in hook configs).
#
# Critical: writes NOTHING to stdout. UserPromptSubmit hook stdout would be
# injected as additional context to the agent, which would violate our
# "inform user, don't manipulate agent" rule.
input=$(cat)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
python3 "$SCRIPT_DIR/check_prompt.py" <<<"$input" >/dev/null 2>>/tmp/watchmen_check_prompt.err
exit 0
