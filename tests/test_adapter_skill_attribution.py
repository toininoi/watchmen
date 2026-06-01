"""Cross-adapter skill attribution tests.

Claude Code is the only agent with a first-class `Skill` tool primitive; for
everyone else, "skill was used" manifests as the model reading the skill's
SKILL.md file via the normal read/bash tool. `extract_skill_from_path` does
that detection, and the codex / pi / opencode adapters call it at every
tool_call site so `tool_calls.skill_name` ends up populated regardless of
which agent invoked the skill.

Two layers of test:

1. Unit tests on the helper itself (positive matches across the canonical
   skill directories, false-positive guards, non-string input handling).
2. Smoke tests that drive each adapter's `scan()` with a tiny fixture
   containing a SKILL.md-referencing tool call, asserting that the row
   handed to the corpus writer has `skill_name == "<slug>"`.

If a future adapter is added that doesn't go through `extract_skill_from_args`
at its tool-call sites, prune's per-skill usage telemetry silently goes
blind for that agent. The smoke tests below are the canary for that
regression.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from watchmen.adapters import codex, opencode, pi
from watchmen.adapters._shared import extract_skill_from_args, extract_skill_from_path


# ─── helper unit tests ─────────────────────────────────────────────────────


def test_extract_skill_from_path_canonical_locations():
    """Match against every real-world skill directory layout."""
    cases = [
        ("/Users/me/.claude/skills/refactor/SKILL.md", "refactor"),
        ("/Users/me/.codex/skills/test-runner/SKILL.md", "test-runner"),
        ("/Users/me/.codex/skills/.system/skill-installer/SKILL.md", "skill-installer"),
        ("/home/u/.pi/skills/deploy/SKILL.md", "deploy"),
        ("/Users/me/.watchmen/bundles/proj/skills/kai/SKILL.md", "kai"),
        ("/repo/.claude/skills/a_b.c-d/SKILL.md", "a_b.c-d"),
    ]
    for path, expected in cases:
        assert extract_skill_from_path(path) == expected, f"failed on {path}"


def test_extract_skill_from_path_negative_cases():
    """Don't match arbitrary file reads or partial paths."""
    assert extract_skill_from_path("/Users/me/notes/SKILL.md") is None
    assert extract_skill_from_path("/var/log/skills.log") is None
    assert extract_skill_from_path("/skills/SKILL.md") is None  # missing slug
    assert extract_skill_from_path("") is None
    assert extract_skill_from_path(None) is None
    assert extract_skill_from_path(42) is None
    assert extract_skill_from_path({"path": "/x/skills/foo/SKILL.md"}) is None  # dict, not str


def test_extract_skill_from_args_walks_dicts_and_strings():
    """Helper accepts whatever shape adapters have in hand."""
    assert extract_skill_from_args("/x/skills/foo/SKILL.md") == "foo"
    assert extract_skill_from_args({"path": "/x/skills/foo/SKILL.md"}) == "foo"
    assert extract_skill_from_args({"cmd": "cat /x/skills/bar/SKILL.md"}) == "bar"
    assert extract_skill_from_args(["irrelevant", "/x/skills/baz/SKILL.md"]) == "baz"
    assert extract_skill_from_args(None) is None
    assert extract_skill_from_args({"a": 1, "b": [2, 3]}) is None


# ─── codex adapter smoke test ─────────────────────────────────────────────


def test_codex_adapter_extracts_skill_name_from_function_call_path():
    """A codex function_call whose arguments reference a SKILL.md should set skill_name."""
    lines = [
        {
            "timestamp": "2026-05-19T12:00:00.000Z",
            "type": "session_meta",
            "payload": {"id": "sess-codex-1", "cwd": "/proj"},
        },
        {
            "timestamp": "2026-05-19T12:00:01.000Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5"},
        },
        {
            "timestamp": "2026-05-19T12:00:02.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "shell",
                "arguments": json.dumps({"cmd": "cat /Users/me/.codex/skills/refactor/SKILL.md"}),
                "call_id": "c1",
            },
        },
    ]
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "rollout-test.jsonl"
        p.write_text("\n".join(json.dumps(line) for line in lines))
        session, _, tool_calls = codex.scan({
            "path": p,
            "project_dir": None,
            "is_subagent": False,
            "parent_session_id": None,
        })
    assert session["tool_use_count"] == 1
    assert len(tool_calls) == 1
    assert tool_calls[0]["tool_name"] == "shell"
    assert tool_calls[0]["skill_name"] == "refactor"


