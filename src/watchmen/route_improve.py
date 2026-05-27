"""Iterative skill improvement for ``watchmen route``.

The point of ``route`` is not just "pick the best model your harness can
reach today" — it's "keep tightening the skill until a cheap model can
carry it without quality loss."  Cheaper models tend to fail on the same
recognisable patterns: ambiguous triggers, missing concrete steps, verbose
prose where a bulleted procedure would do, missing ``when_not_to_use``
boundaries, hallucinated project-specific details.  When watchmen's judge
rationales call those out, ``route`` invokes its own skill-improver pass
(framed user-facing as "watchmen revised the skill") to rewrite SKILL.md
and re-sweeps the candidate pool.  The cheapest model that clears the
threshold wins, and watchmen commits the improved skill — never the
original — back to the bundle.

Hard stop on bail-out: if no cheaper model clears after ``max_iters``,
the user's existing SKILL.md is preserved.  ``--commit-improvements``
opts into keeping the improved skill anyway as a quality upgrade.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

from watchmen.compare import (
    CompareConfig,
    CompareResult,
    ScoreRecord,
    SkillBucketEvidence,
    _call_model,
    load_skill_bucket_evidence,
    run_compare,
)
from watchmen.route import (
    HarnessReference,
    RouteConfig,
    RouteDecision,
    RouteResult,
    candidates_for_harness,
    classify_route,
    detect_harnesses,
    model_id_for_provider,
    native_provider_for_harness,
)
from watchmen.util import bundle_dir


# ─── Constants ───────────────────────────────────────────────────────

# Watchmen's improver model.  Defaults to a strong model because skill
# rewrites are sensitive to subtle structural moves the judge will
# catch; using a cheap improver defeats the point.
DEFAULT_IMPROVER_MODEL = "anthropic/claude-opus-4-7"

# Cull obvious losers between iterations so we don't pay to re-test
# candidates that have no chance.  Anything more than this far below
# the converged-target threshold gets dropped.
CULL_FLOOR_GAP = 0.20

# Cap on the number of failure rationales fed to the improver per
# iteration — too many makes the prompt huge without proportional gain.
MAX_RATIONALES_PER_ITER = 12

# Floor on the auto-computed threshold.  Below this, even a "passing"
# candidate isn't telling us anything useful — the comparison is too
# noisy to trust.  User can still pin --threshold explicitly to bypass.
MIN_AUTO_THRESHOLD = 0.5

# Below this judge score, the reference is presumed unjudged or broken
# (OpenRouter aliasing, judge skipped the output, etc).  When that's
# true the whole comparison is unanchored — we emit `no-data` for the
# harness rather than routing on garbage.
REFERENCE_RELIABLE_FLOOR = 0.30


def _reference_unroutable(cmp_result) -> tuple[bool, str]:
    """Did all reference generations error out / produce no real output?

    Symptoms: every reference generation has either an explicit
    ``error`` field or zero produced_tokens AND zero cost.  OpenRouter
    will sometimes return content for a model id it doesn't know (the
    judge ends up scoring nonsense), but more commonly the call errors
    out fast and the reference summary's cost/tokens are both zero.
    We want to distinguish "model is unreachable" from "judge gave it
    a bad score" — different remedies.

    When `error_kind` is set on every failed generation we report the
    specific kind ("rate_limit", "auth", "unknown_model") so the user
    knows whether to wait, fix credentials, or pick a different model.
    """
    ref_gens = [g for g in cmp_result.generations if g.role == "reference"]
    if not ref_gens:
        return True, "no reference generations recorded"
    errors = [g for g in ref_gens if g.error]
    if errors and len(errors) == len(ref_gens):
        # Look at error kinds across all failures. If they're all the same
        # specific kind, surface it; otherwise fall back to the generic msg.
        kinds = {(g.error_kind or "other") for g in errors}
        if len(kinds) == 1:
            kind = next(iter(kinds))
            specific = {
                "rate_limit": (
                    f"all {len(ref_gens)} reference generation(s) rate-limited "
                    "— OAuth subscription quota exhausted for this model "
                    "(wait for weekly cycle, or set ANTHROPIC_API_KEY to "
                    "burst onto metered API)"
                ),
                "auth": (
                    f"all {len(ref_gens)} reference generation(s) auth-failed "
                    "— credential invalid or missing (run `claude login` / "
                    "`codex login` to refresh)"
                ),
                "unknown_model": (
                    f"all {len(ref_gens)} reference generation(s) reported "
                    "unknown model id — check the model is in the provider's "
                    "current /v1/models list"
                ),
            }.get(kind)
            if specific:
                return True, specific
        return True, f"all {len(ref_gens)} reference generation(s) errored — model id likely not routable"
    no_tokens = [
        g for g in ref_gens
        if int((g.usage or {}).get("completion_tokens", 0) or 0) == 0
        and float(g.cost_usd or 0) == 0
    ]
    if len(no_tokens) == len(ref_gens):
        return True, f"all {len(ref_gens)} reference generation(s) returned empty/zero-cost — model id likely not routable via this provider"
    return False, ""


# ─── Types ───────────────────────────────────────────────────────────

@dataclass
class IterationResult:
    """One pass of (compare each harness) + (improve skill, maybe)."""

    iter_idx: int
    route_result: RouteResult
    cost_usd: float
    converged_harnesses: list[str]  # cleared threshold this iter
    revised_skill_md: str | None  # what watchmen wrote for the *next* iter
    improver_cost_usd: float


@dataclass
class IterativeRouteResult:
    run_id: str
    run_dir: str
    config: RouteConfig
    references: list[HarnessReference]
    iterations: list[IterationResult]
    final_decisions: list[RouteDecision]  # per harness, final winner
    final_skill_md: str  # what gets written back to the bundle (or kept,
                         # if --no commit decision was taken)
    committed: bool  # did we replace the user's SKILL.md on disk?
    bail_reason: str  # "converged" | "max_iters" | "max_cost" | "no_progress"
    total_cost_usd: float


# ─── Threshold ───────────────────────────────────────────────────────

def threshold_for_reference(
    reference_score: float, *, absolute: float | None, offset: float
) -> float:
    """If the user pinned an absolute threshold, use it.  Otherwise the
    target is ``reference - offset`` so a tough skill where the reference
    only scored 0.78 doesn't have an unreachable 0.85 hanging over it.
    """
    if absolute is not None:
        return absolute
    return max(MIN_AUTO_THRESHOLD, reference_score + offset)


def harness_converged(
    decision: RouteDecision, *, threshold: float
) -> bool:
    """A harness has converged when at least one healthy candidate
    cleared the threshold.  We don't require ``downshift`` specifically —
    an ``upshift`` candidate that landed above threshold is also a valid
    convergence (it just means improving the skill didn't help the
    cheaper models).
    """
    if decision.summary is None:
        return False
    if decision.label in {"invalid", "unstable", "truncated", "dominated", "no-data"}:
        return False
    return decision.summary.avg_score >= threshold


# ─── Improver ────────────────────────────────────────────────────────

def improve_skill_with_watchmen(
    *,
    evidence: SkillBucketEvidence,
    iter_result: RouteResult,
    target_threshold: float,
    improver_model: str,
    provider: str,
    progress: Callable[[str], None] | None = None,
) -> tuple[str, float]:
    """Ask watchmen to revise SKILL.md so cheaper models can clear the
    target.  Returns ``(revised_skill_md, cost_usd_for_this_call)``.
    """
    rationales = _collect_failure_rationales(iter_result, target_threshold)
    messages = _improver_messages(
        evidence=evidence,
        rationales=rationales,
        target_threshold=target_threshold,
    )
    if progress:
        progress(
            f"watchmen revising SKILL.md "
            f"({len(rationales)} failure rationales, target ≥ {target_threshold:.2f})"
        )
    with httpx.Client(timeout=300.0) as client:
        content, usage = _call_model(
            client,
            provider=provider,
            model=improver_model,
            messages=messages,
            agent_name="route-improver",
            temperature=0.2,
            max_tokens=4000,
        )
    revised = _strip_fences(content).strip()
    if not revised:
        # Improver returned empty.  Surface as a no-op so the caller can
        # decide whether to bail; don't poison evidence with an empty skill.
        return evidence.skill_md, _cost_from_usage(improver_model, usage)
    return revised, _cost_from_usage(improver_model, usage)


def _collect_failure_rationales(
    result: RouteResult, target_threshold: float
) -> list[dict[str, Any]]:
    """Per harness, gather the judge rationales for candidates that
    didn't clear the target.  Capped so the improver prompt stays bounded.
    """
    out: list[dict[str, Any]] = []
    for harness, compare_result in result.compare_results.items():
        score_index = {s.output_id: s for s in compare_result.scores}
        for summary in compare_result.summaries:
            if summary.role == "reference":
                continue
            if summary.avg_score >= target_threshold:
                continue  # already passing, no need to tell watchmen why
            # Find the lowest-scoring sample for this model and use its
            # rationale; that's the most actionable failure mode.
            samples = [
                g for g in compare_result.generations
                if g.model == summary.model
            ]
            if not samples:
                continue
            worst = min(
                samples,
                key=lambda g: score_index.get(
                    g.output_id, ScoreRecord("", "", 1.0, 0, 0, 0, 0, 0, "")
                ).score,
            )
            score_row = score_index.get(worst.output_id)
            if score_row is None or not score_row.rationale:
                continue
            out.append(
                {
                    "harness": harness,
                    "model": summary.model,
                    "score": score_row.score,
                    "rationale": score_row.rationale,
                }
            )
            if len(out) >= MAX_RATIONALES_PER_ITER:
                return out
    return out


def _improver_messages(
    *,
    evidence: SkillBucketEvidence,
    rationales: list[dict[str, Any]],
    target_threshold: float,
) -> list[dict[str, str]]:
    system = (
        "You are watchmen, the skill-improvement agent.  Your job: rewrite "
        "the SKILL.md below so cheaper coding-agent models can carry it "
        "without quality loss against watchmen's blind judge.  Return ONLY "
        "the revised SKILL.md content (including YAML frontmatter).  Do "
        "not wrap in markdown fences.  Do not add commentary."
    )

    failure_block = "\n\n".join(
        f"### {r['harness']} / {r['model']} scored {r['score']:.2f}\n"
        f"Judge rationale: {r['rationale']}"
        for r in rationales
    ) or "(none — all candidates already at or above target threshold)"

    user = (
        f"## Target\n"
        f"Lift the lowest-scoring cheap candidate above "
        f"{target_threshold:.2f} on watchmen's judge.  Common failure modes "
        f"in cheap models for SKILL.md generation: ambiguous when_to_use "
        f"triggers, missing concrete procedure steps, verbose prose where "
        f"a bulleted procedure would do, missing when_not_to_use "
        f"boundaries, and hallucinated project-specific files or commands.\n\n"
        f"## Constraints\n"
        f"1. Preserve the skill name + slug (the `name:` frontmatter field "
        f"and the conceptual identity of the skill).\n"
        f"2. Preserve any `<!-- watchmen-route:dispatch -->` ... "
        f"`<!-- /watchmen-route:dispatch -->` block exactly as-is.\n"
        f"3. Only add details grounded in the evidence packet below — no "
        f"hallucinated files, commands, or project-specific facts.\n"
        f"4. Prefer fewer, more concrete words over prose elaboration.\n\n"
        f"## Failure rationales from cheaper models this iteration\n\n"
        f"{failure_block}\n\n"
        f"## Current SKILL.md (rewrite this)\n\n"
        f"{evidence.skill_md}\n\n"
        f"## Evidence packet (ground every project-specific detail in this)\n\n"
        f"Candidate metadata: {json.dumps(evidence.candidate or {}, indent=2)}\n\n"
        f"Workspace brief:\n{evidence.workspace_brief}\n\n"
        f"Curation log excerpt:\n{evidence.curation_log or '(none)'}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _strip_fences(text: str) -> str:
    """Defensive: some improver models add ```markdown wrappers even when
    told not to.  Strip them.
    """
    t = text.strip()
    if t.startswith("```"):
        # Drop the opening fence (with optional language tag).
        first_nl = t.find("\n")
        if first_nl != -1:
            t = t[first_nl + 1 :]
    if t.endswith("```"):
        t = t[: -3].rstrip()
    return t


