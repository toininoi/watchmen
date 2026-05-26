"""Model comparison runs for skill-bucket generation.

``watchmen compare`` answers a local question, not a leaderboard question:
for a selected watchmen skill bucket, can cheaper OpenRouter models reproduce
or improve the skill output well enough to replace an expensive reference
model?

The first slice is intentionally narrow:
- bucket = one existing skill slug
- output = SKILL.md
- reference = one Opus generation by default
- candidates = best-of-N generations
- judge = GPT-5.5 through the same OpenRouter route

All raw generations and judgments are persisted under
``bundles/<project>/_compare/<run_id>/`` so the report is inspectable.
"""

from __future__ import annotations

import json
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rich import box
from rich.console import Console
from rich.table import Table

from watchmen.util import bundle_dir


DEFAULT_REFERENCE_MODEL = "anthropic/claude-opus-4-7"
DEFAULT_JUDGE_MODEL = "openai/gpt-5.5"
DEFAULT_PROVIDER = "openrouter"
DEFAULT_CANDIDATES = [
    "openai/gpt-5-mini",
    "deepseek/deepseek-v4-flash",
    "anthropic/claude-sonnet-4-6",
    "minimax/minimax-m2.5",
    "tencent/hy3-preview",
    "stepfun/step-3.5-flash",
    "moonshotai/kimi-k2.6",
]


@dataclass
class SkillBucketEvidence:
    project_key: str
    bucket: str
    skill_md: str
    candidate: dict[str, Any] | None
    workspace_brief: str
    curation_log: str


@dataclass
class CompareTask:
    id: str
    title: str
    instructions: str


@dataclass
class CompareConfig:
    project_key: str
    bucket: str
    reference_model: str = DEFAULT_REFERENCE_MODEL
    judge_model: str = DEFAULT_JUDGE_MODEL
    candidates: list[str] = field(default_factory=lambda: list(DEFAULT_CANDIDATES))
    task_count: int = 3
    reference_n: int = 1
    candidate_n: int = 3
    provider: str = DEFAULT_PROVIDER
    temperature: float = 0.4
    judge_temperature: float = 0.0
    max_tokens: int = 2600
    generation_concurrency: int = 4


@dataclass
class GenerationRecord:
    run_id: str
    task_id: str
    output_id: str
    model: str
    role: str
    sample_index: int
    output: str
    usage: dict[str, Any]
    cost_usd: float
    latency_s: float
    error: str | None = None
    finish_reason: str | None = None
    # Coarse error category: "rate_limit" (HTTP 429), "auth" (401/403),
    # "unknown_model" (400 with a "model" hint), or "other". None means
    # the call succeeded. Used by route to distinguish "this candidate
    # got rate-limited" from "this candidate broke" so the iterative
    # improver can decide whether to retry, skip, or fail loudly.
    error_kind: str | None = None


@dataclass
class ScoreRecord:
    task_id: str
    output_id: str
    score: float
    schema: int
    trigger_quality: int
    procedure_quality: int
    evidence_grounding: int
    context_efficiency: int
    rationale: str


@dataclass
class ModelTaskResult:
    task_id: str
    model: str
    role: str
    best_output_id: str
    best_score: float
    sample_count: int
    cost_usd: float
    produced_tokens: int
    latency_s: float
    visible_chars: int = 0
    empty_outputs: int = 0
    maxed_outputs: int = 0


@dataclass
class ModelSummary:
    model: str
    role: str
    avg_score: float
    worst_score: float
    wins_vs_reference: int | None
    task_count: int
    cost_usd: float
    cost_vs_reference: float | None
    produced_tokens: int
    produced_tokens_vs_reference: float | None
    visible_chars: int
    empty_outputs: int
    maxed_outputs: int
    sample_count: int
    latency_s: float
    latency_vs_reference: float | None
    decision: str
    decision_note: str


@dataclass
class CompareResult:
    run_id: str
    run_dir: str
    config: CompareConfig
    tasks: list[CompareTask]
    generations: list[GenerationRecord]
    scores: list[ScoreRecord]
    task_results: list[ModelTaskResult]
    summaries: list[ModelSummary]


@dataclass(frozen=True)
class _GenerationJob:
    task_output_index: int
    output_index: int
    model: str
    role: str
    sample_index: int
    sample_total: int


_TASK_TEMPLATES = [
    CompareTask(
        id="task-1",
        title="Recreate the skill",
        instructions=(
            "Create the strongest SKILL.md for this bucket from the evidence. "
            "Optimize for accurate triggers, concrete procedure, and valid skill frontmatter."
        ),
    ),
    CompareTask(
        id="task-2",
        title="Reduce context rot",
        instructions=(
            "Create a tighter SKILL.md for this bucket that preserves the real workflow "
            "while removing redundant, stale, or over-specific guidance."
        ),
    ),
    CompareTask(
        id="task-3",
        title="Strengthen boundaries",
        instructions=(
            "Create a SKILL.md that is especially careful about when_to_use and "
            "when_not_to_use, so the skill fires for the right tasks and stays quiet otherwise."
        ),
    ),
]


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n\n[truncated]"


