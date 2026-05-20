"""OpenCode agent session adapter.

Reads OpenCode sessions exported as JSON. OpenCode (anomalyco/opencode)
stores sessions internally in a SQLite-backed store, but provides a clean
`opencode export <id>` CLI that produces these files.

Format:
    {
      "id": "ses_...",
      "cwd": "/path/to/project",
      "model": "anthropic/claude-3-5-sonnet-20241022",
      "messages": [
        {
          "info": {"role": "user", "timestamp": "ISO"},
          "parts": [{"type": "text", "text": "..."}]
        },
        {
          "info": {"role": "assistant", "model_id": "...", "tokens": {"input": 1, "output": 1}},
          "parts": [
            {"type": "reasoning", "text": "..."},
            {"type": "tool", "tool": "bash", "status": "completed", "output": "..."}
          ]
        }
      ]
    }
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from watchmen.adapters._shared import extract_skill_from_args
from watchmen.metrics import turn_cost_usd

NAME = "opencode"

# OpenCode doesn't have a single canonical on-disk session directory like
# Claude Code or Codex. Users are expected to use `opencode export` or
# point watchmen at their own export folder. We check the most likely
# defaults.
SESSIONS_DIRS = (
    Path.home() / ".opencode" / "sessions",
    Path.home() / ".local" / "share" / "opencode" / "sessions",
)


def _parse_iso(ts):
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OSError, ValueError):
            return None
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def discover() -> Iterable[dict]:
    for sessions_dir in SESSIONS_DIRS:
        if not sessions_dir.exists():
            continue
        for f in sessions_dir.glob("*.json"):
            yield {
                "path": f,
                "project_dir": None,  # extracted from JSON in scan()
                "is_subagent": False,
                "parent_session_id": None,
            }


def scan(entry: dict):
    path: Path = entry["path"]
    with open(path, encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError:
            # Fall back to empty result if file is corrupt
            return _empty_session(path)

    # Basic structural check
    if not isinstance(data, dict) or "messages" not in data:
        return _empty_session(path)

    session = {
        "session_id": data.get("id") or path.stem,
        "project_dir": data.get("cwd") or "(unknown)",
        "transcript_path": str(path),
        "started_at": None,
        "ended_at": None,
        "duration_seconds": None,
        "is_subagent": 0,
        "parent_session_id": None,
        "message_count": 0,
        "user_prompt_count": 0,
        "assistant_text_count": 0,
        "assistant_thinking_count": 0,
        "tool_use_count": 0,
        "tool_error_count": 0,
        "models": "[]",
        "input_tokens": 0,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
        "output_tokens": 0,
        "model_dominant": data.get("model"),
        "cost_usd": 0.0,
        "agent": NAME,
    }

    prompts = []
    tool_calls = []
    models = set()
    if session["model_dominant"]:
        models.add(session["model_dominant"])

    is_first_user = True
    messages = data.get("messages", [])
    min_ts: datetime | None = None
    max_ts: datetime | None = None

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        info = msg.get("info") or {}
        role = info.get("role")
        ts = info.get("timestamp")
        parts = msg.get("parts") or []

        if ts:
            parsed = _parse_iso(ts)
            if parsed:
                if not min_ts or parsed < min_ts:
                    min_ts = parsed
                    session["started_at"] = ts if isinstance(ts, str) else parsed.isoformat()
                if not max_ts or parsed > max_ts:
                    max_ts = parsed
                    session["ended_at"] = ts if isinstance(ts, str) else parsed.isoformat()

        session["message_count"] += 1

        if role == "user":
            text_parts = [p.get("text") for p in parts if p.get("type") == "text" and p.get("text")]
            text = "\n".join(t for t in text_parts if t)
            if text:
                # Ensure timestamp is a comparable ISO string for the DB
                p_ts = ts
                if ts and not isinstance(ts, str):
                    p_parsed = _parse_iso(ts)
                    if p_parsed:
                        p_ts = p_parsed.isoformat()

                prompts.append({
                    "session_id": session["session_id"],
                    "timestamp": p_ts,
                    "text": text,
                    "word_count": len(text.split()),
                    "char_count": len(text),
                    "is_first_in_session": int(is_first_user),
                })
                is_first_user = False
                session["user_prompt_count"] += 1

        elif role == "assistant":
            model = info.get("model_id") or session["model_dominant"]
            if model:
                models.add(model)

            tokens = info.get("tokens") or {}
            in_t = int(tokens.get("input") or 0)
            out_t = int(tokens.get("output") or 0)
            cw_t = int(tokens.get("cache_write") or 0)
            cr_t = int(tokens.get("cache_read") or 0)

            session["input_tokens"] += in_t
            session["output_tokens"] += out_t
            session["cache_creation_tokens"] += cw_t
            session["cache_read_tokens"] += cr_t

            if model:
                session["cost_usd"] += turn_cost_usd(model, in_t, cw_t, 0, cr_t, out_t)

            for p in parts:
                ptype = p.get("type")
                if ptype == "text":
                    session["assistant_text_count"] += 1
                elif ptype == "reasoning":
                    session["assistant_thinking_count"] += 1
                elif ptype == "tool":
                    session["tool_use_count"] += 1
                    
                    # Ensure timestamp is a comparable ISO string for the DB
                    tc_ts = ts
                    if ts and not isinstance(ts, str):
                        tc_parsed = _parse_iso(ts)
                        if tc_parsed:
                            tc_ts = tc_parsed.isoformat()

                    tool_calls.append({
                        "session_id": session["session_id"],
                        "timestamp": tc_ts,
                        "tool_name": p.get("tool") or "?",
                        "is_error": 1 if p.get("status") == "error" else 0,
                        "skill_name": extract_skill_from_args(p.get("args")),
                    })
                    if p.get("status") == "error":
                        session["tool_error_count"] += 1

    session["models"] = json.dumps(sorted(list(models)))
    a = _parse_iso(session["started_at"])
    b = _parse_iso(session["ended_at"])
    if a and b:
        session["duration_seconds"] = (b - a).total_seconds()

    return session, prompts, tool_calls


def _empty_session(path: Path):
    return {
        "session_id": path.stem,
        "project_dir": "(unknown)",
        "transcript_path": str(path),
        "started_at": None,
        "ended_at": None,
        "duration_seconds": None,
        "is_subagent": 0,
        "parent_session_id": None,
        "message_count": 0,
        "user_prompt_count": 0,
        "assistant_text_count": 0,
        "assistant_thinking_count": 0,
        "tool_use_count": 0,
        "tool_error_count": 0,
        "models": "[]",
        "input_tokens": 0,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
        "output_tokens": 0,
        "model_dominant": None,
        "cost_usd": 0.0,
        "agent": NAME,
    }, [], []
