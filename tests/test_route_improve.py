"""Tests for the iterative skill-improvement loop on top of `watchmen route`."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


def _now_iso(offset_days: int = 0) -> str:
    return (
        datetime.now(timezone.utc).replace(microsecond=0)
        - timedelta(days=offset_days)
    ).isoformat().replace("+00:00", "Z")


# ─── Threshold helper ────────────────────────────────────────────────


def test_threshold_for_reference_uses_absolute_when_set():
    from watchmen.route_improve import threshold_for_reference

    assert threshold_for_reference(0.85, absolute=0.90, offset=-0.05) == 0.90


def test_threshold_for_reference_falls_back_to_offset_relative_to_ref():
    from watchmen.route_improve import threshold_for_reference, MIN_AUTO_THRESHOLD

    # offset = -0.05 → "within 5% of reference is fine"
    assert threshold_for_reference(0.85, absolute=None, offset=-0.05) == pytest.approx(0.80)
    # Auto threshold floors at MIN_AUTO_THRESHOLD so a noisy low-ref run
    # doesn't end up with a "any candidate passes" target.
    assert threshold_for_reference(0.02, absolute=None, offset=-0.10) == MIN_AUTO_THRESHOLD
    # Explicit --threshold still honoured below the floor (user opt-in).
    assert threshold_for_reference(0.02, absolute=0.10, offset=-0.05) == 0.10


# ─── Convergence guard ──────────────────────────────────────────────


def _summary(model: str, role: str, score: float, decision: str = "pending"):
    from watchmen.compare import ModelSummary
    return ModelSummary(
        model=model, role=role, avg_score=score, worst_score=score,
        wins_vs_reference=2, task_count=3, cost_usd=1.0, cost_vs_reference=1.0,
        produced_tokens=1000, produced_tokens_vs_reference=1.0,
        visible_chars=1000, empty_outputs=0, maxed_outputs=0,
        sample_count=3, latency_s=10.0, latency_vs_reference=1.0,
        decision=decision, decision_note="",
    )


def test_harness_converged_requires_healthy_label():
    from watchmen.route import RouteDecision
    from watchmen.route_improve import harness_converged

    d = RouteDecision(
        harness="claude_code", current_model="anthropic/claude-opus-4-7",
        recommended_model="anthropic/claude-haiku-4-5",
        label="downshift", note="cheaper, near quality",
        avg_score=0.88, cost_vs_current=0.20,
        summary=_summary("anthropic/claude-haiku-4-5", "candidate", 0.88),
    )
    assert harness_converged(d, threshold=0.80) is True
    assert harness_converged(d, threshold=0.95) is False

    # invalid output never converges, even if avg_score is high (the
    # summary would carry the invalid label)
    d_bad = RouteDecision(
        harness="codex", current_model="openai/gpt-5.5",
        recommended_model=None, label="invalid", note="",
        avg_score=0.0, cost_vs_current=None,
    )
    assert harness_converged(d_bad, threshold=0.5) is False


# ─── Improver (mocked LLM) ───────────────────────────────────────────


def test_improver_strips_fences_and_returns_revised_md(monkeypatch):
    """Some improver models wrap output in ```markdown fences despite
    the instruction not to.  Confirm we strip them."""
    from watchmen.compare import SkillBucketEvidence
    from watchmen import route_improve

    def fake_call(client, *, provider, model, messages, agent_name,
                  temperature, max_tokens, **kwargs):
        return (
            "```markdown\n---\nname: demo\n---\n# Better skill\nTighter.\n```",
            {"prompt_tokens": 100, "completion_tokens": 80},
        )
    monkeypatch.setattr(route_improve, "_call_model", fake_call)
    # Cost helper isn't under test here; pin it.
    monkeypatch.setattr(
        route_improve, "_cost_from_usage", lambda *a, **kw: 0.01,
    )

    ev = SkillBucketEvidence(
        project_key="p", bucket="b",
        skill_md="---\nname: demo\n---\n# Old skill\n",
        candidate=None, workspace_brief="brief", curation_log="log",
    )
    from watchmen.route import RouteResult, RouteConfig
    empty = RouteResult(
        run_id="t", run_dir="/tmp", config=RouteConfig(project_key="p", bucket="b"),
        references=[], compare_results={}, decisions=[],
    )
    revised, cost = route_improve.improve_skill_with_watchmen(
        evidence=ev, iter_result=empty, target_threshold=0.85,
        improver_model="anthropic/claude-opus-4-7", provider="openrouter",
    )
    assert "Better skill" in revised
    assert "```" not in revised
    assert "# Old skill" not in revised  # actually replaced
    assert cost == 0.01


def test_improver_returns_original_on_empty_response(monkeypatch):
    """If the improver model returns an empty string (rate-limit retry
    fallback, etc), we keep the original skill rather than poisoning the
    next iteration with empty evidence."""
    from watchmen.compare import SkillBucketEvidence
    from watchmen import route_improve
    from watchmen.route import RouteConfig, RouteResult

    monkeypatch.setattr(
        route_improve, "_call_model",
        lambda *args, **kw: ("", {"prompt_tokens": 100, "completion_tokens": 0}),
    )
    monkeypatch.setattr(route_improve, "_cost_from_usage", lambda *a, **kw: 0.005)

    ev = SkillBucketEvidence(
        project_key="p", bucket="b",
        skill_md="---\nname: demo\n---\n# Original\n",
        candidate=None, workspace_brief="", curation_log="",
    )
    revised, cost = route_improve.improve_skill_with_watchmen(
        evidence=ev,
        iter_result=RouteResult(
            run_id="t", run_dir="/tmp",
            config=RouteConfig(project_key="p", bucket="b"),
            references=[], compare_results={}, decisions=[],
        ),
        target_threshold=0.85,
        improver_model="anthropic/claude-opus-4-7", provider="openrouter",
    )
    assert revised == ev.skill_md  # unchanged
    assert cost == 0.005


