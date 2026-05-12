"""Shared SQL filters for corpus queries.

A 'substantive' session is one where real work happened — at least one tool
use OR more than a couple of messages with multiple prompts. Filters out:
  - sub-4-message aborted sessions (typed a question, hit Ctrl+C)
  - single-prompt no-response chats
  - sessions where the assistant never engaged

The filter is conservative on purpose: if even one tool fired, the session
counts as substantive (a one-line bash question is still real work).

Apply via AND in the WHERE clause of any sessions/prompts query. The default
'<alias>.' assumes the sessions table is aliased as 's', matching the existing
JOIN convention in analyze.py and state.py.

Calibration (kai-hooks-mvp live install, 2026-05-12): filters 9 of 59 main
sessions (~15%), all 0-tool, ≤3-message, ≤5-second aborts. Zero substantive
sessions filtered.
"""

from __future__ import annotations


def substantive_filter(alias: str = "s") -> str:
    """Return a SQL boolean expression that's True for substantive sessions.
    Embed in a WHERE/AND clause:

        WHERE s.is_subagent = 0 AND {substantive_filter()}

    Caller is responsible for the alias — pass alias='' if the columns are
    unqualified in the surrounding query."""
    prefix = f"{alias}." if alias else ""
    return (
        f"({prefix}tool_use_count >= 1 "
        f"OR ({prefix}message_count >= 4 AND {prefix}user_prompt_count >= 2))"
    )