def _cost_from_usage(model: str, usage: dict[str, Any]) -> float:
    from watchmen.compare import _cost_for_usage
    return _cost_for_usage(model, usage)


# ─── Iterative outer loop ────────────────────────────────────────────

def run_route_iterative(
    config: RouteConfig,
    *,
    threshold_absolute: float | None,
    threshold_offset: float,
    max_iters: int,
    max_cost_usd: float | None,
    improver_model: str,
    commit_improvements: bool,
    commit_skill: bool = True,
    progress: Callable[[str], None] | None = None,
) -> IterativeRouteResult:
    """Run compare per harness, improve SKILL.md with watchmen's revisor,
    repeat until each harness has a candidate clearing its target threshold
    (or we hit ``max_iters`` / ``max_cost_usd``).

    Hard stop: if no harness converged, the user's existing SKILL.md is
    NOT replaced unless ``commit_improvements`` is set.
    """
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = bundle_dir(config.project_key) / "_route" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    detected = detect_harnesses(
        config.project_key, since_days=config.since_days
    )
    references = _filter_refs(detected, config.harnesses)
    if not references:
        return IterativeRouteResult(
            run_id=run_id,
            run_dir=str(run_dir),
            config=config,
            references=[],
            iterations=[],
            final_decisions=[],
            final_skill_md="",
            committed=False,
            bail_reason="no_harnesses",
            total_cost_usd=0.0,
        )

    # Initial evidence comes from disk.  Subsequent iterations replace
    # the in-memory ``skill_md`` field without touching the bundle.
    base_evidence = load_skill_bucket_evidence(config.project_key, config.bucket)
    original_skill_md = base_evidence.skill_md
    current_evidence = base_evidence

    # Per harness: which models are still in the running.  Culled across
    # iterations.
    live_candidates: dict[str, list[str]] = {
        ref.harness: candidates_for_harness(
            ref.harness, ref.current_model,
            project_key=config.project_key,
            user_candidates=config.user_candidates,
            since_days=config.since_days * 3,  # widen lookback for candidates
        )
        for ref in references
    }
    # Backend that actually serves each candidate. Own candidates run on the
    # harness's native provider; cross-harness injections run on the backend of
    # the harness that uses them, so a foreign model generates for real in
    # compare instead of erroring under a provider that can't serve it.
    candidate_backend: dict[str, dict[str, str]] = {
        ref.harness: {
            m: native_provider_for_harness(ref.harness)
            for m in live_candidates[ref.harness]
        }
        for ref in references
    }
    if config.cross_harness:
        for ref in references:
            for other in references:
                if other.harness == ref.harness:
                    continue
                if other.current_model not in live_candidates[ref.harness] \
                        and other.current_model != ref.current_model:
                    live_candidates[ref.harness].append(other.current_model)
                    candidate_backend[ref.harness][other.current_model] = \
                        native_provider_for_harness(other.harness)

    iterations: list[IterationResult] = []
    total_cost = 0.0
    converged: dict[str, RouteDecision] = {}
    bail_reason = "max_iters"

    for iter_idx in range(max_iters):
        if progress:
            progress(f"iter {iter_idx + 1}/{max_iters} starting")

        # Run one compare per still-active harness.
        compare_results: dict[str, CompareResult] = {}
        decisions: list[RouteDecision] = []
        iter_cost = 0.0
        active_refs = [r for r in references if r.harness not in converged]

        for ref in active_refs:
            cands = live_candidates[ref.harness]
            if not cands:
                decisions.append(
                    RouteDecision(
                        harness=ref.harness, current_model=ref.current_model,
                        recommended_model=None, label="no-data",
                        note="no candidate pool", avg_score=0.0,
                        cost_vs_current=None,
                    )
                )
                continue
            harness_provider = native_provider_for_harness(ref.harness)
            harness_judge = _default_judge_for_provider(
                harness_provider,
                reference_model=ref.current_model,
                override=config.judge_model,
            )
            # Normalize each id for the backend that will run it (bare for
            # native-bare providers, namespaced for OpenRouter), and record
            # any non-native backend so compare routes that candidate there.
            backend = candidate_backend[ref.harness]
            norm_candidates: list[str] = []
            candidate_providers: dict[str, str] = {}
            for c in cands:
                prov = backend.get(c, harness_provider)
                norm = model_id_for_provider(c, prov)
                norm_candidates.append(norm)
                if prov != harness_provider:
                    candidate_providers[norm] = prov
            cmp_cfg = CompareConfig(
                project_key=config.project_key, bucket=config.bucket,
                reference_model=model_id_for_provider(
                    ref.current_model, harness_provider,
                ),
                judge_model=harness_judge,
                candidates=norm_candidates,
                task_count=config.task_count, reference_n=1,
                candidate_n=config.candidate_n,
                provider=harness_provider,
                candidate_providers=candidate_providers,
                temperature=config.temperature, max_tokens=config.max_tokens,
                generation_concurrency=config.generation_concurrency,
            )
            sub_id = f"{run_id}_iter{iter_idx}_{ref.harness}"
            if progress:
                progress(
                    f"  {ref.harness}: compare ref={ref.current_model} "
                    f"× {len(cands)} candidate(s)"
                )
            cmp_result = run_compare(
                cmp_cfg, run_id=sub_id, progress=progress,
                evidence=current_evidence,
            )
            compare_results[ref.harness] = cmp_result
            iter_cost += sum(g.cost_usd for g in cmp_result.generations)
            decisions.append(classify_route(ref, cmp_result, references))

        # Roll the all-harnesses decisions list forward, plugging in any
        # harness that converged earlier so RouteResult stays coherent.
        for ref in references:
            if ref.harness in converged:
                decisions.append(converged[ref.harness])

        iter_route_result = RouteResult(
            run_id=f"{run_id}_iter{iter_idx}",
            run_dir=str(run_dir / f"iter{iter_idx}"),
            config=config, references=references,
            compare_results=compare_results, decisions=decisions,
        )

        # Compute per-harness threshold (using this iter's reference score)
        # and harvest convergences.
        ref_threshold_per_harness: dict[str, float] = {}
        new_converged_this_iter: list[str] = []
        for d in decisions:
            if d.harness in converged:
                continue
            cmp_result = compare_results.get(d.harness)
            if cmp_result is None:
                continue
            ref_row = next(
                (s for s in cmp_result.summaries if s.role == "reference"),
                None,
            )
            ref_score = ref_row.avg_score if ref_row else 0.0
            ref_for_harness = next(
                (r for r in references if r.harness == d.harness), None
            )
            ref_model = ref_for_harness.current_model if ref_for_harness else d.current_model
            unroutable, unroutable_reason = _reference_unroutable(cmp_result)
            if unroutable or ref_score < REFERENCE_RELIABLE_FLOOR:
                # Either the reference model couldn't be routed at all,
                # or it was routed but produced unjudgeable content.
                # Either way the compare is unanchored and revising the
                # skill won't help.  Emit no-data and skip the harness.
                note = (
                    f"reference {ref_model}: {unroutable_reason}"
                    if unroutable
                    else (
                        f"reference {ref_model} scored {ref_score:.2f} on the judge; "
                        "comparison is unanchored — skipping"
                    )
                )
                converged[d.harness] = RouteDecision(
                    harness=d.harness,
                    current_model=ref_model,
                    recommended_model=None,
                    label="no-data",
                    note=note,
                    avg_score=ref_score,
                    cost_vs_current=None,
                )
                new_converged_this_iter.append(d.harness)
                continue
            t = threshold_for_reference(
                ref_score, absolute=threshold_absolute, offset=threshold_offset
            )
            ref_threshold_per_harness[d.harness] = t
            if harness_converged(d, threshold=t):
                converged[d.harness] = _pick_cheapest_passing(
                    d, cmp_result, threshold=t
                )
                new_converged_this_iter.append(d.harness)

        # Decide whether we improve before the next iter or stop.
        revised_md: str | None = None
        improver_cost = 0.0
        all_done = all(r.harness in converged for r in references)
        cost_exceeded = max_cost_usd is not None and (total_cost + iter_cost) >= max_cost_usd

        # If every harness ended in a no-data label, the loop can't
        # progress: skill revision doesn't fix unroutable model ids or
        # empty candidate pools.  Bail explicitly rather than burn
        # cycles on improver calls with no failure rationales.
        nothing_actionable = bool(converged) and all(
            converged[h].label == "no-data" for h in converged
        )

        if (
            not all_done and iter_idx + 1 < max_iters
            and not cost_exceeded and not nothing_actionable
        ):
            try:
                revised_md, improver_cost = improve_skill_with_watchmen(
                    evidence=current_evidence,
                    iter_result=iter_route_result,
                    target_threshold=_average(ref_threshold_per_harness.values()) or 0.0,
                    improver_model=improver_model,
                    provider=config.provider,
                    progress=progress,
                )
            except Exception as exc:  # noqa: BLE001 — improver failures shouldn't kill the run
                if progress:
                    progress(f"  improver failed: {type(exc).__name__}: {exc}")
                revised_md = None

        iterations.append(
            IterationResult(
                iter_idx=iter_idx,
                route_result=iter_route_result,
                cost_usd=iter_cost,
                converged_harnesses=new_converged_this_iter,
                revised_skill_md=revised_md,
                improver_cost_usd=improver_cost,
            )
        )

        total_cost += iter_cost + improver_cost

        if nothing_actionable:
            bail_reason = "unroutable"
            break
        if all_done:
            bail_reason = "converged"
            break
        if cost_exceeded:
            bail_reason = "max_cost"
            break

        is_last_iter = iter_idx + 1 >= max_iters
        if revised_md and revised_md != current_evidence.skill_md:
            current_evidence = replace(current_evidence, skill_md=revised_md)
            live_candidates = _cull_failing_candidates(
                live_candidates, iter_route_result,
                ref_thresholds=ref_threshold_per_harness,
            )
        elif revised_md is None and not is_last_iter:
            # Improver was called but returned nothing usable; no point
            # re-running compare on the same evidence.  On the last iter
            # the improver isn't called by design, so this branch only
            # fires for genuine improver failures.
            bail_reason = "no_progress"
            break

    # If some harnesses never converged, pad final_decisions with the
    # best label we have for them.
    final_decisions: list[RouteDecision] = []
    for ref in references:
        if ref.harness in converged:
            final_decisions.append(converged[ref.harness])
        else:
            # Use the last iter's decision for this harness, if any.
            last_for_harness = None
            for it in reversed(iterations):
                for d in it.route_result.decisions:
                    if d.harness == ref.harness:
                        last_for_harness = d
                        break
                if last_for_harness is not None:
                    break
            if last_for_harness is not None:
                # If the candidate exists but didn't clear, fall back to "stay".
                final_decisions.append(
                    RouteDecision(
                        harness=ref.harness,
                        current_model=ref.current_model,
                        recommended_model=None,
                        label="stay",
                        note=(
                            "no candidate cleared target threshold after "
                            f"{len(iterations)} iteration(s); preserving current model"
                        ),
                        avg_score=last_for_harness.avg_score,
                        cost_vs_current=None,
                        summary=last_for_harness.summary,
                    )
                )

    # Commit policy: hard stop unless --commit-improvements OR at least
    # one harness converged with the improved skill.
    any_converged = bool(converged)
    should_commit = (any_converged or commit_improvements) and commit_skill
    final_skill_md = (
        current_evidence.skill_md if should_commit else original_skill_md
    )
    if should_commit and current_evidence.skill_md != original_skill_md:
        _commit_skill_to_bundle(
            project_key=config.project_key,
            bucket=config.bucket,
            new_skill_md=current_evidence.skill_md,
        )

    _write_run_artifacts(
        run_dir=run_dir, config=config, references=references,
        iterations=iterations, final_decisions=final_decisions,
        final_skill_md=final_skill_md, committed=(should_commit and
            current_evidence.skill_md != original_skill_md),
        bail_reason=bail_reason, total_cost=total_cost,
    )

    return IterativeRouteResult(
        run_id=run_id,
        run_dir=str(run_dir),
        config=config,
        references=references,
        iterations=iterations,
        final_decisions=final_decisions,
        final_skill_md=final_skill_md,
        committed=(should_commit and current_evidence.skill_md != original_skill_md),
        bail_reason=bail_reason,
        total_cost_usd=total_cost,
    )