# ─── Cheapest-passer selection ───────────────────────────────────────


def test_pick_cheapest_passing_prefers_lower_cost_over_higher_score():
    from watchmen.compare import (
        CompareConfig as CmpCfg, CompareResult as CmpResult,
    )
    from watchmen.route_improve import _pick_cheapest_passing
    from watchmen.route import RouteDecision

    # Two passing candidates: cheap one scores barely-above-threshold,
    # expensive one scores higher.  Sweep semantics demand the cheap one.
    cmp_result = CmpResult(
        run_id="r", run_dir="/tmp",
        config=CmpCfg(project_key="p", bucket="b", reference_model="ref",
                     candidates=["cheap", "premium"]),
        tasks=[], generations=[], scores=[], task_results=[],
        summaries=[
            _summary("ref", "reference", 0.90),
            # cheap: avg 0.82, 0.20× cost vs ref → wins
            type(_summary("cheap", "candidate", 0.82))(
                **{**_summary("cheap", "candidate", 0.82).__dict__,
                   "cost_usd": 0.20, "cost_vs_reference": 0.20}
            ),
            # premium: avg 0.94, 1.30× cost vs ref → loses
            type(_summary("premium", "candidate", 0.94))(
                **{**_summary("premium", "candidate", 0.94).__dict__,
                   "cost_usd": 1.30, "cost_vs_reference": 1.30}
            ),
        ],
    )
    seed_decision = RouteDecision(
        harness="claude_code", current_model="ref",
        recommended_model="premium", label="downshift", note="",
        avg_score=0.82, cost_vs_current=0.20,
    )
    out = _pick_cheapest_passing(seed_decision, cmp_result, threshold=0.80)
    assert out.recommended_model == "cheap"
    assert out.label == "downshift"


# ─── End-to-end iterative loop with mocked compare + improver ────────


class _FakeCompare:
    """Drive `run_compare` from a per-iter script so tests can express
    convergence patterns without going near OpenRouter.

    Each call returns the next scripted result by iter index, taking
    the harness from CompareConfig.candidates' first element as a hint
    of which harness scenario we're acting out.
    """

    def __init__(self, scripts: dict[int, dict[str, list[tuple[str, float, str]]]]):
        # scripts[iter_idx][harness_label] = [(model, score, decision), ...]
        # where the first row is the reference and the rest are candidates.
        self.scripts = scripts
        self.calls = 0

    def __call__(self, config, *, run_id=None, progress=None, evidence=None):
        from watchmen.compare import CompareResult
        # Pull iter from run_id segment "_iterN_..."
        iter_idx = 0
        try:
            iter_idx = int(run_id.split("_iter", 1)[1].split("_", 1)[0])
        except Exception:
            pass
        # Match harness by membership rather than rsplit, since real
        # harness names like "claude_code" contain underscores.
        known = ("claude_code", "codex", "opencode", "pi")
        harness = next((h for h in known if run_id.endswith("_" + h)), "unknown")
        rows = self.scripts.get(iter_idx, {}).get(harness, [])
        from watchmen.compare import ModelSummary, GenerationRecord, ScoreRecord
        summaries = []
        generations = []
        scores = []
        for model, score, decision in rows:
            role = "reference" if not summaries else "candidate"
            # Differentiate cost by model name suffix so cheapest-passer
            # logic has signal: haiku/mini < sonnet < opus.
            cost = (
                0.05 if "haiku" in model or "mini" in model
                else 0.10 if "sonnet" in model
                else 0.30
            )
            summaries.append(
                ModelSummary(
                    model=model, role=role, avg_score=score, worst_score=score,
                    wins_vs_reference=None if role == "reference" else 1,
                    task_count=1, cost_usd=cost,
                    cost_vs_reference=None if role == "reference" else cost / 0.30,
                    produced_tokens=100,
                    produced_tokens_vs_reference=None if role == "reference" else 1.0,
                    visible_chars=100, empty_outputs=0, maxed_outputs=0,
                    sample_count=1, latency_s=5.0,
                    latency_vs_reference=None if role == "reference" else 1.0,
                    decision=decision, decision_note="",
                )
            )
            generations.append(
                GenerationRecord(
                    run_id=run_id, task_id="task-1",
                    output_id=f"task-1-out-{len(generations):03d}",
                    model=model, role=role, sample_index=1,
                    output="x", usage={"completion_tokens": 100},
                    cost_usd=0.10, latency_s=5.0,
                )
            )
            scores.append(
                ScoreRecord(
                    task_id="task-1",
                    output_id=f"task-1-out-{len(scores):03d}",
                    score=score, schema=5, trigger_quality=5,
                    procedure_quality=5, evidence_grounding=5,
                    context_efficiency=5,
                    rationale=f"{model} feedback for iter {iter_idx}",
                )
            )
        self.calls += 1
        return CompareResult(
            run_id=run_id, run_dir=f"/tmp/{run_id}",
            config=config, tasks=[], generations=generations,
            scores=scores, task_results=[], summaries=summaries,
        )


def _wire_route_improve(tmp_path, monkeypatch, *, harnesses_to_seed):
    """Set up bundle_dir + detect_harnesses to point at tmp_path."""
    from watchmen import route as wm_route
    from watchmen import route_improve as wm_imp

    bundles_root = tmp_path / "bundles"
    bundle = bundles_root / "p"
    skill_dir = bundle / "skills" / "demo-bucket"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo-bucket\n---\n# Original SKILL.md\n",
        encoding="utf-8",
    )
    (bundle / "_curation_log.md").write_text("log", encoding="utf-8")

    from watchmen import compare as wm_compare
    monkeypatch.setattr(wm_imp, "bundle_dir", lambda key: bundles_root / key)
    monkeypatch.setattr(wm_route, "bundle_dir", lambda key: bundles_root / key)
    monkeypatch.setattr(wm_compare, "bundle_dir", lambda key: bundles_root / key)
    monkeypatch.setattr(
        wm_imp, "detect_harnesses",
        lambda project_key, since_days=30: harnesses_to_seed,
    )
    # Force a deterministic candidate pool so iter scripts can pin model names.
    monkeypatch.setattr(
        wm_imp, "candidates_for_harness",
        lambda harness, current, **kw: {
            "claude_code": ["anthropic/claude-sonnet-4-6", "anthropic/claude-haiku-4-5"],
            "codex": ["openai/gpt-5-mini"],
        }.get(harness, []),
    )
    return bundle


