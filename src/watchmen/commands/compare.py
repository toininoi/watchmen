"""`watchmen compare` command."""

from __future__ import annotations

import json
from dataclasses import asdict

from rich.console import Console

from watchmen.compare import (
    DEFAULT_CANDIDATES,
    DEFAULT_JUDGE_MODEL,
    DEFAULT_PROVIDER,
    DEFAULT_REFERENCE_MODEL,
    CompareConfig,
    render_compare_summary,
    run_compare,
)
from watchmen.ui import dim, yellow
from watchmen.util import available_skills


def _dedupe_models(models: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for model in models:
        if model in seen:
            continue
        seen.add(model)
        result.append(model)
    return result


def _parse_candidates(value: str | None, extra: list[str] | None = None) -> list[str]:
    extra = [part.strip() for part in (extra or []) if part.strip()]
    if value is None or value.strip().lower() == "auto":
        base = list(DEFAULT_CANDIDATES)
    elif value.strip().lower() in {"none", "off"}:
        base = []
    else:
        base = [part.strip() for part in value.split(",") if part.strip()]
    return _dedupe_models([*base, *extra])


def cmd_compare(args) -> int:
    console = Console()
    candidates = _parse_candidates(
        getattr(args, "candidates", None),
        getattr(args, "candidate_models", None),
    )
    if not candidates:
        print(yellow("no candidate models provided"))
        return 1

    config = CompareConfig(
        project_key=args.project,
        bucket=args.bucket,
        reference_model=getattr(args, "reference", None) or DEFAULT_REFERENCE_MODEL,
        judge_model=getattr(args, "judge", None) or DEFAULT_JUDGE_MODEL,
        candidates=candidates,
        task_count=max(1, int(getattr(args, "tasks", 3))),
        reference_n=max(1, int(getattr(args, "reference_n", 1))),
        candidate_n=max(1, int(getattr(args, "best_of", 3))),
        provider=getattr(args, "provider", None) or DEFAULT_PROVIDER,
        temperature=float(getattr(args, "temperature", 0.4)),
        max_tokens=int(getattr(args, "max_tokens", 2600)),
        generation_concurrency=max(1, int(getattr(args, "concurrency", 4))),
    )

    try:
        progress = None if getattr(args, "json", False) else lambda msg: console.print(f"[dim]{msg}[/]")
        result = run_compare(config, progress=progress)
    except FileNotFoundError as exc:
        print(yellow(str(exc)))
        skills = available_skills(args.project)
        if skills:
            print(dim(f"  available buckets: {', '.join(skills[:20])}"))
        else:
            print(dim(f"  run `watchmen curate {args.project}` first"))
        return 1
    except Exception as exc:
        print(yellow(f"compare failed: {type(exc).__name__}: {exc}"))
        return 1

    if getattr(args, "json", False):
        console.print_json(json.dumps(asdict(result)))
    else:
        render_compare_summary(result, console=console)
    return 0
