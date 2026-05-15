"""Shared OpenRouter tool-calling agent. Used by analyze.py, curate.py, etc.

An Agent is configured with a system prompt + tool specs + tool handlers + a terminal-tool
name. .run(user_msg) runs the multi-turn loop and returns the args of the terminal tool call
(or empty dict if the model gave up without calling it), plus the full message history for logging.
"""

import json
import os
import time
from pathlib import Path
from typing import Callable

import httpx

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def load_api_key() -> str:
    if k := os.environ.get("OPENROUTER_API_KEY"):
        return k
    # Fallbacks in priority order:
    # 1. <project_root>/.env
    # 2. ~/.config/watchmen/.env
    candidates = [
        Path(__file__).parent / ".env",
        Path.home() / ".config" / "watchmen" / ".env",
    ]
    for env_path in candidates:
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("OPENROUTER_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError(
        "OPENROUTER_API_KEY not set. Either `export OPENROUTER_API_KEY=...` or put it in "
        ".env at the watchmen project root or ~/.config/watchmen/.env"
    )


def call_openrouter(client: httpx.Client, headers: dict, payload: dict, max_retries: int = 3):
    for attempt in range(max_retries):
        try:
            r = client.post(OPENROUTER_URL, headers=headers, json=payload, timeout=300.0)
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r.json()
        except httpx.RequestError:
            if attempt == max_retries - 1:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError("exhausted retries")


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
            data = call_openrouter(self.client, self.headers, {
                "model": self.model,
                "messages": messages,
                "tools": self.tool_specs,
            })
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