def test_iterative_converges_at_iter_zero_when_cheap_model_already_passes(
    tmp_path, monkeypatch,
):
    """Iter 0 nails it: cheap model clears the threshold immediately.
    Improver never gets called.  Bail reason: converged."""
    from watchmen import route_improve
    from watchmen.route import HarnessReference, RouteConfig

    refs = [
        HarnessReference(
            harness="claude_code", current_model="anthropic/claude-opus-4-7",
            last_session_ts=_now_iso(0), session_count_window=10,
        ),
    ]
    _wire_route_improve(tmp_path, monkeypatch, harnesses_to_seed=refs)

    fake = _FakeCompare(
        scripts={
            0: {
                "claude_code": [
                    ("anthropic/claude-opus-4-7", 0.90, "reference"),
                    ("anthropic/claude-sonnet-4-6", 0.88, "pending"),
                    ("anthropic/claude-haiku-4-5", 0.86, "pending"),
                ],
            },
        }
    )
    monkeypatch.setattr(route_improve, "run_compare", fake)

    # Fail the test if the improver is called — it shouldn't be.
    def boom(*args, **kwargs):
        raise AssertionError("improver should not run when iter 0 converges")
    monkeypatch.setattr(route_improve, "improve_skill_with_watchmen", boom)

    cfg = RouteConfig(project_key="p", bucket="demo-bucket")
    result = route_improve.run_route_iterative(
        cfg, threshold_absolute=None, threshold_offset=-0.05,
        max_iters=3, max_cost_usd=None,
        improver_model="anthropic/claude-opus-4-7", commit_improvements=False,
    )

    assert result.bail_reason == "converged"
    assert len(result.iterations) == 1
    assert result.committed is False  # SKILL.md was already good; nothing to commit
    # cheapest passer should be haiku, not sonnet
    final = result.final_decisions[0]
    assert final.recommended_model == "anthropic/claude-haiku-4-5"


def test_iterative_uses_improver_when_cheap_models_miss(tmp_path, monkeypatch):
    """Iter 0 fails (no candidate clears).  Improver runs, watchmen
    revises SKILL.md, iter 1 nails it.  Improved skill gets committed."""
    from watchmen import route_improve
    from watchmen.route import HarnessReference, RouteConfig

    refs = [
        HarnessReference(
            harness="claude_code", current_model="anthropic/claude-opus-4-7",
            last_session_ts=_now_iso(0), session_count_window=10,
        ),
    ]
    bundle = _wire_route_improve(tmp_path, monkeypatch, harnesses_to_seed=refs)

    fake = _FakeCompare(
        scripts={
            0: {
                "claude_code": [
                    ("anthropic/claude-opus-4-7", 0.90, "reference"),
                    ("anthropic/claude-sonnet-4-6", 0.70, "pending"),
                    ("anthropic/claude-haiku-4-5", 0.60, "pending"),
                ],
            },
            1: {
                "claude_code": [
                    ("anthropic/claude-opus-4-7", 0.90, "reference"),
                    ("anthropic/claude-sonnet-4-6", 0.88, "pending"),
                    ("anthropic/claude-haiku-4-5", 0.86, "pending"),
                ],
            },
        }
    )
    monkeypatch.setattr(route_improve, "run_compare", fake)

    improver_calls = {"n": 0}

    def fake_improver(*, evidence, iter_result, target_threshold,
                      improver_model, provider, progress=None):
        improver_calls["n"] += 1
        return (
            evidence.skill_md + "\n## Watchmen revision iter "
            f"{improver_calls['n']}\nTighter.\n",
            0.02,
        )
    monkeypatch.setattr(
        route_improve, "improve_skill_with_watchmen", fake_improver,
    )

    cfg = RouteConfig(project_key="p", bucket="demo-bucket")
    result = route_improve.run_route_iterative(
        cfg, threshold_absolute=None, threshold_offset=-0.05,
        max_iters=3, max_cost_usd=None,
        improver_model="anthropic/claude-opus-4-7",
        commit_improvements=False,
    )

    assert result.bail_reason == "converged"
    assert improver_calls["n"] == 1  # only the iter-0 → iter-1 transition
    assert result.committed is True
    # On-disk SKILL.md now contains the revision.
    skill_md = (bundle / "skills" / "demo-bucket" / "SKILL.md").read_text()
    assert "Watchmen revision iter 1" in skill_md


