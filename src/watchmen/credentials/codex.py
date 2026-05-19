"""Read OpenAI Codex CLI's stored credential.

Codex stores its credential at `~/.codex/auth.json`. The file has two
shapes depending on how the user signed in:

  # api-key mode — set via `codex login --api-key sk-...`
  {
    "OPENAI_API_KEY": "sk-...",
    "auth_mode":      "api-key",
    "last_refresh":   "..."
  }

  # chatgpt mode — set via OAuth login with a ChatGPT account
  {
    "OPENAI_API_KEY": null,
    "auth_mode":      "chatgpt",
    "tokens": {
      "access_token":  "...",
      "refresh_token": "...",
      "id_token":      "...",
      "account_id":    "uuid"
    },
    "last_refresh":   "..."
  }

The api-key path is trivial reuse for the existing `openai` provider —
if the user already has an OpenAI key stored via Codex, watchmen can use
it without asking. The chatgpt path is experimental: the OAuth token
authenticates against `chatgpt.com/backend-api/codex/responses` with a
restricted model whitelist that's not publicly documented (the spike
during PR1's design phase saw gpt-5, gpt-5-codex, codex-mini, and
o4-mini all rejected with "model not supported when using Codex with a
ChatGPT account"). We surface the credential here and let provider code
decide whether to actually use it.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path


_AUTH_PATH = Path.home() / ".codex" / "auth.json"


@dataclass(frozen=True)
class CodexCredentials:
    """Parsed view of `~/.codex/auth.json` with both modes folded into one
    shape. `mode` discriminates which fields are meaningful.

    For mode='api-key':       `api_key` is set; all OAuth fields are None.
    For mode='chatgpt':       `access_token` + `account_id` are set;
                              `api_key` is None.
    """

    mode: str  # "api-key" | "chatgpt"
    api_key: str | None
    access_token: str | None
    refresh_token: str | None
    account_id: str | None
    last_refresh_iso: str | None

    @classmethod
    def read(cls) -> "CodexCredentials | None":
        """Load + parse the credential file. Returns None if:
        - file doesn't exist (Codex not installed / not logged in)
        - file is malformed JSON
        - neither an api-key nor a chatgpt-access-token is present

        Never raises. Callers checking 'do I have an OpenAI key via
        Codex?' just look at the return + `.api_key`."""
        # Resolve home lazily so monkeypatched Path.home in tests works.
        path = Path.home() / ".codex" / "auth.json"
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None

        mode = (payload.get("auth_mode") or "").lower()
        api_key = payload.get("OPENAI_API_KEY")
        tokens = payload.get("tokens") or {}
        access = tokens.get("access_token")
        refresh = tokens.get("refresh_token")
        account_id = tokens.get("account_id")
        last_refresh = payload.get("last_refresh")

        # Allow the mode field to disagree with what's actually present —
        # users who switched login flows mid-stream end up with stale
        # mode strings. Use the actual data presence as the source of
        # truth, mode field as the hint.
        if api_key and not mode:
            mode = "api-key"
        if access and not mode:
            mode = "chatgpt"

        if mode == "api-key" and api_key:
            return cls(
                mode="api-key",
                api_key=api_key,
                access_token=None,
                refresh_token=None,
                account_id=None,
                last_refresh_iso=last_refresh,
            )
        if mode == "chatgpt" and access:
            return cls(
                mode="chatgpt",
                api_key=None,
                access_token=access,
                refresh_token=refresh,
                account_id=account_id,
                last_refresh_iso=last_refresh,
            )
        return None

    def likely_expired(self, *, max_age_hours: float = 1.0) -> bool:
        """ChatGPT-mode tokens rotate every ~1 hour. The id_token's JWT
        `exp` field is authoritative, but parsing it pulls in base64
        machinery we'd rather avoid for what's a hint, not a hard check.
        Instead we treat the `last_refresh` timestamp as a proxy: tokens
        older than `max_age_hours` are flagged for refresh / re-login.
        Callers should treat this as advisory; the real verdict comes
        from the API returning 401."""
        if self.mode != "chatgpt" or not self.last_refresh_iso:
            return False
        try:
            from datetime import datetime
            t = datetime.fromisoformat(self.last_refresh_iso.replace("Z", "+00:00"))
            age_seconds = time.time() - t.timestamp()
            return age_seconds > (max_age_hours * 3600)
        except (ValueError, AttributeError):
            return False


def is_codex_available() -> bool:
    """True iff Codex is installed and the user has logged in (auth.json
    exists). Used by onboard / settings UI to decide whether to surface
    the 'reuse Codex credentials' option."""
    return _AUTH_PATH.exists()
