"""Tests for watchmen.adapters.opencode — the OpenCode session parser."""

import json
from pathlib import Path
from watchmen.adapters import opencode

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "opencode_session.json"

def test_scan_opencode_fixture():
    """Verify that the opencode adapter correctly parses the fixture file."""
    # The fixture was created in the setup phase
    entry = {
        "path": FIXTURE_PATH,
        "project_dir": None,
        "is_subagent": False,
        "parent_session_id": None,
    }
    
    session, prompts, tool_calls = opencode.scan(entry)
    
    assert session["session_id"] == "ses_01J9X7Y2Z3A4B5C6D7E8F9G0H1"
    assert session["project_dir"] == "/Users/ahegde/projects/watchmen"
    assert session["agent"] == "opencode"
    assert session["message_count"] == 2
    assert session["user_prompt_count"] == 1
    assert session["assistant_text_count"] == 1
    assert session["assistant_thinking_count"] == 1
    assert session["tool_use_count"] == 1
    assert session["input_tokens"] == 450
    assert session["output_tokens"] == 820
    
    assert len(prompts) == 1
    assert prompts[0]["text"] == "Why is the login failing in the production logs?"
    assert prompts[0]["is_first_in_session"] == 1
    
    assert len(tool_calls) == 1
    assert tool_calls[0]["tool_name"] == "bash"
    assert tool_calls[0]["is_error"] == 0

def test_scan_opencode_tool_error():
    """Verify that tool errors are correctly counted."""
    data = {
        "id": "err_session",
        "messages": [
            {
                "info": {"role": "assistant"},
                "parts": [
                    {"type": "tool", "tool": "ls", "status": "error", "output": "permission denied"}
                ]
            }
        ]
    }
    
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "err.json"
        p.write_text(json.dumps(data))
        
        session, _, tool_calls = opencode.scan({"path": p})
        assert session["tool_use_count"] == 1
        assert session["tool_error_count"] == 1
        assert tool_calls[0]["is_error"] == 1

def test_scan_empty_or_invalid():
    """Verify that invalid files return empty sessions instead of crashing."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p1 = Path(td) / "empty.json"
        p1.write_text("")
        
        p2 = Path(td) / "not_a_session.json"
        p2.write_text(json.dumps({"foo": "bar"}))
        
        s1, _, _ = opencode.scan({"path": p1})
        assert s1["agent"] == "opencode"
        assert s1["message_count"] == 0
        
        s2, _, _ = opencode.scan({"path": p2})
        assert s2["agent"] == "opencode"
        assert s2["message_count"] == 0