def test_iterative_hard_stop_preserves_original_skill_on_max_iters(
    tmp_path, monkeypatch,
):
    """Three iters, no cheap model ever clears.  Hard stop: bundle
    SKILL.md is preserved."""
    from watchmen import route_improve
    from watchmen.route import HarnessReference, RouteConfig

    refs = [
        HarnessReference(
            harness="claude_code", current_model="anthropic/claude-opus-4-7",
            last_session_ts=_now_iso(0), session_count_window=10,
        ),
    ]
    bundle = _wire_route_improve(tmp_path, monkeypatch, harnesses_to_seed=refs)

    # Same low scores every iter.
    failing = [
        ("anthropic/claude-opus-4-7", 0.90, "reference"),
        ("anthropic/claude-sonnet-4-6", 0.50, "pending"),
        ("anthropic/claude-haiku-4-5", 0.40, "pending"),
    ]
    fake = _FakeCompare(scripts={i: {"claude_code": failing} for i in range(3)})
    monkeypatch.setattr(route_improve, "run_compare", fake)
    monkeypatch.setattr(
        route_improve, "improve_skill_with_watchmen",
        lambda **kw: (kw["evidence"].skill_md + "\n# revised\n", 0.01),
    )

    cfg = RouteConfig(project_key="p", bucket="demo-bucket")
    result = route_improve.run_route_iterative(
        cfg, threshold_absolute=None, threshold_offset=-0.05,
        max_iters=3, max_cost_usd=None,
        improver_model="anthropic/claude-opus-4-7",
        commit_improvements=False,
    )

    assert result.bail_reason == "max_iters"
    assert result.committed is False
    skill_md = (bundle / "skills" / "demo-bucket" / "SKILL.md").read_text()
    assert "# Original SKILL.md" in skill_md
    assert "# revised" not in skill_md
    # Decision per harness falls back to "stay".
    assert result.final_decisions[0].label == "stay"


def test_iterative_commit_improvements_overrides_hard_stop(tmp_path, monkeypatch):
    """Same scenario as hard-stop test, but the user passed
    --commit-improvements so the revised skill is committed even though
    no cheap model converged."""
    from watchmen import route_improve
    from watchmen.route import HarnessReference, RouteConfig

    refs = [
        HarnessReference(
            harness="claude_code", current_model="anthropic/claude-opus-4-7",
            last_session_ts=_now_iso(0), session_count_window=10,
        ),
    ]
    bundle = _wire_route_improve(tmp_path, monkeypatch, harnesses_to_seed=refs)

    fake = _FakeCompare(scripts={
        i: {"claude_code": [
            ("anthropic/claude-opus-4-7", 0.90, "reference"),
            ("anthropic/claude-sonnet-4-6", 0.50, "pending"),
        ]}
        for i in range(3)
    })
    monkeypatch.setattr(route_improve, "run_compare", fake)
    monkeypatch.setattr(
        route_improve, "improve_skill_with_watchmen",
        lambda **kw: (kw["evidence"].skill_md + "\n# rev\n", 0.01),
    )

    cfg = RouteConfig(project_key="p", bucket="demo-bucket")
    result = route_improve.run_route_iterative(
        cfg, threshold_absolute=None, threshold_offset=-0.05,
        max_iters=3, max_cost_usd=None,
        improver_model="anthropic/claude-opus-4-7",
        commit_improvements=True,  # user opt-in
    )

    assert result.bail_reason == "max_iters"
    assert result.committed is True
    skill_md = (bundle / "skills" / "demo-bucket" / "SKILL.md").read_text()
    assert "# rev" in skill_md


def test_iterative_max_cost_bails_mid_loop(tmp_path, monkeypatch):
    """Each iter costs $1 (we pin in the fake).  Set --max-cost-usd 1.5
    → the loop bails after iter 0 before improving further."""
    from watchmen import route_improve
    from watchmen.route import HarnessReference, RouteConfig

    refs = [
        HarnessReference(
            harness="claude_code", current_model="anthropic/claude-opus-4-7",
            last_session_ts=_now_iso(0), session_count_window=10,
        ),
    ]
    _wire_route_improve(tmp_path, monkeypatch, harnesses_to_seed=refs)

    # Generations carry $1 each so cost adds up to $2 per iter (ref + 1 cand).
    class _ExpensiveFake(_FakeCompare):
        def __call__(self, config, *, run_id=None, progress=None, evidence=None):
            r = super().__call__(config, run_id=run_id, progress=progress,
                                  evidence=evidence)
            from dataclasses import replace
            r = type(r)(
                run_id=r.run_id, run_dir=r.run_dir, config=r.config,
                tasks=r.tasks,
                generations=[replace(g, cost_usd=1.0) for g in r.generations],
                scores=r.scores, task_results=r.task_results,
                summaries=r.summaries,
            )
            return r

    fake = _ExpensiveFake(scripts={
        i: {"claude_code": [
            ("anthropic/claude-opus-4-7", 0.90, "reference"),
            ("anthropic/claude-sonnet-4-6", 0.60, "pending"),
        ]}
        for i in range(3)
    })
    monkeypatch.setattr(route_improve, "run_compare", fake)
    monkeypatch.setattr(
        route_improve, "improve_skill_with_watchmen",
        lambda **kw: (kw["evidence"].skill_md, 0.0),
    )

    cfg = RouteConfig(project_key="p", bucket="demo-bucket")
    result = route_improve.run_route_iterative(
        cfg, threshold_absolute=None, threshold_offset=-0.05,
        max_iters=5, max_cost_usd=1.5,  # ceiling between iter-0 and iter-1 spend
        improver_model="anthropic/claude-opus-4-7",
        commit_improvements=False,
    )

    assert result.bail_reason == "max_cost"
    # Should have stopped after the first iter that crossed the ceiling.
    assert len(result.iterations) <= 2
    assert result.total_cost_usd >= 1.5