# ─── Per-iter helpers ────────────────────────────────────────────────

def _filter_refs(
    detected: list[HarnessReference], user_harnesses: list[str]
) -> list[HarnessReference]:
    if not user_harnesses:
        return detected
    wanted = {h.replace("-", "_") for h in user_harnesses}
    return [r for r in detected if r.harness in wanted]


def _pick_cheapest_passing(
    decision: RouteDecision, compare_result: CompareResult, *, threshold: float
) -> RouteDecision:
    """Once a harness has converged, pick the *cheapest* passing
    candidate (not the highest-quality one).  Sweep semantics: the user
    wants the cost floor, not the quality ceiling.

    Guard against the "swap to a worse, costlier model" trap. If the
    cheapest passer isn't a Pareto improvement over the reference —
    it's both lower-quality AND not cheaper — recommend `stay`. The
    threshold floor lets candidates that are roughly as good as the
    reference clear convergence, but that doesn't mean swapping to one
    is the right call when the reference is *also* roughly as good
    AND already paid for.
    """
    passing = [
        s for s in compare_result.summaries
        if s.role == "candidate"
        and s.avg_score >= threshold
        and s.decision not in {"invalid output", "unstable output", "truncated output", "dominated"}
    ]
    if not passing:
        return decision  # shouldn't happen — caller already checked harness_converged
    winner = sorted(passing, key=lambda s: (s.cost_usd, -s.avg_score))[0]
    cost_ratio = winner.cost_vs_reference

    # Find the reference row so we can compare scores. Without it we
    # can't tell whether the winner is materially better than what the
    # user already runs.
    ref_row = next(
        (s for s in compare_result.summaries if s.role == "reference"),
        None,
    )

    # Pareto-improvement check: a swap is only worth recommending when
    # the winner is *meaningfully* better OR *meaningfully* cheaper than
    # the reference. If neither, stay. Thresholds match classify_route's
    # stay-guard so the one-shot and iterative paths agree.
    if ref_row is not None:
        not_better = winner.avg_score < ref_row.avg_score + 0.02
        not_cheaper = cost_ratio is None or cost_ratio >= 0.95
        if not_better and not_cheaper:
            return RouteDecision(
                harness=decision.harness,
                current_model=decision.current_model,
                recommended_model=None,
                label="stay",
                note=(
                    f"best passer {winner.model} scored "
                    f"{winner.avg_score:.3f} vs ref {ref_row.avg_score:.3f} "
                    f"({cost_ratio:.2f}× cost)" if cost_ratio is not None
                    else f"best passer {winner.model} scored "
                         f"{winner.avg_score:.3f} vs ref {ref_row.avg_score:.3f} "
                         f"(cost data unavailable)"
                ) + "; not worth a swap",
                avg_score=ref_row.avg_score,
                cost_vs_current=cost_ratio,
                summary=winner,
            )

    if cost_ratio is not None and cost_ratio < 1.0:
        label, note = "downshift", (
            f"cheapest passer after iteration; "
            f"{cost_ratio:.2f}× cost vs reference, ≥ threshold {threshold:.2f}"
        )
    elif cost_ratio is not None and cost_ratio > 1.0:
        label, note = "upshift", (
            f"pricier candidate at {cost_ratio:.2f}× cost cleared threshold; "
            "no cheaper model did"
        )
    else:
        label, note = "stay", (
            f"winner score {winner.avg_score:.2f} matches threshold "
            f"{threshold:.2f} but cost data unavailable"
        )
    return RouteDecision(
        harness=decision.harness,
        current_model=decision.current_model,
        recommended_model=winner.model,
        label=label,
        note=note,
        avg_score=winner.avg_score,
        cost_vs_current=cost_ratio,
        summary=winner,
    )