def test_codex_adapter_skill_name_is_none_for_unrelated_tool_calls():
    """Reads that aren't SKILL.md should leave skill_name empty."""
    lines = [
        {"timestamp": "2026-05-19T12:00:00Z", "type": "session_meta",
         "payload": {"id": "sess-codex-2", "cwd": "/proj"}},
        {"timestamp": "2026-05-19T12:00:01Z", "type": "turn_context",
         "payload": {"model": "gpt-5"}},
        {"timestamp": "2026-05-19T12:00:02Z", "type": "response_item",
         "payload": {"type": "function_call", "name": "shell",
                     "arguments": json.dumps({"cmd": "ls -la"}), "call_id": "c2"}},
    ]
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "rollout-noskill.jsonl"
        p.write_text("\n".join(json.dumps(line) for line in lines))
        _, _, tool_calls = codex.scan({
            "path": p, "project_dir": None,
            "is_subagent": False, "parent_session_id": None,
        })
    assert tool_calls[0]["skill_name"] is None


# ─── pi adapter smoke test ────────────────────────────────────────────────


def test_pi_adapter_extracts_skill_name_from_toolcall_arguments():
    """A pi toolCall whose arguments reference a SKILL.md should set skill_name."""
    lines = [
        {"type": "session", "version": 3, "id": "sess-pi-1",
         "timestamp": "2026-05-19T12:00:00.000Z", "cwd": "/proj"},
        {"type": "message", "id": "m1", "parentId": "sess-pi-1",
         "timestamp": "2026-05-19T12:00:01.000Z",
         "message": {"role": "assistant", "model": "claude-3-5-sonnet",
                     "content": [{"type": "toolCall", "name": "read",
                                  "arguments": {"path": "/Users/me/.pi/skills/deploy/SKILL.md"}}]}},
    ]
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "session.jsonl"
        p.write_text("\n".join(json.dumps(line) for line in lines))
        session, _, tool_calls = pi.scan({
            "path": p, "project_dir": None,
            "is_subagent": False, "parent_session_id": None,
        })
    assert session["tool_use_count"] == 1
    assert tool_calls[0]["tool_name"] == "read"
    assert tool_calls[0]["skill_name"] == "deploy"


def test_pi_adapter_extracts_skill_name_from_bash_command_string():
    """bashExecution carries the command as content; helper should still find SKILL.md."""
    lines = [
        {"type": "session", "version": 3, "id": "sess-pi-2",
         "timestamp": "2026-05-19T12:00:00.000Z", "cwd": "/proj"},
        {"type": "message", "id": "m2", "parentId": "sess-pi-2",
         "timestamp": "2026-05-19T12:00:01.000Z",
         "message": {"role": "bashExecution",
                     "content": "cat /Users/me/.claude/skills/refactor/SKILL.md"}},
    ]
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "session.jsonl"
        p.write_text("\n".join(json.dumps(line) for line in lines))
        _, _, tool_calls = pi.scan({
            "path": p, "project_dir": None,
            "is_subagent": False, "parent_session_id": None,
        })
    assert tool_calls[0]["tool_name"] == "bash"
    assert tool_calls[0]["skill_name"] == "refactor"


def test_codex_adapter_attributes_turn_cost_to_active_skill():
    """Reading a SKILL.md opens a span; token_count cost in that turn and
    later turns accrues to the skill row until the next genuine user prompt."""
    from watchmen.adapters.codex import _codex_turn_cost
    last = {"input_tokens": 1000, "output_tokens": 1000}
    one = _codex_turn_cost("gpt-5", last)
    assert one > 0
    tc = lambda: {"type": "event_msg", "timestamp": "2026-05-19T12:00:09Z",
                  "payload": {"type": "token_count", "info": {"last_token_usage": last}}}
    lines = [
        {"type": "session_meta", "timestamp": "2026-05-19T12:00:00Z", "payload": {"id": "s", "cwd": "/p"}},
        {"type": "turn_context", "timestamp": "2026-05-19T12:00:01Z", "payload": {"model": "gpt-5"}},
        {"type": "response_item", "timestamp": "2026-05-19T12:00:02Z",
         "payload": {"type": "function_call", "name": "shell",
                     "arguments": json.dumps({"cmd": "cat /Users/me/.codex/skills/refactor/SKILL.md"})}},
        tc(),  # in-span turn -> accrues to refactor
        {"type": "response_item", "timestamp": "2026-05-19T12:00:20Z",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "different task now"}]}},
        tc(),  # post-span turn -> not attributed
    ]
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "rollout-x.jsonl"
        p.write_text("\n".join(json.dumps(x) for x in lines))
        _, _, tool_calls = codex.scan({"path": p, "project_dir": None,
                                       "is_subagent": False, "parent_session_id": None})
    skill_rows = [t for t in tool_calls if t.get("skill_name") == "refactor"]
    assert len(skill_rows) == 1
    assert skill_rows[0]["cost_usd"] == one  # only the in-span token_count