def test_iterative_marks_no_data_when_reference_judge_score_is_unreliable(
    tmp_path, monkeypatch,
):
    """If the reference's judge score is below REFERENCE_RELIABLE_FLOOR
    (0.30), the whole compare is unanchored.  Don't try to route — emit
    `no-data` and leave the user's SKILL.md alone.

    Regression: real smoke run showed an OpenRouter model id alias
    that returned content but the judge gave it 0.0; that made the
    auto-threshold collapse to 0 and any candidate trivially "passed".
    """
    from watchmen import route_improve
    from watchmen.route import HarnessReference, RouteConfig

    refs = [
        HarnessReference(
            harness="claude_code",
            current_model="anthropic/claude-sonnet-4-mystery-alias",
            last_session_ts=_now_iso(0), session_count_window=2,
        ),
    ]
    _wire_route_improve(tmp_path, monkeypatch, harnesses_to_seed=refs)

    # Reference scores 0.0; candidates score relatively high.  Without
    # the gate, the loop would converge on a haiku "downshift" and the
    # user would get a misleading recommendation.
    fake = _FakeCompare(
        scripts={
            0: {
                "claude_code": [
                    ("anthropic/claude-sonnet-4-mystery-alias", 0.0, "reference"),
                    ("anthropic/claude-sonnet-4-6", 0.92, "pending"),
                    ("anthropic/claude-haiku-4-5", 0.85, "pending"),
                ],
            },
        }
    )
    monkeypatch.setattr(route_improve, "run_compare", fake)
    monkeypatch.setattr(
        route_improve, "improve_skill_with_watchmen",
        lambda **kw: (kw["evidence"].skill_md, 0.0),
    )

    cfg = RouteConfig(project_key="p", bucket="demo-bucket")
    result = route_improve.run_route_iterative(
        cfg, threshold_absolute=None, threshold_offset=-0.05,
        max_iters=3, max_cost_usd=None,
        improver_model="anthropic/claude-opus-4-7",
        commit_improvements=False,
    )

    d = result.final_decisions[0]
    assert d.label == "no-data"
    assert d.recommended_model is None
    # Stops iterating immediately — no point trying to revise the skill
    # when we can't even score the reference reliably.
    assert len(result.iterations) == 1


def test_threshold_floors_at_min_auto_threshold_with_low_reference():
    """Reference at 0.4 + offset -0.05 would naturally land at 0.35 —
    too noisy to anchor a route.  Floor kicks the auto-threshold up to
    MIN_AUTO_THRESHOLD (0.5)."""
    from watchmen.route_improve import (
        MIN_AUTO_THRESHOLD,
        threshold_for_reference,
    )
    assert threshold_for_reference(0.40, absolute=None, offset=-0.05) == MIN_AUTO_THRESHOLD



def test_reference_unroutable_when_all_generations_errored():
    """If every reference generation has `error` set, the model id was
    never routable.  Detected without going through the judge — the
    judge wouldn't see an output anyway."""
    from watchmen.compare import (
        CompareConfig, CompareResult, GenerationRecord,
    )
    from watchmen.route_improve import _reference_unroutable

    errs = [
        GenerationRecord(
            run_id="r", task_id="t1",
            output_id=f"t1-out-{i:03d}",
            model="anthropic/claude-sonnet-4-20250514",
            role="reference", sample_index=1,
            output="GENERATION_ERROR: NotFoundError: 404", usage={},
            cost_usd=0.0, latency_s=0.5,
            error="NotFoundError: 404",
        )
        for i in range(3)
    ]
    result = CompareResult(
        run_id="r", run_dir="/tmp",
        config=CompareConfig(project_key="p", bucket="b",
                             reference_model="ref", candidates=[]),
        tasks=[], generations=errs, scores=[], task_results=[],
        summaries=[],
    )
    unroutable, reason = _reference_unroutable(result)
    assert unroutable is True
    assert "errored" in reason


def test_reference_unroutable_when_all_generations_zero_cost_zero_tokens():
    """OpenRouter sometimes routes a stale model id, returns silence
    (zero tokens, zero cost, no explicit error).  Still unroutable in
    any meaningful sense — surface it the same way as an explicit error."""
    from watchmen.compare import (
        CompareConfig, CompareResult, GenerationRecord,
    )
    from watchmen.route_improve import _reference_unroutable

    silent = [
        GenerationRecord(
            run_id="r", task_id="t1",
            output_id=f"t1-out-{i:03d}",
            model="anthropic/claude-sonnet-4-20250514",
            role="reference", sample_index=1,
            output="",
            usage={"completion_tokens": 0},
            cost_usd=0.0, latency_s=1.2,
        )
        for i in range(3)
    ]
    result = CompareResult(
        run_id="r", run_dir="/tmp",
        config=CompareConfig(project_key="p", bucket="b",
                             reference_model="ref", candidates=[]),
        tasks=[], generations=silent, scores=[], task_results=[],
        summaries=[],
    )
    unroutable, reason = _reference_unroutable(result)
    assert unroutable is True
    assert "empty" in reason or "zero" in reason


def test_iterative_bails_early_when_every_harness_is_no_data(tmp_path, monkeypatch):
    """No candidates + unroutable references = nothing the improver
    can fix.  Loop should exit immediately with bail_reason='unroutable'
    instead of running 3 useless improver calls."""
    from watchmen import route_improve
    from watchmen.route import HarnessReference, RouteConfig

    refs = [
        HarnessReference(
            harness="claude_code",
            current_model="anthropic/claude-sonnet-4-20250514",
            last_session_ts=_now_iso(0), session_count_window=2,
        ),
    ]
    _wire_route_improve(tmp_path, monkeypatch, harnesses_to_seed=refs)

    # Reference scores 0; no candidates either.
    fake = _FakeCompare(
        scripts={
            0: {
                "claude_code": [
                    ("anthropic/claude-sonnet-4-20250514", 0.0, "reference"),
                ],
            },
        }
    )
    monkeypatch.setattr(route_improve, "run_compare", fake)

    # Improver should never be called; bomb if it is.
    def boom(**kw):
        raise AssertionError("improver should not run when every harness is no-data")
    monkeypatch.setattr(route_improve, "improve_skill_with_watchmen", boom)

    cfg = RouteConfig(project_key="p", bucket="demo-bucket")
    result = route_improve.run_route_iterative(
        cfg, threshold_absolute=None, threshold_offset=-0.05,
        max_iters=3, max_cost_usd=None,
        improver_model="anthropic/claude-opus-4-7",
        commit_improvements=False,
    )

    assert result.bail_reason == "unroutable"
    assert len(result.iterations) == 1
    assert result.final_decisions[0].label == "no-data"


