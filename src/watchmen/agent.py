"""Shared OpenRouter tool-calling agent. Used by analyze.py, curate.py, etc.

An Agent is configured with a system prompt + tool specs + tool handlers + a terminal-tool
name. .run(user_msg) runs the multi-turn loop and returns the args of the terminal tool call
(or empty dict if the model gave up without calling it), plus the full message history for logging.
"""

import json
import os
import random
import sys
import time
from pathlib import Path
from typing import Callable

import httpx

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Retry on these HTTP status codes — 429 (rate limit) and 5xx (server-side
# blips, gateway timeouts, etc.). 408 (request timeout) is treated as
# transient too. Everything else (4xx) is a client-side problem and isn't
# worth retrying.
_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504, 524}


def load_api_key() -> str:
    if k := os.environ.get("OPENROUTER_API_KEY"):
        return k
    # Canonical location written by `watchmen settings api-key set` and read
    # by every agent in the pipeline. The wizard chmods this 0600.
    env_path = Path.home() / ".config" / "watchmen" / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("OPENROUTER_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError(
        "OPENROUTER_API_KEY not set. Either `export OPENROUTER_API_KEY=...` or "
        "run `watchmen settings api-key set <key>` (writes ~/.config/watchmen/.env)."
    )


def _backoff_seconds(attempt: int, retry_after: str | None) -> float:
    """Exponential backoff with jitter, honoring Retry-After if the server set one.

    Jitter spreads simultaneous-retry storms when multiple Stage 2 workers all
    hit the same rate-limit at once. Retry-After (seconds form) is treated as
    a floor — we wait at least that long.
    """
    server_hint = 0.0
    if retry_after:
        try:
            server_hint = float(retry_after)
        except ValueError:
            pass
    backoff = (2 ** attempt) + random.uniform(0.0, 1.0)
    return max(backoff, server_hint)