def test_pi_adapter_attributes_turn_cost_to_active_skill():
    """A pi read of SKILL.md opens a span; later assistant-message cost accrues
    to the skill until the next genuine user prompt."""
    from watchmen.metrics import turn_cost_usd
    one = turn_cost_usd("claude-3-5-sonnet", 1000, 0, 0, 0, 1000)
    assert one > 0
    usage = {"input": 1000, "output": 1000}
    lines = [
        {"type": "session", "version": 3, "id": "s", "timestamp": "2026-05-19T12:00:00Z", "cwd": "/p"},
        {"type": "message", "id": "u1", "parentId": "s", "timestamp": "2026-05-19T12:00:01Z",
         "message": {"role": "user", "content": "go"}},
        # assistant reads the skill (opens span; this msg's own cost not charged to it)
        {"type": "message", "id": "a1", "parentId": "u1", "timestamp": "2026-05-19T12:00:02Z",
         "message": {"role": "assistant", "model": "claude-3-5-sonnet", "usage": usage,
                     "content": [{"type": "toolCall", "name": "read",
                                  "arguments": {"path": "/Users/me/.pi/skills/deploy/SKILL.md"}}]}},
        # in-span working turn -> accrues to deploy
        {"type": "message", "id": "a2", "parentId": "a1", "timestamp": "2026-05-19T12:00:03Z",
         "message": {"role": "assistant", "model": "claude-3-5-sonnet", "usage": usage,
                     "content": [{"type": "text", "text": "working"}]}},
        # genuine prompt ends span
        {"type": "message", "id": "u2", "parentId": "a2", "timestamp": "2026-05-19T12:00:04Z",
         "message": {"role": "user", "content": "now something else"}},
        # post-span turn -> not attributed
        {"type": "message", "id": "a3", "parentId": "u2", "timestamp": "2026-05-19T12:00:05Z",
         "message": {"role": "assistant", "model": "claude-3-5-sonnet", "usage": usage,
                     "content": [{"type": "text", "text": "done"}]}},
    ]
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "session.jsonl"
        p.write_text("\n".join(json.dumps(x) for x in lines))
        _, _, tool_calls = pi.scan({"path": p, "project_dir": None,
                                    "is_subagent": False, "parent_session_id": None})
    skill_rows = [t for t in tool_calls if t.get("skill_name") == "deploy"]
    assert len(skill_rows) == 1
    assert skill_rows[0]["cost_usd"] == one  # only the one in-span working turn


# ─── opencode adapter smoke test ──────────────────────────────────────────


def test_opencode_adapter_extracts_skill_name_from_tool_part_args():
    """An opencode tool part whose args reference a SKILL.md should set skill_name."""
    data = {
        "id": "ses_skill_test",
        "cwd": "/proj",
        "model": "anthropic/claude-3-5-sonnet",
        "messages": [
            {"info": {"role": "user", "timestamp": "2026-05-19T12:00:00.000Z"},
             "parts": [{"type": "text", "text": "use the refactor skill"}]},
            {"info": {"role": "assistant", "timestamp": "2026-05-19T12:00:05.000Z",
                      "model_id": "claude-3-5-sonnet", "tokens": {"input": 10, "output": 20}},
             "parts": [{"type": "tool", "tool": "read",
                        "args": {"path": "/Users/me/.opencode/skills/refactor/SKILL.md"},
                        "status": "completed", "output": "..."}]},
        ],
    }
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "session.json"
        p.write_text(json.dumps(data))
        _, _, tool_calls = opencode.scan({"path": p})
    assert len(tool_calls) == 1
    assert tool_calls[0]["tool_name"] == "read"
    assert tool_calls[0]["skill_name"] == "refactor"