def test_reference_unroutable_reports_rate_limit_when_all_errors_are_429():
    """When the reference fails because the OAuth quota is exhausted (all
    generations 429 with `error_kind='rate_limit'`), surface that explicitly
    so the user knows to wait or set ANTHROPIC_API_KEY rather than chasing
    a phantom model-id bug."""
    from watchmen.compare import (
        CompareConfig, CompareResult, GenerationRecord,
    )
    from watchmen.route_improve import _reference_unroutable

    errs = [
        GenerationRecord(
            run_id="r", task_id="t1",
            output_id=f"t1-out-{i:03d}",
            model="claude-opus-4-7",
            role="reference", sample_index=1,
            output="GENERATION_ERROR: HTTPStatusError: 429 Client Error",
            usage={},
            cost_usd=0.0, latency_s=0.3,
            error="HTTPStatusError: 429 rate_limit_error",
            error_kind="rate_limit",
        )
        for i in range(3)
    ]
    result = CompareResult(
        run_id="r", run_dir="/tmp",
        config=CompareConfig(project_key="p", bucket="b",
                             reference_model="claude-opus-4-7", candidates=[]),
        tasks=[], generations=errs, scores=[], task_results=[],
        summaries=[],
    )
    unroutable, reason = _reference_unroutable(result)
    assert unroutable is True
    assert "rate-limited" in reason
    assert "quota" in reason.lower()


def test_reference_unroutable_reports_auth_when_all_errors_are_401():
    """All-401 references — credential is broken, not the model. The
    error message should send the user to `claude login`."""
    from watchmen.compare import (
        CompareConfig, CompareResult, GenerationRecord,
    )
    from watchmen.route_improve import _reference_unroutable

    errs = [
        GenerationRecord(
            run_id="r", task_id="t1",
            output_id=f"t1-out-{i:03d}",
            model="claude-opus-4-7",
            role="reference", sample_index=1,
            output="GENERATION_ERROR: HTTPStatusError: 401",
            usage={},
            cost_usd=0.0, latency_s=0.2,
            error="HTTPStatusError: 401 Unauthorized",
            error_kind="auth",
        )
        for i in range(3)
    ]
    result = CompareResult(
        run_id="r", run_dir="/tmp",
        config=CompareConfig(project_key="p", bucket="b",
                             reference_model="claude-opus-4-7", candidates=[]),
        tasks=[], generations=errs, scores=[], task_results=[],
        summaries=[],
    )
    unroutable, reason = _reference_unroutable(result)
    assert unroutable is True
    assert "auth" in reason.lower()
    assert "login" in reason


def test_classify_route_and_pick_cheapest_passing_agree_on_same_result():
    """Regression for divergence found in PR #85 review: one-shot
    classify_route (picked highest-quality) and iterative
    _pick_cheapest_passing (picked cheapest passer) could produce
    different recommendations for the same compare result. They must
    agree — both implement sweep semantics now.

    Concrete scenario: candidates A (0.91, 1.5× cost) and B (0.86, 0.7×
    cost), ref=0.90, threshold=0.85, both pass. Before the unification:
      - classify_route picked A → "stay" (not better, not cheaper)
      - _pick_cheapest_passing picked B → "downshift" (0.7× cost)
    After unification: both pick B and recommend downshift.
    """
    from watchmen.compare import (
        CompareConfig, CompareResult, ModelSummary,
    )
    from watchmen.route import (
        HarnessReference, RouteDecision, classify_route,
    )
    from watchmen.route_improve import _pick_cheapest_passing

    ref_row = ModelSummary(
        model="ref-model", role="reference", avg_score=0.90,
        worst_score=0.90, wins_vs_reference=None, task_count=1,
        cost_usd=1.0, cost_vs_reference=None,
        produced_tokens=1000, produced_tokens_vs_reference=None,
        visible_chars=5000, empty_outputs=0, maxed_outputs=0,
        sample_count=1, latency_s=20.0, latency_vs_reference=None,
        decision="reference", decision_note="",
    )
    pricey_quality = ModelSummary(
        model="cand-A", role="candidate", avg_score=0.91,
        worst_score=0.91, wins_vs_reference=0, task_count=1,
        cost_usd=1.5, cost_vs_reference=1.5,
        produced_tokens=1200, produced_tokens_vs_reference=1.2,
        visible_chars=6000, empty_outputs=0, maxed_outputs=0,
        sample_count=1, latency_s=25.0, latency_vs_reference=1.25,
        decision="comparable", decision_note="",
    )
    cheaper_passer = ModelSummary(
        model="cand-B", role="candidate", avg_score=0.86,
        worst_score=0.86, wins_vs_reference=0, task_count=1,
        cost_usd=0.7, cost_vs_reference=0.7,
        produced_tokens=800, produced_tokens_vs_reference=0.8,
        visible_chars=4000, empty_outputs=0, maxed_outputs=0,
        sample_count=1, latency_s=15.0, latency_vs_reference=0.75,
        decision="comparable", decision_note="",
    )
    cmp_result = CompareResult(
        run_id="r", run_dir="/tmp",
        config=CompareConfig(
            project_key="p", bucket="b", reference_model="ref-model",
            candidates=["cand-A", "cand-B"],
        ),
        tasks=[], generations=[], scores=[], task_results=[],
        summaries=[ref_row, pricey_quality, cheaper_passer],
    )
    harness_ref = HarnessReference(
        harness="claude_code", current_model="ref-model",
        last_session_ts="2026-01-01T00:00:00Z", session_count_window=10,
    )

    one_shot = classify_route(harness_ref, cmp_result, [harness_ref])
    seed_decision = RouteDecision(
        harness="claude_code", current_model="ref-model",
        recommended_model=None, label="stay", note="",
        avg_score=0.90, cost_vs_current=None,
    )
    iterative = _pick_cheapest_passing(seed_decision, cmp_result, threshold=0.85)

    # Both paths must converge on the same recommendation.
    assert one_shot.recommended_model == iterative.recommended_model, (
        f"one-shot recommended {one_shot.recommended_model!r} but "
        f"iterative recommended {iterative.recommended_model!r}"
    )
    assert one_shot.label == iterative.label, (
        f"one-shot label {one_shot.label!r} but iterative {iterative.label!r}"
    )
    # Sanity: the cheaper candidate IS the right answer (Pareto-good).
    assert one_shot.recommended_model == "cand-B"
    assert one_shot.label == "downshift"