def call_openrouter(
    client: httpx.Client,
    headers: dict,
    payload: dict,
    *,
    max_retries: int = 4,
    log: Callable[[str], None] | None = None,
) -> dict:
    """Single chat-completion call with retry on transient failures.

    Retries on httpx.RequestError (connect failures, read timeouts) AND on
    HTTP 408 / 429 / 5xx status codes. Non-retryable errors (4xx other than
    408/429) raise immediately. Exhausting retries raises HTTPStatusError
    for status failures or the last RequestError for network failures.
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            r = client.post(OPENROUTER_URL, headers=headers, json=payload, timeout=300.0)
            if r.status_code in _RETRYABLE_STATUS and attempt < max_retries - 1:
                delay = _backoff_seconds(attempt, r.headers.get("Retry-After"))
                if log:
                    log(f"openrouter: HTTP {r.status_code}, retry in {delay:.1f}s (attempt {attempt + 1}/{max_retries})")
                time.sleep(delay)
                continue
            r.raise_for_status()
            return r.json()
        except httpx.RequestError as e:
            last_exc = e
            if attempt == max_retries - 1:
                raise
            delay = _backoff_seconds(attempt, None)
            if log:
                log(f"openrouter: {type(e).__name__} ({e}), retry in {delay:.1f}s (attempt {attempt + 1}/{max_retries})")
            time.sleep(delay)
    # All retries exhausted on a retryable status — surface the last response
    # by letting the loop raise via raise_for_status above; this is unreachable.
    raise RuntimeError(f"exhausted retries; last error: {last_exc}")


def _turn_cost(model: str, usage: dict) -> float:
    """Best-effort per-turn cost in USD from an OpenRouter usage block.

    Returns 0.0 if pricing data isn't available — the budget ceiling won't
    fire spuriously when we can't price a model accurately. Imported lazily
    so module import doesn't trigger a 30s OpenRouter /models fetch.
    """
    if not usage or not model:
        return 0.0
    try:
        from watchmen.model_prices import turn_cost_usd
    except Exception:
        return 0.0
    prompt = int(usage.get("prompt_tokens", 0) or 0)
    completion = int(usage.get("completion_tokens", 0) or 0)
    cache_read = int(((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0))
    # OpenRouter folds cached tokens into prompt_tokens; subtract so we don't double-count.
    fresh_input = max(prompt - cache_read, 0)
    try:
        return turn_cost_usd(model, fresh_input, 0, 0, cache_read, completion)
    except Exception:
        return 0.0


class CostCeilingReached(RuntimeError):
    """Raised when an Agent run crosses its `max_cost_usd` budget mid-loop."""


class Agent:
    def __init__(
        self,
        name: str,
        model: str,
        system_prompt: str,
        tool_specs: list[dict],
        tool_handlers: dict[str, Callable],
        terminal_tool: str,
        client: httpx.Client | None = None,
        api_key: str | None = None,
        log_path: Path | None = None,
        result_max_chars: int = 30000,
        max_cost_usd: float | None = None,
    ):
        self.name = name
        self.model = model
        self.system_prompt = system_prompt
        self.tool_specs = tool_specs
        self.tool_handlers = tool_handlers
        self.terminal_tool = terminal_tool
        self.client = client or httpx.Client(timeout=300.0)
        self.api_key = api_key or load_api_key()
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            # OpenRouter app attribution: app=watchmen, sub-agent=name.
            # https://openrouter.ai/docs/api-reference/overview#headers
            "HTTP-Referer": "https://github.com/firstbatchxyz/watchmen",
            "X-Title": f"watchmen:{name}",
        }
        self.log_path = log_path
        self.result_max_chars = result_max_chars
        # Per-run cumulative cost ceiling. None = unlimited (the historical
        # behavior). When set, the loop aborts with CostCeilingReached as
        # soon as the running total crosses the threshold — protects against
        # runaway agents in scheduled curator runs.
        self.max_cost_usd = max_cost_usd
        self.cumulative_cost_usd = 0.0

    def _log(self, msg: str) -> None:
        if self.log_path:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")

    def run(self, user_msg: str, max_iter: int = 24) -> tuple[dict, list[dict]]:
        messages: list[dict] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_msg},
        ]
        terminal_args: dict = {}
        for it in range(max_iter):
            data = call_openrouter(
                self.client,
                self.headers,
                {
                    "model": self.model,
                    "messages": messages,
                    "tools": self.tool_specs,
                },
                log=self._log,
            )

            # Track per-turn cost; honor the budget ceiling before doing
            # any tool dispatch so we don't burn another iteration.
            turn_cost = _turn_cost(self.model, data.get("usage") or {})
            self.cumulative_cost_usd += turn_cost
            if self.max_cost_usd is not None and self.cumulative_cost_usd >= self.max_cost_usd:
                self._log(
                    f"[{self.name}] cost ceiling reached: ${self.cumulative_cost_usd:.4f} "
                    f">= ${self.max_cost_usd:.4f}; aborting loop"
                )
                print(
                    f"  [agent={self.name}] cost ceiling reached "
                    f"(${self.cumulative_cost_usd:.4f} >= ${self.max_cost_usd:.4f}); aborting",
                    file=sys.stderr,
                )
                break

            msg = data["choices"][0]["message"]
            clean = {"role": "assistant", "content": msg.get("content") or ""}
            if msg.get("tool_calls"):
                clean["tool_calls"] = msg["tool_calls"]
            messages.append(clean)

            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                break

            ended = False
            for tc in tool_calls:
                fn = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                self._log(f"[{self.name}][iter {it}] {fn}({list(args.keys())})")

                if fn == self.terminal_tool:
                    terminal_args = args
                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": "ok"})
                    ended = True
                    continue

                handler = self.tool_handlers.get(fn)
                if handler is None:
                    result = f"unknown tool: {fn}"
                else:
                    try:
                        out = handler(**args)
                        result = out if isinstance(out, str) else json.dumps(out, default=str)
                    except Exception as e:
                        result = f"ERROR: {type(e).__name__}: {e}"
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result[:self.result_max_chars]})

            if ended:
                break

        return terminal_args, messages