def _cull_failing_candidates(
    live_candidates: dict[str, list[str]],
    iter_result: RouteResult,
    *,
    ref_thresholds: dict[str, float],
) -> dict[str, list[str]]:
    """Drop candidates that scored ``< threshold - CULL_FLOOR_GAP`` so we
    don't pay to re-test obvious losers.  A model that scored 0.4 against
    a 0.85 target won't catch up via prose edits.
    """
    culled: dict[str, list[str]] = {}
    for harness, cands in live_candidates.items():
        cmp_result = iter_result.compare_results.get(harness)
        threshold = ref_thresholds.get(harness, 0.0)
        if cmp_result is None:
            culled[harness] = list(cands)
            continue
        floor = max(0.0, threshold - CULL_FLOOR_GAP)
        by_model = {s.model: s for s in cmp_result.summaries}
        kept = [
            c for c in cands
            if c not in by_model or by_model[c].avg_score >= floor
        ]
        culled[harness] = kept
    return culled


def _average(values) -> float | None:
    vals = list(values)
    if not vals:
        return None
    return sum(vals) / len(vals)


# ─── Bundle commit + artifact write ──────────────────────────────────

def _commit_skill_to_bundle(*, project_key: str, bucket: str, new_skill_md: str) -> None:
    """Replace the on-disk SKILL.md with the improved version.  Caller
    is responsible for any audit logging."""
    path = bundle_dir(project_key) / "skills" / bucket / "SKILL.md"
    path.write_text(new_skill_md, encoding="utf-8")