def test_claude_pro_resolve_api_key_raises_when_token_expired(monkeypatch):
    """OAuth expiry race: a long iterative route run can cross the token
    boundary mid-loop. `resolve_api_key` must fail fast with a clear
    error rather than handing out an expired token that 401s opaquely
    from `/v1/messages`."""
    import pytest
    from watchmen.providers import ClaudePro
    from watchmen.credentials import ClaudeCodeCredentials

    expired = ClaudeCodeCredentials(
        access_token="expired-token",
        refresh_token="some-refresh",
        expires_at_ms=1_000,  # ancient
        scopes=("user:inference",),
        subscription_type="team",
        rate_limit_tier="default_claude_max_5x",
    )
    monkeypatch.setattr(
        "watchmen.credentials.ClaudeCodeCredentials.read",
        classmethod(lambda cls: expired),
    )

    provider = ClaudePro()
    with pytest.raises(RuntimeError, match="OAuth token expired"):
        provider.resolve_api_key(configured=None)


def test_claude_pro_resolve_api_key_returns_token_when_valid(monkeypatch):
    """Sanity check: valid (non-expired) credential returns the token
    unchanged, no error."""
    import time
    from watchmen.providers import ClaudePro
    from watchmen.credentials import ClaudeCodeCredentials

    fresh = ClaudeCodeCredentials(
        access_token="fresh-token",
        refresh_token="some-refresh",
        expires_at_ms=int(time.time() * 1000) + 60 * 60 * 1000,  # +1h
        scopes=("user:inference",),
        subscription_type="team",
        rate_limit_tier="default_claude_max_5x",
    )
    monkeypatch.setattr(
        "watchmen.credentials.ClaudeCodeCredentials.read",
        classmethod(lambda cls: fresh),
    )

    assert ClaudePro().resolve_api_key(configured=None) == "fresh-token"


def test_pick_cheapest_passing_recommends_stay_when_winner_worse_and_costlier():
    """When the only passing candidate is both lower-quality AND not
    cheaper than the reference, route should recommend `stay`, not
    flip to `upshift` for a clearly losing swap. Regression for the
    smoke that surfaced gpt-5.2 (0.860, 1.19x cost) being recommended
    over gpt-5.5 (0.900) — strictly worse on both axes."""
    from watchmen.compare import (
        CompareConfig, CompareResult, ModelSummary,
    )
    from watchmen.route import RouteDecision
    from watchmen.route_improve import _pick_cheapest_passing

    ref = ModelSummary(
        model="gpt-5.5", role="reference", avg_score=0.900,
        worst_score=0.900, wins_vs_reference=None, task_count=1,
        cost_usd=0.030, cost_vs_reference=None,
        produced_tokens=2270, produced_tokens_vs_reference=None,
        visible_chars=10000, empty_outputs=0, maxed_outputs=0,
        sample_count=1, latency_s=42.0, latency_vs_reference=None,
        decision="reference", decision_note="",
    )
    worse_pricier = ModelSummary(
        model="gpt-5.2", role="candidate", avg_score=0.860,
        worst_score=0.860, wins_vs_reference=0, task_count=1,
        cost_usd=0.036, cost_vs_reference=1.19,  # both worse AND pricier
        produced_tokens=2030, produced_tokens_vs_reference=0.89,
        visible_chars=8939, empty_outputs=0, maxed_outputs=0,
        sample_count=1, latency_s=39.5, latency_vs_reference=0.93,
        decision="comparable", decision_note="",
    )
    cmp_result = CompareResult(
        run_id="r", run_dir="/tmp",
        config=CompareConfig(
            project_key="p", bucket="b", reference_model="gpt-5.5",
            candidates=["gpt-5.2"],
        ),
        tasks=[], generations=[], scores=[], task_results=[],
        summaries=[ref, worse_pricier],
    )
    seed_decision = RouteDecision(
        harness="codex", current_model="gpt-5.5", recommended_model=None,
        label="stay", note="", avg_score=0.900, cost_vs_current=None,
    )
    # Threshold 0.85 = ref - 0.05 (default offset). Candidate scores 0.86 so passes.
    out = _pick_cheapest_passing(seed_decision, cmp_result, threshold=0.85)

    assert out.label == "stay", f"expected stay, got {out.label}: {out.note}"
    assert out.recommended_model is None
    assert "not worth a swap" in out.note


