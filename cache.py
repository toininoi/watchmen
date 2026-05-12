"""Content-addressable cache for curator stages.

Each stage's agent reads from a set of input-side tools (read_thesis_section,
read_repo_file, read_session_full, list_repo_files, query_corpus,
read_kai_claude_file, list_kai_claude_files). We instrument those tools to
record every (tool_name, args, sha256(result)) tuple during a run, and persist
that log alongside the stage's output. On the next run, replay those tool
calls — if every result hashes to the cached value, skip the agent entirely
and keep the existing output.

False cache hits (skip when we shouldn't) require an instrumented tool to
return DIFFERENT results for the SAME inputs between runs — impossible unless
a tool's pure-function contract broke. False cache misses (re-run when we
could have skipped) are fine; we lose the speedup but produce a correct bundle.

Cache files (each holds a JSON array of {tool, args, result_hash}):
    kai_claude/<project>/.candidates.inputs.json    — stage 1
    kai_claude/<project>/skills/<slug>/.inputs.json — stage 2 (per skill)
    kai_claude/<project>/.claude_md.inputs.json     — stage 3
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Callable

# Tools whose results affect the agent's output. Effect-side tools
# (write_kai_claude_file, append_curation_log, run_critic, finish_*) are NOT
# instrumented — they're outputs, not dependencies.
INPUT_TOOLS = frozenset({
    "query_corpus",
    "read_session_full",
    "read_thesis_section",
    "list_repo_files",
    "read_repo_file",
    "read_kai_claude_file",
    "list_kai_claude_files",
})


def _hash_result(result) -> str:
    """Stable hash of a tool's return. Tools return strings or JSON-serializable
    values; coerce to deterministic bytes before hashing."""
    if isinstance(result, str):
        payload = result.encode("utf-8")
    else:
        payload = json.dumps(result, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class ReadRecorder:
    """Append-only log of (tool, args, result_hash) captured during a stage's
    agent run. Pass to wrap_handlers; after the agent completes successfully,
    call .export() to persist the log via write_cache()."""

    def __init__(self):
        self._log: list[dict] = []

    def record(self, tool_name: str, args: dict, result) -> None:
        self._log.append({
            "tool": tool_name,
            "args": args,
            "result_hash": _hash_result(result),
        })

    def export(self) -> list[dict]:
        return list(self._log)

    def __len__(self) -> int:
        return len(self._log)


def wrap_handlers(
    handlers: dict[str, Callable],
    recorder: ReadRecorder,
    input_tools: frozenset = INPUT_TOOLS,
) -> dict[str, Callable]:
    """Return a new handler dict where input-side tools are wrapped to record
    every call into the recorder. Non-input tools pass through unchanged."""
    wrapped: dict[str, Callable] = {}
    for name, fn in handlers.items():
        if name in input_tools:
            wrapped[name] = _make_recording_wrapper(name, fn, recorder)
        else:
            wrapped[name] = fn
    return wrapped


def _make_recording_wrapper(name: str, fn: Callable, recorder: ReadRecorder) -> Callable:
    def wrapped(**kwargs):
        result = fn(**kwargs)
        recorder.record(name, kwargs, result)
        return result
    return wrapped


def cache_hit(cache_file: Path, handlers: dict[str, Callable]) -> bool:
    """Replay every (tool, args) from cache_file against the given handlers.
    Return True iff every result hashes to the cached value."""
    if not cache_file.exists():
        return False
    try:
        log = json.loads(cache_file.read_text())
    except (json.JSONDecodeError, OSError):
        return False
    if not isinstance(log, list) or not log:
        return False
    for entry in log:
        try:
            tool = entry["tool"]
            args = entry["args"]
            expected = entry["result_hash"]
        except (KeyError, TypeError):
            return False
        fn = handlers.get(tool)
        if fn is None:
            return False
        try:
            result = fn(**args)
        except Exception:
            return False
        if _hash_result(result) != expected:
            return False
    return True


def write_cache(cache_file: Path, recorder: ReadRecorder) -> None:
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(recorder.export(), indent=2))


def invalidate_all(project_root: Path) -> int:
    """Delete every cache file under kai_claude/<project>/. Returns the count
    removed. Backing `curate --regen-all`."""
    if not project_root.exists():
        return 0
    targets: list[Path] = [
        project_root / ".candidates.inputs.json",
        project_root / ".claude_md.inputs.json",
    ]
    skills_dir = project_root / "skills"
    if skills_dir.exists():
        for d in skills_dir.iterdir():
            if d.is_dir():
                targets.append(d / ".inputs.json")
    removed = 0
    for t in targets:
        if t.exists():
            t.unlink()
            removed += 1
    return removed
