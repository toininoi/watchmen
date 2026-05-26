"""`watchmen route` command.

Default mode is the iterative skill-improvement loop: watchmen rewrites
the skill across multiple compare passes until a cheaper model can carry
it within the target threshold of the reference.  ``--no-improve`` falls
back to the one-shot variant (current model vs same-family candidates,
pick best, write router files).
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from rich import box
from rich.console import Console
from rich.table import Table

from watchmen.route import (
    RouteConfig,
    RouteResult,
    run_route,
)
from watchmen.route_improve import (
    IterativeRouteResult,
    run_route_iterative,
)
from watchmen.route_rewrite import apply_route_rewrites
from watchmen.ui import dim, yellow
from watchmen.util import available_skills


def _parse_repeatable(values: list[str] | None) -> list[str]:
    return [v.strip() for v in (values or []) if v.strip()]


def _parse_harnesses(value: str | None, extra: list[str] | None = None) -> list[str]:
    extra_list = _parse_repeatable(extra)
    if not value or value.strip().lower() == "auto":
        base: list[str] = []
    elif value.strip().lower() in {"none", "off"}:
        base = []
    else:
        base = [p.strip() for p in value.split(",") if p.strip()]
    seen: set[str] = set()
    result: list[str] = []
    for item in [*base, *extra_list]:
        norm = item.replace("-", "_")
        if norm in seen:
            continue
        seen.add(norm)
        result.append(norm)
    return result


def cmd_route(args) -> int:
    console = Console()

    user_candidates = _parse_repeatable(getattr(args, "candidate_models", None))
    harnesses = _parse_harnesses(
        getattr(args, "harnesses", None),
        getattr(args, "harness_extra", None),
    )

    config = RouteConfig(
        project_key=args.project,
        bucket=args.bucket,
        harnesses=harnesses,
        since_days=max(1, int(getattr(args, "since", 30))),
        cross_harness=bool(getattr(args, "cross_harness", False)),
        user_candidates=user_candidates,
        task_count=max(1, int(getattr(args, "tasks", 3))),
        candidate_n=max(1, int(getattr(args, "best_of", 3))),
        # When the user didn't pass --judge, leave this None so route's
        # per-harness builder defaults the judge to that harness's
        # reference (modal) model — same model the user runs daily, so
        # the judge is anchored in what they already trust and stays
        # inside their own quota.
        judge_model=getattr(args, "judge", None) or None,
        provider=getattr(args, "provider", None) or "openrouter",
        temperature=float(getattr(args, "temperature", 0.4)),
        max_tokens=int(getattr(args, "max_tokens", 2600)),
        generation_concurrency=max(1, int(getattr(args, "concurrency", 4))),
    )

    progress = (
        None if getattr(args, "json", False)
        else lambda msg: console.print(f"[dim]{msg}[/]")
    )

    try:
        if getattr(args, "no_improve", False):
            result = run_route(config, progress=progress)
        else:
            result = run_route_iterative(
                config,
                threshold_absolute=getattr(args, "threshold", None),
                threshold_offset=float(getattr(args, "threshold_offset", -0.05)),
                max_iters=max(1, int(getattr(args, "max_iters", 3))),
                max_cost_usd=getattr(args, "max_cost_usd", None),
                improver_model=getattr(args, "improver", None)
                    or "anthropic/claude-opus-4-7",
                commit_improvements=bool(getattr(args, "commit_improvements", False)),
                commit_skill=not bool(getattr(args, "no_rewrite", False)),
                progress=progress,
            )
    except FileNotFoundError as exc:
        print(yellow(str(exc)))
        skills = available_skills(args.project)
        if skills:
            print(dim(f"  available buckets: {', '.join(skills[:20])}"))
        else:
            print(dim(f"  run `watchmen curate {args.project}` first"))
        return 1
    except Exception as exc:  # noqa: BLE001
        print(yellow(f"route failed: {type(exc).__name__}: {exc}"))
        return 1

    # Both code paths surface to the rewrite step with the same shape:
    # a result whose `references` + `decisions` we can read.
    if not _has_references(result):
        print(
            yellow(
                f"no harnesses detected for {args.project} in the last "
                f"{config.since_days} days"
            )
        )
        print(dim(
            "  hint: ingest first (`watchmen ingest`) or widen the window with "
            "`--since 90`"
        ))
        return 1

    rewrite_outcomes: list[Any] = []
    if not getattr(args, "no_rewrite", False):
        # The rewriter takes a RouteResult shape.  For the iterative path
        # we adapt by handing it a final RouteResult synthesised from
        # `final_decisions`.
        rewrite_input = _rewrite_input_for(result)
        rewrite_outcomes = apply_route_rewrites(
            rewrite_input,
            dry_run=bool(getattr(args, "dry_run", False)),
        )

    if getattr(args, "json", False):
        console.print_json(json.dumps(_result_to_dict(result, rewrite_outcomes)))
    else:
        if isinstance(result, IterativeRouteResult):
            render_iterative_summary(result, rewrite_outcomes, console=console)
        else:
            render_route_summary(result, rewrite_outcomes, console=console)
    return 0


def _has_references(result: Any) -> bool:
    return bool(getattr(result, "references", None))


def _rewrite_input_for(result: Any) -> RouteResult:
    """The rewriter is shaped around RouteResult.  For the iterative
    runner, pack the final decisions into the same shape so we don't
    duplicate the file-emission logic per code path.
    """
    if isinstance(result, IterativeRouteResult):
        return RouteResult(
            run_id=result.run_id,
            run_dir=result.run_dir,
            config=result.config,
            references=result.references,
            compare_results={},  # rewriter doesn't need these
            decisions=result.final_decisions,
        )
    return result


# ─── Rendering ───────────────────────────────────────────────────────

def render_route_summary(
    result: RouteResult,
    rewrite_outcomes: list[Any],
    *,
    console: Console | None = None,
) -> None:
    console = console or Console()
    cfg = result.config

    console.print(
        f"[bold]watchmen route[/] {cfg.project_key}/{cfg.bucket} "
        f"[dim](one-shot mode)[/]\n"
        f"[dim]run={result.run_id} "
        f"harnesses={len(result.references)} "
        f"cross_harness={cfg.cross_harness} "
        f"since={cfg.since_days}d[/]"
    )
    _render_decision_table(result.decisions, result.references, console)

    if rewrite_outcomes:
        console.print()
        console.print("[bold]rewrites[/]")
        for outcome in rewrite_outcomes:
            line = (
                f"  [dim]{outcome.action:>10}[/]  "
                f"{outcome.harness:<12} "
                f"{outcome.artifact_kind:<11} "
                f"{outcome.path or '(no file)'}"
            )
            if outcome.reason:
                line += f"  [dim]— {outcome.reason}[/]"
            console.print(line)

    console.print(f"\n[dim]artifacts -> {result.run_dir}[/]")


def render_iterative_summary(
    result: IterativeRouteResult,
    rewrite_outcomes: list[Any],
    *,
    console: Console | None = None,
) -> None:
    console = console or Console()
    cfg = result.config

    headline = "[bold green]converged[/]" if result.bail_reason == "converged" else (
        "[bold yellow]bailed[/] " + result.bail_reason.replace("_", " ")
    )
    console.print(
        f"[bold]watchmen route[/] {cfg.project_key}/{cfg.bucket}  "
        f"({headline})\n"
        f"[dim]run={result.run_id} "
        f"iterations={len(result.iterations)} "
        f"total_cost=${result.total_cost_usd:.4f} "
        f"committed_skill={result.committed}[/]"
    )

    # Per-iteration table.  One row per (iter, harness) — the columns
    # show how the cheap models' scores evolved as watchmen revised the
    # skill.
    iter_table = Table(box=box.SIMPLE, title="per-iteration scores",
                        show_lines=False)
    iter_table.add_column("iter", justify="right")
    iter_table.add_column("harness", overflow="fold")
    iter_table.add_column("ref score", justify="right")
    iter_table.add_column("best candidate", overflow="fold", min_width=22)
    iter_table.add_column("cand score", justify="right")
    iter_table.add_column("cost", justify="right")
    iter_table.add_column("note", overflow="fold")

    for it in result.iterations:
        for d in it.route_result.decisions:
            summary = d.summary
            iter_table.add_row(
                f"{it.iter_idx}",
                d.harness,
                f"{d.avg_score:.3f}" if summary and summary.role == "reference" else
                _ref_score_for(d, it),
                summary.model if summary else "-",
                f"{summary.avg_score:.3f}" if summary else "-",
                f"${it.cost_usd:.3f}" if d == it.route_result.decisions[0] else "",
                d.note,
            )
    console.print(iter_table)

    # Final decisions row.
    final_table = Table(box=box.SIMPLE, title="final routes")
    final_table.add_column("harness")
    final_table.add_column("current model", overflow="fold", min_width=22)
    final_table.add_column("recommended", overflow="fold", min_width=22)
    final_table.add_column("avg score", justify="right")
    final_table.add_column("cost vs current", justify="right")
    final_table.add_column("decision")
    final_table.add_column("note", overflow="fold")
    for d in result.final_decisions:
        decision_style = {
            "downshift": "[bold green]downshift[/]",
            "upshift": "[bold cyan]upshift[/]",
            "switch-harness": "[bold magenta]switch-harness[/]",
            "stay": "[dim]stay[/]",
            "no-data": "[dim]no-data[/]",
        }.get(d.label, f"[yellow]{d.label}[/]")
        cost = (
            f"{d.cost_vs_current:.2f}x"
            if d.cost_vs_current is not None else "-"
        )
        final_table.add_row(
            d.harness, d.current_model, d.recommended_model or "-",
            f"{d.avg_score:.3f}", cost, decision_style, d.note,
        )
    console.print(final_table)

    if rewrite_outcomes:
        console.print()
        console.print("[bold]rewrites[/]")
        for outcome in rewrite_outcomes:
            line = (
                f"  [dim]{outcome.action:>10}[/]  "
                f"{outcome.harness:<12} "
                f"{outcome.artifact_kind:<11} "
                f"{outcome.path or '(no file)'}"
            )
            if outcome.reason:
                line += f"  [dim]— {outcome.reason}[/]"
            console.print(line)

    console.print(f"\n[dim]artifacts -> {result.run_dir}[/]")
    if result.committed:
        console.print(
            "[dim]bundle SKILL.md was updated with watchmen's improvements.[/]"
        )


def _ref_score_for(decision: Any, iteration: Any) -> str:
    cmp_r = iteration.route_result.compare_results.get(decision.harness)
    if cmp_r is None:
        return "-"
    for s in cmp_r.summaries:
        if s.role == "reference":
            return f"{s.avg_score:.3f}"
    return "-"


def _render_decision_table(decisions, references, console: Console) -> None:
    table = Table(box=box.SIMPLE, show_lines=False)
    table.add_column("harness")
    table.add_column("current model", overflow="fold", min_width=22)
    table.add_column("avg score", justify="right")
    table.add_column("recommended", overflow="fold", min_width=22)
    table.add_column("cost vs current", justify="right")
    table.add_column("decision")
    table.add_column("why", overflow="fold")
    for decision in decisions:
        ref = next(
            (r for r in references if r.harness == decision.harness),
            None,
        )
        sessions = f"{ref.session_count_window}s" if ref else "?"
        cost = (
            f"{decision.cost_vs_current:.2f}x"
            if decision.cost_vs_current is not None
            else "-"
        )
        decision_style = {
            "downshift": "[bold green]downshift[/]",
            "upshift": "[bold cyan]upshift[/]",
            "switch-harness": "[bold magenta]switch-harness[/]",
            "stay": "[dim]stay[/]",
            "no-data": "[dim]no-data[/]",
        }.get(decision.label, f"[yellow]{decision.label}[/]")
        table.add_row(
            f"{decision.harness} ({sessions})",
            decision.current_model,
            f"{decision.avg_score:.3f}",
            decision.recommended_model or "-",
            cost,
            decision_style,
            decision.note,
        )
    console.print(table)


def _result_to_dict(result: Any, rewrite_outcomes: list[Any]) -> dict[str, Any]:
    if isinstance(result, IterativeRouteResult):
        return {
            "mode": "iterative",
            "run_id": result.run_id,
            "run_dir": result.run_dir,
            "iterations": len(result.iterations),
            "total_cost_usd": result.total_cost_usd,
            "bail_reason": result.bail_reason,
            "committed": result.committed,
            "references": [
                {
                    "harness": r.harness,
                    "current_model": r.current_model,
                    "last_session_ts": r.last_session_ts,
                    "session_count_window": r.session_count_window,
                }
                for r in result.references
            ],
            "final_decisions": [
                {
                    "harness": d.harness,
                    "current_model": d.current_model,
                    "recommended_model": d.recommended_model,
                    "label": d.label,
                    "note": d.note,
                    "avg_score": d.avg_score,
                    "cost_vs_current": d.cost_vs_current,
                }
                for d in result.final_decisions
            ],
            "rewrites": [asdict(o) for o in rewrite_outcomes],
        }
    # one-shot path
    return {
        "mode": "one-shot",
        "run_id": result.run_id,
        "run_dir": result.run_dir,
        "references": [
            {
                "harness": r.harness,
                "current_model": r.current_model,
                "last_session_ts": r.last_session_ts,
                "session_count_window": r.session_count_window,
            }
            for r in result.references
        ],
        "decisions": [
            {
                "harness": d.harness,
                "current_model": d.current_model,
                "recommended_model": d.recommended_model,
                "label": d.label,
                "note": d.note,
                "avg_score": d.avg_score,
                "cost_vs_current": d.cost_vs_current,
            }
            for d in result.decisions
        ],
        "rewrites": [asdict(o) for o in rewrite_outcomes],
    }