def test_pick_cheapest_passing_still_recommends_downshift_when_cheaper():
    """Sanity check: when a passer IS cheaper than the reference and
    roughly the same quality, downshift should still fire."""
    from watchmen.compare import (
        CompareConfig, CompareResult, ModelSummary,
    )
    from watchmen.route import RouteDecision
    from watchmen.route_improve import _pick_cheapest_passing

    ref = ModelSummary(
        model="gpt-5.5", role="reference", avg_score=0.900,
        worst_score=0.900, wins_vs_reference=None, task_count=1,
        cost_usd=0.030, cost_vs_reference=None,
        produced_tokens=2270, produced_tokens_vs_reference=None,
        visible_chars=10000, empty_outputs=0, maxed_outputs=0,
        sample_count=1, latency_s=42.0, latency_vs_reference=None,
        decision="reference", decision_note="",
    )
    cheaper_passer = ModelSummary(
        model="gpt-5.4-mini", role="candidate", avg_score=0.880,
        worst_score=0.880, wins_vs_reference=0, task_count=1,
        cost_usd=0.020, cost_vs_reference=0.67,  # 33% cheaper
        produced_tokens=1273, produced_tokens_vs_reference=0.56,
        visible_chars=5884, empty_outputs=0, maxed_outputs=0,
        sample_count=1, latency_s=13.4, latency_vs_reference=0.32,
        decision="comparable", decision_note="",
    )
    cmp_result = CompareResult(
        run_id="r", run_dir="/tmp",
        config=CompareConfig(
            project_key="p", bucket="b", reference_model="gpt-5.5",
            candidates=["gpt-5.4-mini"],
        ),
        tasks=[], generations=[], scores=[], task_results=[],
        summaries=[ref, cheaper_passer],
    )
    seed_decision = RouteDecision(
        harness="codex", current_model="gpt-5.5", recommended_model=None,
        label="stay", note="", avg_score=0.900, cost_vs_current=None,
    )
    out = _pick_cheapest_passing(seed_decision, cmp_result, threshold=0.85)

    # Score is within 0.02 of ref (0.880 vs 0.900 → diff 0.02, borderline)
    # but cost is meaningfully cheaper (0.67) so downshift wins.
    assert out.label == "downshift", f"expected downshift, got {out.label}: {out.note}"
    assert out.recommended_model == "gpt-5.4-mini"


def test_default_judge_falls_back_to_reference_model_when_no_override():
    """The judge defaults to the reference (modal) model — same one the
    user runs day-to-day, so the judge stays inside the quota they already
    pay for. Hardcoded opus/gpt-5.5 defaults were burning quota the user
    might not have, especially on subscription tiers."""
    from watchmen.route_improve import _default_judge_for_provider

    # claude-pro: reference passed bare → judge bare (OAuth provider).
    assert _default_judge_for_provider(
        "claude-pro", reference_model="claude-haiku-4-5-20251001",
    ) == "claude-haiku-4-5-20251001"

    # claude-pro: reference passed namespaced → namespace stripped (OAuth
    # provider takes bare ids).
    assert _default_judge_for_provider(
        "claude-pro", reference_model="anthropic/claude-haiku-4-5-20251001",
    ) == "claude-haiku-4-5-20251001"

    # chatgpt: reference is a bare gpt slug → stays bare.
    assert _default_judge_for_provider(
        "chatgpt", reference_model="gpt-5.5",
    ) == "gpt-5.5"

    # openrouter: reference stays namespaced (OR needs namespaced form).
    assert _default_judge_for_provider(
        "openrouter", reference_model="anthropic/claude-opus-4-7",
    ) == "anthropic/claude-opus-4-7"


def test_default_judge_respects_explicit_user_override():
    """When --judge is set (override is non-empty), the user's choice
    wins regardless of what the reference model is."""
    from watchmen.route_improve import _default_judge_for_provider

    # Override beats reference.
    assert _default_judge_for_provider(
        "claude-pro",
        reference_model="claude-haiku-4-5-20251001",
        override="claude-opus-4-7",
    ) == "claude-opus-4-7"

    # Empty / whitespace override → falls back to reference.
    assert _default_judge_for_provider(
        "claude-pro",
        reference_model="claude-haiku-4-5-20251001",
        override="",
    ) == "claude-haiku-4-5-20251001"
    assert _default_judge_for_provider(
        "claude-pro",
        reference_model="claude-haiku-4-5-20251001",
        override="   ",
    ) == "claude-haiku-4-5-20251001"


def test_classify_call_exception_buckets_known_http_codes():
    """The compare error classifier should recognize 429 / 401 / 403 /
    400-with-model from httpx.HTTPStatusError instances directly, and
    fall back to string matching for wrapped exceptions."""
    import httpx
    from watchmen.compare import _classify_call_exception

    def _err(status: int, body: str) -> httpx.HTTPStatusError:
        req = httpx.Request("POST", "https://api.example.com/v1/messages")
        resp = httpx.Response(status, content=body.encode(), request=req)
        return httpx.HTTPStatusError(f"Client error '{status}'", request=req, response=resp)

    assert _classify_call_exception(_err(429, "rate_limit_error")) == "rate_limit"
    assert _classify_call_exception(_err(401, "auth")) == "auth"
    assert _classify_call_exception(_err(403, "forbidden")) == "auth"
    # Strict 400 → "unknown_model" only on unambiguous tokens.
    assert _classify_call_exception(_err(400, '{"error":{"message":"unknown model"}}')) == "unknown_model"
    assert _classify_call_exception(_err(400, '{"error":{"type":"not_found_error"}}')) == "unknown_model"
    assert _classify_call_exception(_err(400, '{"error":{"message":"model_not_found"}}')) == "unknown_model"
    # A generic 400 (e.g., the quota-driven kind opus returns) must NOT
    # be flagged as unknown_model just because the URL or generic prose
    # contains the word "model". This was the false-positive that the
    # earlier (loose) heuristic produced.
    assert _classify_call_exception(_err(400, '{"error":{"type":"invalid_request_error","message":"max_tokens too high"}}')) == "other"
    assert _classify_call_exception(_err(500, "server error")) == "other"

    # String fallback for non-HTTPStatusError exceptions
    class _Wrapped(Exception): pass
    assert _classify_call_exception(_Wrapped("rate_limit_error from provider")) == "rate_limit"
    assert _classify_call_exception(_Wrapped("authentication_error")) == "auth"
    assert _classify_call_exception(_Wrapped("model_not_found")) == "unknown_model"
    assert _classify_call_exception(_Wrapped("timeout")) == "other"