def _json_dump(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _jsonl_dump(path: Path, rows: list[Any]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(asdict(row), sort_keys=True) + "\n")


def _generation_sort_key(rec: GenerationRecord) -> tuple[str, str]:
    return (rec.task_id, rec.output_id)


def _read_text_if_exists(path: Path, *, limit: int) -> str:
    if not path.exists():
        return ""
    return _clip(path.read_text(encoding="utf-8", errors="replace"), limit)


def load_skill_bucket_evidence(project_key: str, bucket: str) -> SkillBucketEvidence:
    base = bundle_dir(project_key)
    skill_md = base / "skills" / bucket / "SKILL.md"
    if not skill_md.exists():
        raise FileNotFoundError(f"no skill bucket at {skill_md}")

    candidate: dict[str, Any] | None = None
    candidates_path = base / "_candidates.json"
    if candidates_path.exists():
        try:
            candidates = json.loads(candidates_path.read_text(encoding="utf-8"))
        except Exception:
            candidates = []
        if isinstance(candidates, list):
            candidate = next((c for c in candidates if c.get("slug") == bucket), None)

    brief_parts = []
    for name in ("CLAUDE.md", "AGENTS.md", "_index.md"):
        path = base / name
        text = _read_text_if_exists(path, limit=6000)
        if text:
            brief_parts.append(f"## {name}\n\n{text}")

    return SkillBucketEvidence(
        project_key=project_key,
        bucket=bucket,
        skill_md=_read_text_if_exists(skill_md, limit=14000),
        candidate=candidate,
        workspace_brief="\n\n---\n\n".join(brief_parts) or "(no workspace brief found)",
        curation_log=_read_text_if_exists(base / "_curation_log.md", limit=8000),
    )


def build_compare_tasks(count: int) -> list[CompareTask]:
    count = max(1, count)
    tasks = list(_TASK_TEMPLATES[:count])
    while len(tasks) < count:
        idx = len(tasks) + 1
        tasks.append(
            CompareTask(
                id=f"task-{idx}",
                title=f"Stress variant {idx}",
                instructions=(
                    "Create a production-ready SKILL.md for this bucket, preserving only "
                    "evidence-backed project details and avoiding hallucinated files or commands."
                ),
            )
        )
    return tasks


def _evidence_packet(evidence: SkillBucketEvidence) -> str:
    candidate_json = json.dumps(evidence.candidate or {}, indent=2, sort_keys=True)
    return _clip(
        "\n\n".join([
            f"Project: {evidence.project_key}",
            f"Bucket skill slug: {evidence.bucket}",
            "## Candidate metadata\n\n" + candidate_json,
            "## Existing SKILL.md\n\n" + evidence.skill_md,
            "## Workspace brief\n\n" + evidence.workspace_brief,
            "## Curation log excerpt\n\n" + (evidence.curation_log or "(none)"),
        ]),
        28000,
    )


def _judge_evidence_packet(evidence: SkillBucketEvidence) -> str:
    candidate_json = json.dumps(evidence.candidate or {}, indent=2, sort_keys=True)
    return _clip(
        "\n\n".join([
            f"Project: {evidence.project_key}",
            f"Bucket skill slug: {evidence.bucket}",
            "## Candidate metadata\n\n" + _clip(candidate_json, 2500),
            "## Existing SKILL.md\n\n" + _clip(evidence.skill_md, 7000),
            "## Workspace brief excerpt\n\n" + _clip(evidence.workspace_brief, 3500),
            "## Curation log excerpt\n\n" + _clip(evidence.curation_log or "(none)", 2500),
        ]),
        18000,
    )


def _generation_messages(evidence: SkillBucketEvidence, task: CompareTask) -> list[dict[str, str]]:
    system = (
        "You are a senior coding-agent skill author. Return only the complete SKILL.md "
        "content, including YAML frontmatter. Do not wrap it in markdown fences. "
        "Ground every project-specific detail in the evidence."
    )
    user = (
        f"Task: {task.title}\n\n"
        f"{task.instructions}\n\n"
        "Rubric the output will be judged on: valid SKILL.md structure, precise triggers, "
        "clear boundaries, actionable procedure, evidence grounding, and context efficiency.\n\n"
        + _evidence_packet(evidence)
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _extract_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.removeprefix("```json").removeprefix("```").strip()
        if stripped.endswith("```"):
            stripped = stripped[:-3].strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end < start:
        raise ValueError("judge response did not contain a JSON object")
    parsed = json.loads(stripped[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("judge response was not a JSON object")
    return parsed


def _model_for_pricing(model: str) -> str:
    return model.split("/", 1)[1] if "/" in model else model


def _cost_for_usage(model: str, usage: dict[str, Any]) -> float:
    if not usage:
        return 0.0
    cost_details = usage.get("cost_details") or {}
    for provider_cost in (cost_details.get("upstream_inference_cost"), usage.get("cost")):
        try:
            value = float(provider_cost)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    try:
        from watchmen.model_prices import turn_cost_usd
    except Exception:
        return 0.0
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    cache_read = int(((usage.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0)
    fresh_input = max(prompt - cache_read, 0)
    try:
        return turn_cost_usd(_model_for_pricing(model), fresh_input, 0, 0, cache_read, completion)
    except Exception:
        return 0.0


def _produced_tokens(usage: dict[str, Any]) -> int:
    if not usage:
        return 0
    for key in ("completion_tokens", "output_tokens"):
        try:
            return max(0, int(usage.get(key) or 0))
        except (TypeError, ValueError):
            continue
    return 0


def _visible_chars(text: str) -> int:
    return len(text.strip())


def _maxed_completion(usage: dict[str, Any], max_tokens: int) -> bool:
    if max_tokens <= 0:
        return False
    return _produced_tokens(usage) >= max_tokens


def _call_model_data(
    client,
    *,
    provider: str,
    model: str,
    messages: list[dict[str, str]],
    agent_name: str,
    temperature: float,
    max_tokens: int,
    json_mode: bool = False,
) -> dict[str, Any]:
    from watchmen.agent import chat_call

    extra: dict[str, Any] = {}
    if json_mode and provider in {"openrouter", "openai"}:
        extra["response_format"] = {"type": "json_object"}
    data = chat_call(
        client,
        messages,
        provider=provider,
        model=model,
        agent_name=agent_name,
        max_retries=2,
        temperature=temperature,
        max_tokens=max_tokens,
        **extra,
    )
    return data


def _call_model(
    client,
    *,
    provider: str,
    model: str,
    messages: list[dict[str, str]],
    agent_name: str,
    temperature: float,
    max_tokens: int,
    json_mode: bool = False,
) -> tuple[str, dict[str, Any]]:
    data = _call_model_data(
        client,
        provider=provider,
        model=model,
        messages=messages,
        agent_name=agent_name,
        temperature=temperature,
        max_tokens=max_tokens,
        json_mode=json_mode,
    )
    return data["choices"][0]["message"].get("content") or "", data.get("usage") or {}


def _generate_one(
    client,
    config: CompareConfig,
    evidence: SkillBucketEvidence,
    task: CompareTask,
    *,
    run_id: str,
    model: str,
    role: str,
    sample_index: int,
    output_index: int,
) -> GenerationRecord:
    output_id = f"{task.id}-out-{output_index:03d}"
    start = time.monotonic()
    finish_reason = None
    error_kind: str | None = None
    try:
        data = _call_model_data(
            client,
            provider=config.provider,
            model=model,
            messages=_generation_messages(evidence, task),
            agent_name="compare-generator",
            temperature=config.temperature,
            max_tokens=config.max_tokens,
        )
        choice = data["choices"][0]
        output = choice["message"].get("content") or ""
        usage = data.get("usage") or {}
        finish_reason = choice.get("finish_reason")
        error = None
    except Exception as exc:
        output = f"GENERATION_ERROR: {type(exc).__name__}: {exc}"
        usage = {}
        error = f"{type(exc).__name__}: {exc}"
        error_kind = _classify_call_exception(exc)
    latency = time.monotonic() - start
    return GenerationRecord(
        run_id=run_id,
        task_id=task.id,
        output_id=output_id,
        model=model,
        role=role,
        sample_index=sample_index,
        output=output,
        usage=usage,
        cost_usd=_cost_for_usage(model, usage),
        latency_s=round(latency, 3),
        error=error,
        finish_reason=finish_reason,
        error_kind=error_kind,
    )


def _classify_call_exception(exc: Exception) -> str:
    """Bucket a chat_call failure into a coarse error category.

    Sources we recognize:
      - `httpx.HTTPStatusError`: read the status code off `.response`.
      - Any exception whose stringified form mentions a recognizable
        provider phrase ("rate_limit_error", "unauthorized", etc.) —
        cheap fallback for wrapped/re-raised exceptions.

    Categories:
      - "rate_limit" — HTTP 429 or rate_limit_error body. Most actionable
        for the iterative improver: it can retry/skip rather than treat
        the candidate as broken.
      - "auth" — HTTP 401 / 403. Distinct from rate limit because no
        amount of waiting fixes it; the credential is wrong.
      - "unknown_model" — HTTP 400 with "model" in body (Anthropic /
        OpenAI both surface this when the id isn't in their catalog).
      - "other" — everything else (timeouts, 5xx, parse errors).
    """
    status: int | None = None
    body: str = ""
    try:
        # Local import to avoid forcing httpx at module-import time in
        # environments that mock the provider stack.
        import httpx
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            try:
                body = exc.response.text or ""
            except Exception:
                body = ""
    except Exception:
        pass

    if status == 429:
        return "rate_limit"
    if status in (401, 403):
        return "auth"
    if status == 400:
        # Only flag as "unknown_model" when the response body uses an
        # *unambiguous* model-not-found phrase. The previous heuristic
        # of "any 'model' substring" tripped on Anthropic 400s for
        # canonical models that were actually rate-limited (the body
        # contains words like "model" or routes through `/messages`).
        # Match specific tokens that providers emit when the id itself
        # is wrong, and nothing else.
        body_l = body.lower()
        if (
            "model_not_found" in body_l
            or "not_found_error" in body_l
            or "invalid model" in body_l
            or "unknown model" in body_l
        ):
            return "unknown_model"

    blob = str(exc).lower()
    if "rate_limit_error" in blob or "rate limit" in blob:
        return "rate_limit"
    if "unauthorized" in blob or "authentication_error" in blob:
        return "auth"
    if "model_not_found" in blob or "unknown model" in blob:
        return "unknown_model"
    return "other"


def _build_generation_jobs(config: CompareConfig, *, start_output_index: int) -> tuple[list[_GenerationJob], int]:
    jobs: list[_GenerationJob] = []
    output_index = start_output_index
    task_output_index = 0
    for sample in range(1, config.reference_n + 1):
        output_index += 1
        task_output_index += 1
        jobs.append(
            _GenerationJob(
                task_output_index=task_output_index,
                output_index=output_index,
                model=config.reference_model,
                role="reference",
                sample_index=sample,
                sample_total=config.reference_n,
            )
        )
    for model in config.candidates:
        for sample in range(1, config.candidate_n + 1):
            output_index += 1
            task_output_index += 1
            jobs.append(
                _GenerationJob(
                    task_output_index=task_output_index,
                    output_index=output_index,
                    model=model,
                    role="candidate",
                    sample_index=sample,
                    sample_total=config.candidate_n,
                )
            )
    return jobs, output_index


def _generation_label(job: _GenerationJob, total: int) -> str:
    return (
        f"gen {job.task_output_index}/{total}: "
        f"{job.role} {job.model} sample {job.sample_index}/{job.sample_total}"
    )


def _judge_messages(
    evidence: SkillBucketEvidence,
    task: CompareTask,
    outputs: list[GenerationRecord],
) -> list[dict[str, str]]:
    system = (
        "You are watchmen's blind skill-output judge. Score each anonymous SKILL.md "
        "against the evidence and task. Do not reward similarity to a reference; reward "
        "the best evidence-grounded skill artifact. Return only JSON."
    )
    chunks = []
    for rec in outputs:
        chunks.append(
            f"===== OUTPUT {rec.output_id} =====\n"
            f"{_clip(rec.output, 4000)}"
        )
    user = (
        f"Project: {evidence.project_key}\n"
        f"Bucket: {evidence.bucket}\n"
        f"Task: {task.title}\n\n"
        f"{task.instructions}\n\n"
        "Evidence packet:\n"
        f"{_judge_evidence_packet(evidence)}\n\n"
        "Score every output with integers 0-5 for schema, trigger_quality, "
        "procedure_quality, evidence_grounding, and context_efficiency. "
        "Also provide score as a float 0.0-1.0.\n\n"
        "Return JSON exactly like:\n"
        '{"scores":[{"id":"task-1-out-001","score":0.0,"schema":0,'
        '"trigger_quality":0,"procedure_quality":0,"evidence_grounding":0,'
        '"context_efficiency":0,"rationale":"short reason"}]}\n\n'
        + "\n\n".join(chunks)
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _coerce_score(task_id: str, raw: dict[str, Any]) -> ScoreRecord:
    def cint(key: str) -> int:
        try:
            return max(0, min(5, int(round(float(raw.get(key, 0))))))
        except (TypeError, ValueError):
            return 0

    try:
        score = max(0.0, min(1.0, float(raw.get("score", 0.0))))
    except (TypeError, ValueError):
        score = 0.0
    return ScoreRecord(
        task_id=task_id,
        output_id=str(raw.get("id") or raw.get("output_id") or ""),
        score=round(score, 4),
        schema=cint("schema"),
        trigger_quality=cint("trigger_quality"),
        procedure_quality=cint("procedure_quality"),
        evidence_grounding=cint("evidence_grounding"),
        context_efficiency=cint("context_efficiency"),
        rationale=str(raw.get("rationale") or "").strip()[:500],
    )


def _judge_task_outputs(
    client,
    config: CompareConfig,
    evidence: SkillBucketEvidence,
    task: CompareTask,
    outputs: list[GenerationRecord],
    *,
    run_id: str,
    run_dir: Path | None = None,
) -> list[ScoreRecord]:
    shuffled = list(outputs)
    random.Random(f"{run_id}:{task.id}").shuffle(shuffled)
    messages = _judge_messages(evidence, task, shuffled)
    last_error = ""
    scores: list[ScoreRecord] = []
    for attempt in range(1, 4):
        content, _usage = _call_model(
            client,
            provider=config.provider,
            model=config.judge_model,
            messages=messages,
            agent_name="compare-judge",
            temperature=config.judge_temperature,
            max_tokens=max(1800, 260 * len(outputs)),
            json_mode=True,
        )
        try:
            parsed = _extract_json_object(content)
            raw_scores = parsed.get("scores") or []
            if not isinstance(raw_scores, list):
                raise ValueError("judge JSON did not contain a scores list")
            scores = [_coerce_score(task.id, raw) for raw in raw_scores if isinstance(raw, dict)]
            if not scores:
                raise ValueError("judge JSON contained no score records")
            break
        except (json.JSONDecodeError, ValueError) as exc:
            last_error = str(exc)
            if run_dir is not None:
                (run_dir / f"{task.id}.judge-attempt-{attempt}.txt").write_text(
                    content or "(empty response)",
                    encoding="utf-8",
                )
            if attempt == 3:
                scores = [
                    ScoreRecord(
                        task_id=task.id,
                        output_id=rec.output_id,
                        score=0.0,
                        schema=0,
                        trigger_quality=0,
                        procedure_quality=0,
                        evidence_grounding=0,
                        context_efficiency=0,
                        rationale=f"Judge failed to return parseable JSON after retries: {last_error}",
                    )
                    for rec in outputs
                ]
                break
            messages = [
                *messages,
                {"role": "assistant", "content": _clip(content or "(empty response)", 2000)},
                {
                    "role": "user",
                    "content": (
                        "Your previous response was not valid JSON for the required schema. "
                        "Return only a JSON object with a `scores` list. Include exactly one "
                        "score object for every output id."
                    ),
                },
            ]
    seen = {score.output_id for score in scores}
    for rec in outputs:
        if rec.output_id not in seen:
            scores.append(
                ScoreRecord(
                    task_id=task.id,
                    output_id=rec.output_id,
                    score=0.0,
                    schema=0,
                    trigger_quality=0,
                    procedure_quality=0,
                    evidence_grounding=0,
                    context_efficiency=0,
                    rationale="Judge did not return a score for this output.",
                )
            )
    return scores


def _build_task_results(
    config: CompareConfig,
    tasks: list[CompareTask],
    generations: list[GenerationRecord],
    scores: list[ScoreRecord],
) -> list[ModelTaskResult]:
    by_score = {score.output_id: score for score in scores}
    results: list[ModelTaskResult] = []
    models = sorted({rec.model for rec in generations})
    for task in tasks:
        for model in models:
            recs = [rec for rec in generations if rec.task_id == task.id and rec.model == model]
            if not recs:
                continue
            best = max(recs, key=lambda rec: by_score.get(rec.output_id, ScoreRecord(task.id, rec.output_id, 0, 0, 0, 0, 0, 0, "")).score)
            best_score = by_score.get(best.output_id)
            results.append(
                ModelTaskResult(
                    task_id=task.id,
                    model=model,
                    role=best.role,
                    best_output_id=best.output_id,
                    best_score=best_score.score if best_score else 0.0,
                    sample_count=len(recs),
                    cost_usd=round(sum(rec.cost_usd for rec in recs), 6),
                    produced_tokens=sum(_produced_tokens(rec.usage) for rec in recs),
                    latency_s=round(sum(rec.latency_s for rec in recs), 3),
                    visible_chars=sum(_visible_chars(rec.output) for rec in recs),
                    empty_outputs=sum(1 for rec in recs if not rec.output.strip()),
                    maxed_outputs=sum(1 for rec in recs if _maxed_completion(rec.usage, config.max_tokens)),
                )
            )
    return results


def _dominating_model(candidate: ModelSummary, others: list[ModelSummary]) -> ModelSummary | None:
    """Return the clearest model that dominates this one on quality/cost/latency."""
    dominators: list[ModelSummary] = []
    for other in others:
        if other.model == candidate.model:
            continue
        quality_ok = other.avg_score >= candidate.avg_score
        cost_ok = other.cost_usd <= candidate.cost_usd
        latency_ok = other.latency_s <= candidate.latency_s
        strictly_better = (
            other.avg_score > candidate.avg_score
            or other.cost_usd < candidate.cost_usd
            or other.latency_s < candidate.latency_s
        )
        if quality_ok and cost_ok and latency_ok and strictly_better:
            dominators.append(other)
    if not dominators:
        return None
    return sorted(
        dominators,
        key=lambda row: (
            row.role != "reference",
            -row.avg_score,
            row.cost_usd,
            row.latency_s,
        ),
    )[0]


def _summarize_models(config: CompareConfig, task_results: list[ModelTaskResult]) -> list[ModelSummary]:
    ref_rows = [row for row in task_results if row.role == "reference"]
    ref_avg = sum(row.best_score for row in ref_rows) / len(ref_rows) if ref_rows else 0.0
    ref_cost = sum(row.cost_usd for row in ref_rows)
    ref_tokens = sum(row.produced_tokens for row in ref_rows)
    ref_latency = sum(row.latency_s for row in ref_rows)
    ref_by_task = {row.task_id: row.best_score for row in ref_rows}

    summaries: list[ModelSummary] = []
    for model in [config.reference_model, *config.candidates]:
        rows = [row for row in task_results if row.model == model]
        if not rows:
            continue
        role = rows[0].role
        avg = sum(row.best_score for row in rows) / len(rows)
        cost = sum(row.cost_usd for row in rows)
        produced_tokens = sum(row.produced_tokens for row in rows)
        visible_chars = sum(row.visible_chars for row in rows)
        empty_outputs = sum(row.empty_outputs for row in rows)
        maxed_outputs = sum(row.maxed_outputs for row in rows)
        sample_count = sum(row.sample_count for row in rows)
        latency = sum(row.latency_s for row in rows)
        wins = None
        if role != "reference":
            wins = sum(1 for row in rows if row.best_score >= ref_by_task.get(row.task_id, 1.01))
        summaries.append(
            ModelSummary(
                model=model,
                role=role,
                avg_score=round(avg, 4),
                worst_score=round(min(row.best_score for row in rows), 4),
                wins_vs_reference=wins,
                task_count=len(rows),
                cost_usd=round(cost, 6),
                cost_vs_reference=round(cost / ref_cost, 4) if ref_cost else None,
                produced_tokens=produced_tokens,
                produced_tokens_vs_reference=round(produced_tokens / ref_tokens, 4) if ref_tokens else None,
                visible_chars=visible_chars,
                empty_outputs=empty_outputs,
                maxed_outputs=maxed_outputs,
                sample_count=sample_count,
                latency_s=round(latency, 3),
                latency_vs_reference=round(latency / ref_latency, 4) if ref_latency else None,
                decision="reference" if role == "reference" else "pending",
                decision_note="baseline" if role == "reference" else "",
            )
        )

    for summary in summaries:
        if summary.role == "reference":
            continue
        if summary.sample_count > 0 and summary.empty_outputs == summary.sample_count:
            summary.decision = "invalid output"
            summary.decision_note = f"all {summary.sample_count} samples empty"
        elif summary.empty_outputs > 0:
            summary.decision = "unstable output"
            summary.decision_note = f"{summary.empty_outputs}/{summary.sample_count} samples empty"
        elif summary.maxed_outputs > 0 and summary.worst_score < 0.5:
            summary.decision = "truncated output"
            summary.decision_note = f"{summary.maxed_outputs}/{summary.sample_count} samples hit max tokens"
        elif (dominator := _dominating_model(summary, summaries)) is not None:
            summary.decision = "dominated"
            summary.decision_note = f"dominated by {dominator.model}"
        elif summary.avg_score >= max(0.0, ref_avg - 0.03) and (summary.cost_vs_reference or 999) < 1.0:
            if (summary.latency_vs_reference or 999) <= 1.10:
                summary.decision = "replace reference"
                summary.decision_note = "near quality, cheaper, similar/faster"
            else:
                summary.decision = "cost tradeoff"
                summary.decision_note = "near quality and cheaper, but slower"
        elif (summary.cost_vs_reference or 999) < 1.0:
            summary.decision = "cheap tradeoff"
            summary.decision_note = "cheaper, lower quality"
        elif summary.avg_score > ref_avg:
            summary.decision = "quality tradeoff"
            summary.decision_note = "higher quality, but cost/latency tradeoff"
        elif (summary.latency_vs_reference or 999) < 1.0:
            summary.decision = "speed tradeoff"
            summary.decision_note = "faster, not cheaper/near-quality"
        else:
            summary.decision = "non-dominated tradeoff"
            summary.decision_note = "no model beats all 3 axes"
    return sorted(summaries, key=lambda row: (row.role != "reference", -row.avg_score, row.cost_usd))


def _write_report(result: CompareResult) -> Path:
    path = Path(result.run_dir) / "report.md"
    lines = [
        f"# watchmen compare: {result.config.project_key}/{result.config.bucket}",
        "",
        f"- run: `{result.run_id}`",
        f"- reference: `{result.config.reference_model}` x{result.config.reference_n}",
        f"- candidates: {', '.join(f'`{m}` x{result.config.candidate_n}' for m in result.config.candidates)}",
        f"- judge: `{result.config.judge_model}`",
        f"- generation concurrency: `{result.config.generation_concurrency}`",
        "",
        "| model | role | avg | worst | wins | cost | cost vs ref | comp tok | tok/ref | chars | empty | maxed | latency | decision | note |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    for row in result.summaries:
        wins = "ref" if row.wins_vs_reference is None else f"{row.wins_vs_reference}/{row.task_count}"
        cost_ratio = "-" if row.cost_vs_reference is None else f"{row.cost_vs_reference:.2f}x"
        token_ratio = "-" if row.produced_tokens_vs_reference is None else f"{row.produced_tokens_vs_reference:.2f}x"
        lines.append(
            f"| `{row.model}` | {row.role} | {row.avg_score:.3f} | {row.worst_score:.3f} | "
            f"{wins} | ${row.cost_usd:.4f} | {cost_ratio} | {row.produced_tokens} | "
            f"{token_ratio} | {row.visible_chars} | {row.empty_outputs}/{row.sample_count} | "
            f"{row.maxed_outputs}/{row.sample_count} | "
            f"{row.latency_s:.1f}s | {row.decision} | {row.decision_note} |"
        )
    lines.append("")
    lines.append("## Best Outputs")
    for task in result.tasks:
        lines.append("")
        lines.append(f"### {task.id}: {task.title}")
        for row in result.task_results:
            if row.task_id == task.id:
                lines.append(f"- `{row.model}` best `{row.best_output_id}` score={row.best_score:.3f}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def run_compare(
    config: CompareConfig,
    *,
    run_id: str | None = None,
    progress=None,
    evidence: SkillBucketEvidence | None = None,
) -> CompareResult:
    """Run one compare pass over a skill bucket.

    Set ``evidence`` to bypass the on-disk SKILL.md read; ``watchmen route``
    uses this to iterate against an in-memory revised SKILL.md without
    clobbering the user's bundle until the loop converges.  Default
    behaviour is unchanged.
    """
    import httpx

    run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if evidence is None:
        evidence = load_skill_bucket_evidence(config.project_key, config.bucket)
    tasks = build_compare_tasks(config.task_count)
    run_dir = bundle_dir(config.project_key) / "_compare" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    _json_dump(run_dir / "config.json", asdict(config))
    _json_dump(run_dir / "tasks.json", [asdict(task) for task in tasks])
    _jsonl_dump(run_dir / "generations.jsonl", [])
    _jsonl_dump(run_dir / "judgments.jsonl", [])

    generations: list[GenerationRecord] = []
    scores: list[ScoreRecord] = []
    output_index = 0
    with httpx.Client(timeout=300.0) as client:
        for task_idx, task in enumerate(tasks, start=1):
            jobs, output_index = _build_generation_jobs(config, start_output_index=output_index)
            per_task_outputs = len(jobs)
            generation_concurrency = max(1, int(config.generation_concurrency or 1))
            max_workers = min(generation_concurrency, per_task_outputs) if per_task_outputs else 1
            if progress:
                progress(
                    f"task {task_idx}/{len(tasks)}: generating {per_task_outputs} outputs "
                    f"(concurrency={max_workers})"
                )
            task_generations: list[GenerationRecord] = []
            if jobs:
                with ThreadPoolExecutor(max_workers=max_workers) as pool:
                    future_to_job = {}
                    for job in jobs:
                        if progress:
                            progress(f"task {task_idx}/{len(tasks)}: queued {_generation_label(job, per_task_outputs)}")
                        future = pool.submit(
                            _generate_one,
                            client,
                            config,
                            evidence,
                            task,
                            run_id=run_id,
                            model=job.model,
                            role=job.role,
                            sample_index=job.sample_index,
                            output_index=job.output_index,
                        )
                        future_to_job[future] = job
                    for future in as_completed(future_to_job):
                        job = future_to_job[future]
                        try:
                            record = future.result()
                        except Exception as exc:
                            record = GenerationRecord(
                                run_id=run_id,
                                task_id=task.id,
                                output_id=f"{task.id}-out-{job.output_index:03d}",
                                model=job.model,
                                role=job.role,
                                sample_index=job.sample_index,
                                output=f"GENERATION_ERROR: {type(exc).__name__}: {exc}",
                                usage={},
                                cost_usd=0.0,
                                latency_s=0.0,
                                error=f"{type(exc).__name__}: {exc}",
                            )
                        task_generations.append(record)
                        generations.append(record)
                        generations.sort(key=_generation_sort_key)
                        _jsonl_dump(run_dir / "generations.jsonl", generations)
                        if progress:
                            detail = (
                                f"{_produced_tokens(record.usage)} comp tok, "
                                f"{_visible_chars(record.output)} chars"
                            )
                            if record.finish_reason:
                                detail += f", finish={record.finish_reason}"
                            if record.output == "":
                                detail += ", empty"
                            status = (
                                "error"
                                if record.error
                                else f"${record.cost_usd:.4f}, {record.latency_s:.1f}s, {detail}"
                            )
                            progress(
                                f"task {task_idx}/{len(tasks)}: done "
                                f"{_generation_label(job, per_task_outputs)} ({status})"
                            )
            task_generations.sort(key=_generation_sort_key)
            if progress:
                progress(f"task {task_idx}/{len(tasks)}: judging {len(task_generations)} outputs with {config.judge_model}")
            task_scores = _judge_task_outputs(
                client,
                config,
                evidence,
                task,
                task_generations,
                run_id=run_id,
                run_dir=run_dir,
            )
            scores.extend(task_scores)
            _jsonl_dump(run_dir / "judgments.jsonl", scores)
            if progress and any(score.rationale.startswith("Judge failed") for score in task_scores):
                progress(f"task {task_idx}/{len(tasks)}: judge failed after retries; outputs saved for inspection")

    task_results = _build_task_results(config, tasks, generations, scores)
    summaries = _summarize_models(config, task_results)
    result = CompareResult(
        run_id=run_id,
        run_dir=str(run_dir),
        config=config,
        tasks=tasks,
        generations=generations,
        scores=scores,
        task_results=task_results,
        summaries=summaries,
    )

    _jsonl_dump(run_dir / "generations.jsonl", generations)
    _jsonl_dump(run_dir / "judgments.jsonl", scores)
    _json_dump(run_dir / "summary.json", {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "summaries": [asdict(row) for row in summaries],
        "task_results": [asdict(row) for row in task_results],
    })
    _write_report(result)
    return result


def render_compare_summary(result: CompareResult, *, console: Console | None = None) -> None:
    console = console or Console()
    console.print(
        f"[bold]watchmen compare[/] {result.config.project_key}/{result.config.bucket}\n"
        f"[dim]run={result.run_id} judge={result.config.judge_model} "
        f"reference={result.config.reference_model} x{result.config.reference_n} "
        f"candidate_n={result.config.candidate_n} concurrency={result.config.generation_concurrency}[/]"
    )
    table = Table(box=box.SIMPLE)
    table.add_column("model")
    table.add_column("avg", justify="right")
    table.add_column("worst", justify="right")
    table.add_column("wins", justify="right")
    table.add_column("cost", justify="right")
    table.add_column("cost/ref", justify="right")
    table.add_column("comp tok", justify="right")
    table.add_column("tok/ref", justify="right")
    table.add_column("chars", justify="right")
    table.add_column("empty", justify="right")
    table.add_column("maxed", justify="right")
    table.add_column("latency", justify="right")
    table.add_column("decision")
    table.add_column("why")

    summaries = result.summaries

    def max_value(values):
        return max(values) if values else None

    def min_value(values):
        return min(values) if values else None

    best_avg = max_value([row.avg_score for row in summaries])
    best_worst = max_value([row.worst_score for row in summaries])
    best_wins = max_value([row.wins_vs_reference for row in summaries if row.wins_vs_reference is not None])
    best_cost = min_value([row.cost_usd for row in summaries])
    best_cost_ratio = min_value([row.cost_vs_reference for row in summaries if row.cost_vs_reference is not None])
    best_token_ratio = min_value([
        row.produced_tokens_vs_reference
        for row in summaries
        if row.produced_tokens_vs_reference is not None
    ])
    best_chars = max_value([row.visible_chars for row in summaries])
    best_empty = min_value([row.empty_outputs for row in summaries])
    best_maxed = min_value([row.maxed_outputs for row in summaries])
    best_latency = min_value([row.latency_s for row in summaries])

    def cell(value: str, winner: bool, style: str = "green") -> str:
        return f"[{style}]{value}[/]" if winner else value

    for row in result.summaries:
        wins = "ref" if row.wins_vs_reference is None else f"{row.wins_vs_reference}/{row.task_count}"
        cost_ratio = "-" if row.cost_vs_reference is None else f"{row.cost_vs_reference:.2f}x"
        token_ratio = "-" if row.produced_tokens_vs_reference is None else f"{row.produced_tokens_vs_reference:.2f}x"
        table.add_row(
            cell(row.model, row.role == "reference", "bold"),
            cell(f"{row.avg_score:.3f}", row.avg_score == best_avg),
            cell(f"{row.worst_score:.3f}", row.worst_score == best_worst),
            cell(wins, row.wins_vs_reference is not None and row.wins_vs_reference == best_wins),
            cell(f"${row.cost_usd:.4f}", row.cost_usd == best_cost),
            cell(cost_ratio, row.cost_vs_reference is not None and row.cost_vs_reference == best_cost_ratio),
            str(row.produced_tokens),
            cell(token_ratio, row.produced_tokens_vs_reference is not None and row.produced_tokens_vs_reference == best_token_ratio),
            cell(str(row.visible_chars), row.visible_chars == best_chars),
            cell(f"{row.empty_outputs}/{row.sample_count}", row.empty_outputs == best_empty),
            cell(f"{row.maxed_outputs}/{row.sample_count}", row.maxed_outputs == best_maxed),
            cell(f"{row.latency_s:.1f}s", row.latency_s == best_latency),
            cell(row.decision, row.decision == "replace reference", "bold green"),
            row.decision_note,
        )
    console.print(table)
    console.print(f"[dim]artifacts -> {result.run_dir}[/]")