def _write_run_artifacts(
    *,
    run_dir: Path,
    config: RouteConfig,
    references: list[HarnessReference],
    iterations: list[IterationResult],
    final_decisions: list[RouteDecision],
    final_skill_md: str,
    committed: bool,
    bail_reason: str,
    total_cost: float,
) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "iterations.json").write_text(
        json.dumps(
            [
                {
                    "iter": it.iter_idx,
                    "cost_usd": it.cost_usd,
                    "improver_cost_usd": it.improver_cost_usd,
                    "converged_this_iter": it.converged_harnesses,
                    "skill_revised": it.revised_skill_md is not None,
                    "decisions": [
                        {
                            "harness": d.harness,
                            "current_model": d.current_model,
                            "recommended_model": d.recommended_model,
                            "label": d.label,
                            "note": d.note,
                            "avg_score": d.avg_score,
                        }
                        for d in it.route_result.decisions
                    ],
                }
                for it in iterations
            ],
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (run_dir / "final_decisions.json").write_text(
        json.dumps(
            [
                {
                    "harness": d.harness,
                    "current_model": d.current_model,
                    "recommended_model": d.recommended_model,
                    "label": d.label,
                    "note": d.note,
                    "avg_score": d.avg_score,
                    "cost_vs_current": d.cost_vs_current,
                }
                for d in final_decisions
            ],
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    # Persist the final improved skill so the user can hand-restore if
    # they don't like what watchmen wrote.
    (run_dir / "SKILL.md.final").write_text(final_skill_md, encoding="utf-8")
    (run_dir / "summary.json").write_text(
        json.dumps(
            {
                "committed": committed,
                "bail_reason": bail_reason,
                "total_cost_usd": round(total_cost, 4),
                "iterations": len(iterations),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )



def _default_judge_for_provider(
    provider: str,
    *,
    reference_model: str,
    override: str | None = None,
) -> str:
    """Pick a judge model id for this iteration's compare call.

    Default policy: **judge with the harness's reference (modal) model.**
    This is the same model the user runs on this harness day-to-day, which
    means two things:

      1. **Quota alignment.** The judge call stays inside the quota the
         user already pays for. A hardcoded "strong" default like
         opus-4-7 ends up 429-ing on subscription tiers where opus is
         the most quota-constrained model — even though haiku/sonnet have
         plenty of budget and would judge fine.
      2. **Anchoring.** Decisions are evaluated by the same intelligence
         level the user is comfortable with. If they wouldn't trust the
         judge to do their day job, they shouldn't trust it to grade swap
         candidates either.

    `--judge` always wins when explicitly set.
    """
    if override and override.strip():
        # Always honour an explicit --judge value.
        return override
    # Strip provider prefix when the harness's provider takes bare ids
    # (claude-pro OAuth, chatgpt OAuth, anthropic-direct, openai-direct).
    from watchmen.route import model_id_for_provider
    return model_id_for_provider(reference_model, provider)


# Legacy provider-keyed lookup retained only as a fallback layer for
# tests / external callers that never had a reference model handy. Not
# wired into the live route paths anymore.
_LEGACY_PROVIDER_JUDGE = {
    "claude-pro": "claude-opus-4-7",
    "anthropic": "claude-opus-4-7",
    "chatgpt": "gpt-5.5",
    "openai": "gpt-5.5",
    "openrouter": "openai/gpt-5.5",
}
