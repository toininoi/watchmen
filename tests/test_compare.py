"""Tests for watchmen compare."""

from __future__ import annotations

import json
from pathlib import Path


def _write_skill(base: Path, slug: str) -> None:
    skill_dir = base / "skills" / slug
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: demo skill
description: Demo skill for model comparison
when_to_use:
  - "User asks for a demo workflow"
when_not_to_use:
  - "Unrelated requests"
---

# Demo Skill

Run the repo-specific demo workflow carefully.
""",
        encoding="utf-8",
    )


def test_compare_loads_skill_bucket_evidence(tmp_path, monkeypatch):
    from watchmen import compare

    bundle = tmp_path / "bundles" / "demo"
    _write_skill(bundle, "demo-skill")
    (bundle / "CLAUDE.md").write_text("# Demo repo\n\nUse uv.", encoding="utf-8")
    (bundle / "_candidates.json").write_text(
        json.dumps([
            {
                "slug": "demo-skill",
                "name": "Demo Skill",
                "description": "A candidate",
                "source_files": ["src/demo.py"],
            }
        ]),
        encoding="utf-8",
    )
    monkeypatch.setattr(compare, "bundle_dir", lambda project: tmp_path / "bundles" / project)

    evidence = compare.load_skill_bucket_evidence("demo", "demo-skill")

    assert evidence.project_key == "demo"
    assert evidence.bucket == "demo-skill"
    assert "Demo Skill" in evidence.skill_md
    assert evidence.candidate is not None
    assert evidence.candidate["source_files"] == ["src/demo.py"]
    assert "CLAUDE.md" in evidence.workspace_brief


def test_compare_cost_prefers_provider_reported_usage():
    from watchmen import compare

    usage = {
        "prompt_tokens": 7002,
        "completion_tokens": 3843,
        "cost": 0.0,
        "cost_details": {
            "upstream_inference_cost": 0.00205632,
            "upstream_inference_prompt_cost": 0.00098028,
            "upstream_inference_completions_cost": 0.00107604,
        },
    }

    assert compare._cost_for_usage("deepseek/deepseek-v4-flash", usage) == 0.00205632


def test_compare_run_selects_best_of_n_and_writes_artifacts(tmp_path, monkeypatch):
    from watchmen import compare

    bundle = tmp_path / "bundles" / "demo"
    _write_skill(bundle, "demo-skill")
    monkeypatch.setattr(compare, "bundle_dir", lambda project: tmp_path / "bundles" / project)

    outputs: list[tuple[str, str, int]] = []

    def fake_generate(client, config, evidence, task, *, run_id, model, role, sample_index, output_index):
        output_id = f"{task.id}-out-{output_index:03d}"
        outputs.append((model, role, sample_index))
        score_hint = {
            ("opus", 1): "ref",
            ("candidate-a", 1): "weak",
            ("candidate-a", 2): "strong",
            ("candidate-b", 1): "okay",
            ("candidate-b", 2): "bad",
        }[(model, sample_index)]
        return compare.GenerationRecord(
            run_id=run_id,
            task_id=task.id,
            output_id=output_id,
            model=model,
            role=role,
            sample_index=sample_index,
            output=f"---\nname: {score_hint}\n---\n# {score_hint}",
            usage={"prompt_tokens": 10, "completion_tokens": 5},
            cost_usd={"opus": 1.0, "candidate-a": 0.1, "candidate-b": 0.05}[model],
            latency_s={"opus": 10.0, "candidate-a": 2.0, "candidate-b": 1.0}[model],
        )

    def fake_judge(client, config, evidence, task, task_outputs, *, run_id, run_dir=None):
        scores = {
            "ref": 0.90,
            "weak": 0.50,
            "strong": 0.93,
            "okay": 0.82,
            "bad": 0.20,
        }
        rows = []
        for rec in task_outputs:
            label = rec.output.split("# ", 1)[1]
            rows.append(
                compare.ScoreRecord(
                    task_id=task.id,
                    output_id=rec.output_id,
                    score=scores[label],
                    schema=5,
                    trigger_quality=5,
                    procedure_quality=5,
                    evidence_grounding=5,
                    context_efficiency=5,
                    rationale=label,
                )
            )
        return rows

    monkeypatch.setattr(compare, "_generate_one", fake_generate)
    monkeypatch.setattr(compare, "_judge_task_outputs", fake_judge)

    config = compare.CompareConfig(
        project_key="demo",
        bucket="demo-skill",
        reference_model="opus",
        candidates=["candidate-a", "candidate-b"],
        task_count=1,
        reference_n=1,
        candidate_n=2,
        generation_concurrency=2,
    )
    progress: list[str] = []
    result = compare.run_compare(config, run_id="test-run", progress=progress.append)

    assert len(outputs) == 5
    assert "task 1/1: generating 5 outputs (concurrency=2)" in progress
    assert "task 1/1: queued gen 1/5: reference opus sample 1/1" in progress
    assert "task 1/1: queued gen 3/5: candidate candidate-a sample 2/2" in progress
    assert "task 1/1: queued gen 5/5: candidate candidate-b sample 2/2" in progress
    assert any(
        msg.startswith("task 1/1: done gen 3/5: candidate candidate-a sample 2/2")
        for msg in progress
    )
    assert "task 1/1: judging 5 outputs with openai/gpt-5.5" in progress
    assert (bundle / "_compare" / "test-run" / "config.json").exists()
    assert (bundle / "_compare" / "test-run" / "generations.jsonl").exists()
    assert (bundle / "_compare" / "test-run" / "judgments.jsonl").exists()
    report = (bundle / "_compare" / "test-run" / "report.md")
    assert report.exists()
    report_text = report.read_text(encoding="utf-8")
    assert "comp tok" in report_text

    by_model = {row.model: row for row in result.summaries}
    assert by_model["opus"].decision == "reference"
    assert by_model["candidate-a"].avg_score == 0.93
    assert by_model["candidate-a"].wins_vs_reference == 1
    assert by_model["candidate-a"].cost_vs_reference == 0.2
    assert by_model["candidate-a"].produced_tokens == 10
    assert by_model["candidate-a"].produced_tokens_vs_reference == 2.0
    assert by_model["candidate-a"].decision == "replace reference"
    assert by_model["candidate-b"].decision == "cheap tradeoff"


def test_compare_reference_can_dominate_candidate():
    from watchmen import compare

    config = compare.CompareConfig(
        project_key="demo",
        bucket="demo-skill",
        reference_model="opus",
        candidates=["candidate"],
    )
    summaries = compare._summarize_models(
        config,
        [
            compare.ModelTaskResult(
                task_id="task-1",
                model="opus",
                role="reference",
                best_output_id="task-1-out-001",
                best_score=0.95,
                sample_count=1,
                cost_usd=0.30,
            produced_tokens=100,
            visible_chars=100,
            latency_s=90.0,
            ),
            compare.ModelTaskResult(
                task_id="task-1",
                model="candidate",
                role="candidate",
                best_output_id="task-1-out-002",
                best_score=0.88,
                sample_count=3,
                cost_usd=0.50,
            produced_tokens=250,
            visible_chars=250,
            latency_s=120.0,
            ),
        ],
    )

    by_model = {row.model: row for row in summaries}
    assert by_model["candidate"].decision == "dominated"


def test_compare_empty_outputs_are_invalid_not_cheap():
    from watchmen import compare

    config = compare.CompareConfig(
        project_key="demo",
        bucket="demo-skill",
        reference_model="opus",
        candidates=["empty-cheap"],
        max_tokens=2600,
    )
    summaries = compare._summarize_models(
        config,
        [
            compare.ModelTaskResult(
                task_id="task-1",
                model="opus",
                role="reference",
                best_output_id="task-1-out-001",
                best_score=0.88,
                sample_count=1,
                cost_usd=0.10,
                produced_tokens=2400,
                latency_s=40.0,
                visible_chars=5000,
                empty_outputs=0,
                maxed_outputs=0,
            ),
            compare.ModelTaskResult(
                task_id="task-1",
                model="empty-cheap",
                role="candidate",
                best_output_id="task-1-out-002",
                best_score=0.0,
                sample_count=1,
                cost_usd=0.04,
                produced_tokens=2600,
                latency_s=57.0,
                visible_chars=0,
                empty_outputs=1,
                maxed_outputs=1,
            ),
        ],
    )

    by_model = {row.model: row for row in summaries}
    assert by_model["empty-cheap"].decision == "invalid output"
    assert by_model["empty-cheap"].decision_note == "all 1 samples empty"


def test_compare_judge_retries_invalid_json_and_scores(tmp_path, monkeypatch):
    from watchmen import compare

    evidence = compare.SkillBucketEvidence(
        project_key="demo",
        bucket="demo-skill",
        skill_md="---\nname: demo\n---\n# Demo",
        candidate=None,
        workspace_brief="brief",
        curation_log="log",
    )
    task = compare.CompareTask("task-1", "Judge retry", "Score outputs")
    outputs = [
        compare.GenerationRecord(
            run_id="run",
            task_id="task-1",
            output_id="task-1-out-001",
            model="candidate",
            role="candidate",
            sample_index=1,
            output="---\nname: output\n---\n# Output",
            usage={},
            cost_usd=0.0,
            latency_s=0.0,
        )
    ]
    calls = 0

    def fake_call_model(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return "not json", {}
        return (
            '{"scores":[{"id":"task-1-out-001","score":0.87,"schema":5,'
            '"trigger_quality":4,"procedure_quality":5,"evidence_grounding":4,'
            '"context_efficiency":4,"rationale":"good"}]}',
            {},
        )

    monkeypatch.setattr(compare, "_call_model", fake_call_model)

    scores = compare._judge_task_outputs(
        None,
        compare.CompareConfig(project_key="demo", bucket="demo-skill"),
        evidence,
        task,
        outputs,
        run_id="run",
        run_dir=tmp_path,
    )

    assert calls == 2
    assert scores[0].score == 0.87
    assert (tmp_path / "task-1.judge-attempt-1.txt").read_text() == "not json"


def test_compare_judge_fallback_keeps_run_alive(tmp_path, monkeypatch):
    from watchmen import compare

    evidence = compare.SkillBucketEvidence(
        project_key="demo",
        bucket="demo-skill",
        skill_md="---\nname: demo\n---\n# Demo",
        candidate=None,
        workspace_brief="brief",
        curation_log="log",
    )
    task = compare.CompareTask("task-1", "Judge fallback", "Score outputs")
    outputs = [
        compare.GenerationRecord(
            run_id="run",
            task_id="task-1",
            output_id="task-1-out-001",
            model="candidate",
            role="candidate",
            sample_index=1,
            output="---\nname: output\n---\n# Output",
            usage={},
            cost_usd=0.0,
            latency_s=0.0,
        )
    ]

    monkeypatch.setattr(compare, "_call_model", lambda *args, **kwargs: ("still not json", {}))

    scores = compare._judge_task_outputs(
        None,
        compare.CompareConfig(project_key="demo", bucket="demo-skill"),
        evidence,
        task,
        outputs,
        run_id="run",
        run_dir=tmp_path,
    )

    assert scores[0].score == 0.0
    assert "parseable JSON" in scores[0].rationale
    assert (tmp_path / "task-1.judge-attempt-3.txt").exists()


def test_compare_candidate_parser_auto_and_csv():
    from watchmen.commands.compare import _parse_candidates

    auto_candidates = _parse_candidates("auto")
    assert len(auto_candidates) == 7
    assert "tencent/hy3-preview" in auto_candidates
    assert "stepfun/step-3.5-flash" in auto_candidates
    assert "moonshotai/kimi-k2.6" in auto_candidates
    assert _parse_candidates("a,b, c ") == ["a", "b", "c"]
    assert _parse_candidates("a,b", ["b", "d"]) == ["a", "b", "d"]
    assert _parse_candidates("none", ["custom/model"]) == ["custom/model"]


def test_generate_one_routes_candidate_to_its_own_provider(monkeypatch):
    """A candidate listed in candidate_providers generates on that backend;
    the reference (not in the map) falls back to the run's provider. This is
    what lets a cross-harness run execute a foreign model for real instead of
    forcing it through a provider that can't serve it."""
    from watchmen import compare

    evidence = compare.SkillBucketEvidence(
        project_key="demo", bucket="demo-skill",
        skill_md="---\nname: demo\n---\n# Demo", candidate=None,
        workspace_brief="brief", curation_log="log",
    )
    task = compare.CompareTask("task-1", "Gen", "Make a SKILL.md")
    seen: dict[str, str] = {}

    def fake_call_model_data(client, *, provider, model, **kwargs):
        seen[model] = provider
        return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {}}

    monkeypatch.setattr(compare, "_call_model_data", fake_call_model_data)
    cfg = compare.CompareConfig(
        project_key="demo", bucket="demo-skill",
        reference_model="anthropic/claude-opus-4-7",
        candidates=["gpt-5.5"],
        provider="claude-pro",
        candidate_providers={"gpt-5.5": "chatgpt"},
    )

    compare._generate_one(None, cfg, evidence, task, run_id="r",
                          model="gpt-5.5", role="candidate",
                          sample_index=1, output_index=1)
    compare._generate_one(None, cfg, evidence, task, run_id="r",
                          model="anthropic/claude-opus-4-7", role="reference",
                          sample_index=1, output_index=2)

    assert seen["gpt-5.5"] == "chatgpt"                 # foreign candidate -> its backend
    assert seen["anthropic/claude-opus-4-7"] == "claude-pro"  # reference -> source backend
