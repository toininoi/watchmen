"""Credential discovery for OAuth-style provider reuse.

watchmen historically required pasting an API key per provider. The
modules under this package let users reuse credentials they've already
set up via Claude Code or Codex CLIs:

- `claude_code` — reads the OAuth token Claude Code stores in the macOS
  keychain (subscription-quota auth against api.anthropic.com).
- `codex` — reads `~/.codex/auth.json`, which contains either a raw
  OPENAI_API_KEY (Codex api-key mode) or an OAuth access_token + refresh
  token (Codex chatgpt mode).

Everything here is platform-aware and degrades gracefully — none of the
discovery functions raise on a fresh machine without Claude Code or
Codex installed; they return None and the caller decides what to do.
This is critical because the provider abstraction calls these on every
key resolution attempt, and surprises in those code paths would surface
as cryptic errors during analyst / curator runs.
"""

from watchmen.credentials.claude_code import (
    ClaudeCodeCredentials,
    is_claude_code_available,
)
from watchmen.credentials.codex import (
    CodexCredentials,
    is_codex_available,
)

__all__ = [
    "ClaudeCodeCredentials",
    "CodexCredentials",
    "is_claude_code_available",
    "is_codex_available",
]
