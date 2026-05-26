"""Harness-aware skill-bucket model evaluation.

``watchmen route`` answers a sharper question than ``watchmen compare``:
given the harnesses the user *actually runs* on this project (claude-code,
codex, opencode, pi), and the model each one most recently used for this
skill bucket, which model should each harness swap to — and which file does
watchmen need to emit so the harness natively delegates the skill to that
model on the next invocation?

Engine reuses ``watchmen.compare``'s generate-best-of-N + blind-judge +
scoring pipeline.  The new work is in three places:

  1. ``detect_harnesses(...)`` reads watchmen's existing corpus.db (already
     populated by every adapter on every ingest) to find, per harness, the
     most-recent model the user ran for this project.
  2. ``run_route(...)`` calls compare's primitives once per harness, with
     that harness's current model as the reference and the same provider
     family as the candidate pool.
  3. ``classify_route(...)`` emits decision labels keyed to user action:
     ``stay``, ``downshift``, ``upshift``, ``switch-harness``.  Inherits
     ``dominated`` / ``unstable`` / ``invalid`` / ``truncated`` from compare
     so an output-quality issue isn't relabeled as a "downshift opportunity".

The per-harness file-emission step (``watchmen.route_rewrite``) is a
separate module: it converts the decision into the harness-specific
artifact (e.g. ``.claude/agents/<bucket>-router.md`` for claude-code,
``~/.codex/route-<bucket>.config.toml`` for codex) plus a body block in
SKILL.md pointing the main agent at it.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from watchmen.compare import (
    CompareConfig,
    CompareResult,
    ModelSummary,
    load_skill_bucket_evidence,
    run_compare,
)
from watchmen.util import (
    bundle_dir,
    corpus_db_path,
    project_dir_predicate,
)


# ─── Per-harness native provider resolution ──────────────────────────

# Maps a detected harness to the provider watchmen should route compare
# calls through.  OAuth subs (claude-pro, chatgpt) are preferred over
# API-direct paths because they're flat-rate — running 10 candidates
# costs the same as running 1 — which is exactly the budget shape route
# wants.  Multi-provider harnesses (opencode, pi.dev) inherit watchmen's
# global ``config.active_provider()`` since their actual provider is
# inferred from each session's namespace, not a single fixed choice.
HARNESS_NATIVE_PROVIDER: dict[str, str] = {
    "claude_code": "claude-pro",
    "codex": "chatgpt",
}


# Providers that expect bare (un-namespaced) model ids.  OpenRouter is
# the odd one out — it needs ``anthropic/claude-...`` / ``openai/...``
# because it routes by namespace.  Direct API and OAuth-sub providers
# take the bare form (``claude-opus-4-7``, ``gpt-5.5``).
NATIVE_BARE_PROVIDERS: set[str] = {
    "anthropic", "openai", "claude-pro", "chatgpt",
}


# Heuristic prefixes for "is this a $vendor model?" used by the
# provider-compat filter.  Match either a namespaced id
# (``openai/gpt-5.5``) or the bare model name's typical prefix pattern.
PROVIDER_BARE_MODEL_HINTS: dict[str, tuple[str, ...]] = {
    "anthropic": ("claude-", "opus-", "sonnet-", "haiku-"),
    "openai": ("gpt-", "o1", "o3", "o4", "o5", "o6", "davinci", "babbage"),
}


def provider_supports_model(model_id: str, provider: str) -> bool:
    """Whether ``provider`` can plausibly route ``model_id``.

    Filters candidate pools so the user's corpus history doesn't suggest
    cross-provider candidates the current provider can't dispatch — e.g.
    claude-pro OAuth can't run Gemini even if CC has Gemini sessions in
    its corpus from a previous multi-provider proxy setup.

    Heuristic but covers the common case.  When in doubt (a model id
    that doesn't match any known prefix), default to accept so users
    can still pass exotic ``--candidate`` values explicitly.
    """
    if provider == "openrouter":
        return "/" in model_id  # OR needs namespaced ids; bare won't route
    if provider not in {"anthropic", "openai", "claude-pro", "chatgpt"}:
        return True  # multi-provider / unknown — let it through
    vendor = {
        "claude-pro": "anthropic",
        "chatgpt": "openai",
        "anthropic": "anthropic",
        "openai": "openai",
    }[provider]
    if "/" in model_id:
        return model_id.split("/", 1)[0] == vendor
    hints = PROVIDER_BARE_MODEL_HINTS.get(vendor, ())
    return any(model_id.lower().startswith(h.lower()) for h in hints)


def model_id_for_provider(model_id: str, provider: str) -> str:
    """Return the model id in the format ``provider`` expects.

    OpenRouter requires the ``namespace/model`` form.  Every other
    provider we ship for (anthropic-direct, openai-direct, claude-pro
    OAuth, chatgpt OAuth) takes the bare model name.  This helper
    strips or preserves the namespace prefix as needed so the route
    code can stay provider-agnostic at the call site.
    """
    if provider in NATIVE_BARE_PROVIDERS:
        if "/" in model_id:
            return model_id.split("/", 1)[1]
        return model_id
    # OpenRouter / unknown: assume namespaced form is wanted.
    return model_id


def native_provider_for_harness(harness: str) -> str:
    """Which provider name to pass to ``watchmen.agent.chat_call`` for
    this harness's compare run.  Hardcoded for single-provider harnesses
    (CC always anthropic-tier, codex always openai-tier); falls back to
    watchmen's global default for multi-provider harnesses."""
    if harness in HARNESS_NATIVE_PROVIDER:
        return HARNESS_NATIVE_PROVIDER[harness]
    from watchmen import config as _cfg
    try:
        return _cfg.active_provider()
    except Exception:
        return "openrouter"


# ─── Constants ───────────────────────────────────────────────────────

# The four harnesses watchmen ships adapters for.  Anything outside this
# list won't have a route rewriter today; detection will simply skip it.
SUPPORTED_HARNESSES: tuple[str, ...] = ("claude_code", "codex", "opencode", "pi")

# Which provider family a harness's "native" model ids come from.  Used
# only by `_normalize_model_id` to add a sensible namespace prefix when a
# session's model_dominant is bare (codex writes 'gpt-5.5', CC writes
# 'claude-opus-4-7').  No longer used to BUILD candidate pools — that
# now comes from real corpus history.
HARNESS_DEFAULT_FAMILY: dict[str, str] = {
    "claude_code": "anthropic",
    "codex": "openai",
}

# Default lookback for discovering candidate models in the user's
# corpus.  Wider than detection (30d, picks the *current* model) so
# we capture every model the user has run in this harness within
# memory — including ones they used months ago and might want to
# return to.  365 days catches a full year of harness history,
# basically every model still relevant.
DEFAULT_CANDIDATE_SINCE_DAYS = 365


# ─── Types ───────────────────────────────────────────────────────────

@dataclass
class HarnessReference:
    """One harness's most-recent state, used as the route's baseline."""

    harness: str
    current_model: str
    last_session_ts: str
    session_count_window: int


@dataclass
class RouteConfig:
    project_key: str
    bucket: str
    harnesses: list[str] = field(default_factory=list)
    since_days: int = 30
    cross_harness: bool = False
    user_candidates: list[str] = field(default_factory=list)
    task_count: int = 3
    candidate_n: int = 3
    # None = default to the harness's reference (modal) model per
    # compare call. Set explicitly only when the user passes --judge.
    judge_model: str | None = None
    provider: str = "openrouter"
    temperature: float = 0.4
    max_tokens: int = 2600
    generation_concurrency: int = 4


@dataclass
class RouteDecision:
    """Per-harness verdict.  Inherits compare's quality-guard labels so an
    unstable / invalid candidate is never mis-promoted as a downshift."""

    harness: str
    current_model: str
    recommended_model: str | None
    label: str  # stay | downshift | upshift | switch-harness | dominated |
                # unstable | invalid | truncated | no-data
    note: str
    avg_score: float
    cost_vs_current: float | None
    summary: ModelSummary | None = None  # the winning candidate's compare row


@dataclass
class RouteResult:
    run_id: str
    run_dir: str
    config: RouteConfig
    references: list[HarnessReference]
    compare_results: dict[str, CompareResult]  # keyed by harness
    decisions: list[RouteDecision]


# ─── Detection: who is the user, what are they running? ──────────────

def detect_harnesses(
    project_key: str, *, since_days: int = 30
) -> list[HarnessReference]:
    """Per harness with sessions on this project in the last N days, return
    the most-recent (model, ts) pair plus a window count.

    Filters:
      - is_subagent = 0 (we don't want delegation-stack noise)
      - model_dominant IS NOT NULL (a session that never recorded a model
        can't anchor a route decision)
      - started_at >= now - since_days

    The "most-recent" rather than "modal" choice is deliberate: a user who
    swapped their default model yesterday wants the new model as the route
    baseline, not last month's habit.
    """
    db = corpus_db_path()
    if not db.exists():
        return []
    pred = project_dir_predicate(project_key)
    if not pred:
        return []
    where, params = pred

    cutoff = _iso_minus_days(since_days)
    conn = sqlite3.connect(db)
    try:
        # Modal by session count, primary + subagent both included (any
        # Task / thread_spawn delegation that ran model X reflects the
        # user's actual reliance on X).  Tie-break on most-recent so a
        # truly tied vote falls toward the model the user touched last.
        rows = conn.execute(
            f"""
            SELECT s.agent, s.model_dominant,
                   COUNT(*) AS n,
                   MAX(s.started_at) AS last_seen
            FROM sessions s
            WHERE {where}
              AND s.model_dominant IS NOT NULL
              AND s.model_dominant != ''
              AND s.model_dominant != '<synthetic>'
              AND s.started_at >= ?
            GROUP BY s.agent, s.model_dominant
            ORDER BY s.agent, n DESC, last_seen DESC
            """,
            (*params, cutoff),
        ).fetchall()
    finally:
        conn.close()

    references: list[HarnessReference] = []
    seen: set[str] = set()
    for agent, model, count, last_seen in rows:
        if agent in seen or agent not in SUPPORTED_HARNESSES:
            continue
        seen.add(agent)
        references.append(
            HarnessReference(
                harness=agent,
                current_model=_normalize_model_id(model, harness=agent),
                last_session_ts=last_seen,
                session_count_window=int(count),
            )
        )
    return references


def _iso_minus_days(days: int) -> str:
    return (
        datetime.now(timezone.utc).replace(microsecond=0)
        - _timedelta_days(days)
    ).isoformat().replace("+00:00", "Z")


def _timedelta_days(days: int):
    from datetime import timedelta
    return timedelta(days=days)


# ─── Provider family + candidate curation ─────────────────────────────

def _normalize_model_id(model_id: str, *, harness: str | None = None) -> str:
    """Canonicalize bare model ids to provider-namespaced form when the
    harness implies a default provider.  Adapters disagree on what they
    write to ``sessions.model_dominant`` — codex writes ``gpt-5.5`` bare,
    claude-code writes ``claude-opus-4-7`` bare, while pi and opencode use
    the namespaced form.  Without normalization, reference and candidate
    comparisons trip on string-equality mismatches and we surface
    recommendations like "swap from gpt-5.5 to openai/gpt-5.5" — which is
    a no-op the user can't act on.
    """
    if "/" in model_id or not harness:
        return model_id
    family = HARNESS_DEFAULT_FAMILY.get(harness)
    if family is None:
        return model_id
    return f"{family}/{model_id}"


def _canonicalize_corpus_model_id(model_id: str) -> str | None:
    """Coerce a corpus-stored model id into the form `/v1/models` accepts.

    Corpus ids come from heterogeneous adapter sources and aren't all in
    canonical Anthropic/OpenAI form:
      - CC sometimes records dot-version aliases like `claude-opus-4.7`
        (display string), which the API rejects with 400 — needs `-7`.
      - Older CC versions wrote `claude-4.5-haiku-20251001` (version-first
        word order) instead of canonical `claude-haiku-4-5-20251001`.
      - Sentinel values like `<synthetic>` should be dropped, not called.
      - OpenRouter slugs (`anthropic/claude-opus-4-7`) are namespaced;
        the native-provider call paths want bare ids — strip the prefix.

    Returns None for unsalvageable ids (synthetic, empty, malformed).
    """
    if not model_id:
        return None
    id_ = model_id.strip()
    if not id_ or id_.startswith("<") or id_ == "null":
        return None
    # Strip provider-namespace prefix — native (OAuth) endpoints expect
    # bare ids. OpenRouter routing reattaches the namespace via
    # model_id_for_provider when needed.
    if "/" in id_:
        id_ = id_.split("/", 1)[1]
    # Anthropic dateless ids use dashes between version digits
    # (`claude-opus-4-7`). Convert any dot-version form to that.
    if "claude-" in id_ and "." in id_:
        id_ = id_.replace(".", "-")
    # Some older CC builds wrote version-first word order
    # (`claude-4-5-haiku-20251001`). Anthropic flipped the convention
    # starting with Claude 4: family comes BEFORE version digits, so the
    # canonical form is `claude-haiku-4-5-20251001`. Pre-4 generations
    # (3, 3.5) kept family-at-end and remain canonical (`claude-3-5-
    # sonnet-20241022`), so the swap is gated on the major version >= 4.
    parts = id_.split("-")
    if (
        len(parts) >= 4
        and parts[0] == "claude"
        and parts[1].isdigit()
        and parts[2].isdigit()
        and parts[3] in {"opus", "sonnet", "haiku"}
        and int(parts[1]) >= 4
    ):
        # claude / v / v / fam / [rest...] → claude / fam / v / v / [rest...]
        id_ = "-".join([parts[0], parts[3], parts[1], parts[2]] + parts[4:])
    return id_


def provider_family_for_model(model_id: str, *, harness: str | None = None) -> str:
    """Extract the provider family from a namespaced model id, falling back
    to the harness's default family for bare names.

    Namespaced (``anthropic/claude-opus-4-7``) is what every adapter
    writes for openrouter-routed traffic.  Codex's native pricing path
    uses bare ids (``gpt-5.5``); for those we lean on the harness default.
    """
    if "/" in model_id:
        return model_id.split("/", 1)[0].lower()
    if harness and harness in HARNESS_DEFAULT_FAMILY:
        return HARNESS_DEFAULT_FAMILY[harness]
    # Heuristic bare-id fallback.  Keep narrow so we surface unknowns
    # rather than guessing badly.
    lower = model_id.lower()
    if "gpt-" in lower or lower.startswith(("o3", "o4", "o5", "o6")):
        return "openai"
    if "claude" in lower or lower.startswith("opus") or lower.startswith("sonnet") or lower.startswith("haiku"):
        return "anthropic"
    return "unknown"


def provider_available_models(provider: str) -> list[str]:
    """Authoritative model list for `provider`, or [] if discovery fails.

    Currently implemented for the two OAuth-sub providers route uses
    (claude-pro hitting Anthropic's /v1/models, chatgpt hitting Codex's
    /models). Other providers return [] which means "fall back to corpus
    discovery for them". Cached per-process via lru_cache so a route run
    only hits the catalog endpoints once even when multiple harnesses
    share a provider.
    """
    return _provider_available_models_cached(provider)


@lru_cache(maxsize=8)
def _provider_available_models_cached(provider: str) -> tuple[str, ...]:
    try:
        if provider == "claude-pro":
            from watchmen.providers import ClaudePro
            return tuple(ClaudePro().list_available_models())
        if provider == "chatgpt":
            from watchmen.providers import ChatGPT
            return tuple(ChatGPT().list_available_models())
    except Exception:
        return ()
    return ()


def candidates_for_harness(
    harness: str,
    current_model: str,
    *,
    project_key: str | None = None,
    user_candidates: Iterable[str] = (),
    since_days: int = DEFAULT_CANDIDATE_SINCE_DAYS,
) -> list[str]:
    """Discover the candidate pool for this harness.

    Three sources, in priority order:

      1. **Authoritative provider catalog.** Claude-pro's /v1/models +
         chatgpt's /models endpoint return the model ids the user's
         credentials can *actually call*. When available, this list is
         the ground truth: corpus ids get normalized + intersected with
         it, dropping anything that doesn't appear (retired models,
         display-string artifacts, cross-provider leakage).
      2. **Corpus history.** Distinct models the user has run in this
         harness, normalized via `_canonicalize_corpus_model_id`. Acts
         as an ordering hint (recent + frequent first) and as fallback
         when the catalog endpoint is unreachable.
      3. **User-supplied `--candidate` values.** Always appended, also
         normalized, never filtered by the catalog (so users can stress-
         test ids the catalog hasn't surfaced yet).

    The current model itself is always excluded from the pool.
    """
    provider = native_provider_for_harness(harness)
    catalog = list(provider_available_models(provider))
    catalog_set = set(catalog)

    # Build the seen-set against every form the current model could take,
    # so corpus + catalog + user ids all dedupe against it.
    cur_bare = current_model.split("/", 1)[1] if "/" in current_model else current_model
    cur_canon = _canonicalize_corpus_model_id(cur_bare) or cur_bare
    seen: set[str] = {current_model, cur_bare, cur_canon}

    pool: list[str] = []

    # Corpus history → canonicalized → filtered by catalog (when we have one).
    try:
        discovered = _corpus_models_for_harness(
            project_key=None, harness=harness, since_days=since_days,
        )
    except Exception:
        discovered = []
    for raw in discovered:
        canon = _canonicalize_corpus_model_id(raw)
        if canon is None or canon in seen:
            continue
        # If we have an authoritative catalog, require the canonicalized id
        # to be in it. Without a catalog, fall back to the provider-supports
        # heuristic to drop obvious cross-provider noise.
        if catalog_set:
            if canon not in catalog_set:
                continue
        else:
            if not provider_supports_model(canon, provider):
                continue
        seen.add(canon)
        pool.append(canon)

    # Backfill from the catalog with anything the corpus missed. This lets
    # users discover newer models they haven't yet tried (e.g. a freshly
    # released opus-4-7 they could swap to).
    for canon in catalog:
        if canon in seen:
            continue
        seen.add(canon)
        pool.append(canon)

    # User-supplied candidates always append, normalized but never filtered
    # by the catalog — useful for testing ids that don't show up yet (preview
    # access, beta gates, etc.).
    for cand in user_candidates:
        canon = _canonicalize_corpus_model_id(cand) or cand.strip()
        if not canon or canon in seen:
            continue
        seen.add(canon)
        pool.append(canon)

    return pool


def _corpus_models_for_harness(
    *, project_key: str | None, harness: str, since_days: int,
) -> list[str]:
    """Distinct models this harness ran, ordered by most-recent use first
    then session count.

    When ``project_key`` is None, queries globally — every project the
    user has touched with this harness.  Used by ``candidates_for_harness``
    for pool discovery: the user might be on project A today but they
    can swap to any model they've run on any project, since the auth
    sub gives them access to all of them.

    Subagent sessions are intentionally included (Task / thread_spawn
    subagents are real delegations the user spawned and care about),
    but synthetic and empty model_dominant values are filtered.
    """
    db = corpus_db_path()
    if not db.exists():
        return []
    cutoff = _iso_minus_days(since_days)
    where_parts = [
        "s.agent = ?",
        "s.model_dominant IS NOT NULL",
        "s.model_dominant != ''",
        "s.model_dominant != '<synthetic>'",
        "s.started_at >= ?",
    ]
    params: list[Any] = [harness, cutoff]
    if project_key is not None:
        pred = project_dir_predicate(project_key)
        if pred is None:
            return []
        where_clause, pred_params = pred
        where_parts.insert(0, where_clause)
        params = list(pred_params) + params
    where = " AND ".join(where_parts)
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            f"""
            SELECT s.model_dominant, MAX(s.started_at) AS last_seen, COUNT(*) AS n
            FROM sessions s
            WHERE {where}
            GROUP BY s.model_dominant
            ORDER BY last_seen DESC, n DESC
            """,
            tuple(params),
        ).fetchall()
    finally:
        conn.close()
    return [row[0] for row in rows]


# ─── Route execution: one compare run per harness ─────────────────────

def run_route(config: RouteConfig, *, progress=None) -> RouteResult:
    """Run a `compare` per detected (or user-supplied) harness.

    Each harness gets its own CompareResult, judged blind by the same
    judge model on the same skill evidence.  The reference for each
    compare is that harness's most-recent model; the candidate pool is
    the same provider family (so swaps are actually swappable in that
    harness).
    """
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = bundle_dir(config.project_key) / "_route" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    detected = detect_harnesses(
        config.project_key, since_days=config.since_days
    )
    references = _filter_or_seed_references(detected, config.harnesses)

    if not references:
        # Surface this cleanly to the CLI rather than silently no-op.
        _write_json(run_dir / "config.json", _config_to_dict(config))
        _write_json(run_dir / "references.json", [])
        return RouteResult(
            run_id=run_id,
            run_dir=str(run_dir),
            config=config,
            references=[],
            compare_results={},
            decisions=[],
        )

    _write_json(run_dir / "config.json", _config_to_dict(config))
    _write_json(
        run_dir / "references.json",
        [
            {
                "harness": r.harness,
                "current_model": r.current_model,
                "last_session_ts": r.last_session_ts,
                "session_count_window": r.session_count_window,
            }
            for r in references
        ],
    )

    # Pre-load evidence once.  Each per-harness compare needs the same
    # skill bucket; load_skill_bucket_evidence raises FileNotFoundError if
    # the bucket doesn't exist, which is exactly the error we want the
    # CLI to surface (with a "run watchmen curate first" hint).
    load_skill_bucket_evidence(config.project_key, config.bucket)

    compare_results: dict[str, CompareResult] = {}
    for ref in references:
        candidates = candidates_for_harness(
            ref.harness, ref.current_model,
            project_key=config.project_key,
            user_candidates=config.user_candidates,
            since_days=config.since_days * 3,  # widen for candidate discovery
        )
        if config.cross_harness:
            # Add other harnesses' current models to this harness's pool.
            # Marked separately by the rewriter so we can label the
            # `switch-harness` decision.
            for other in references:
                if other.harness == ref.harness:
                    continue
                if other.current_model not in candidates and other.current_model != ref.current_model:
                    candidates.append(other.current_model)

        if not candidates:
            if progress:
                progress(f"{ref.harness}: no candidate pool; skipping")
            continue

        harness_provider = native_provider_for_harness(ref.harness)
        cmp_cfg = CompareConfig(
            project_key=config.project_key,
            bucket=config.bucket,
            reference_model=model_id_for_provider(
                ref.current_model, harness_provider,
            ),
            judge_model=_default_judge_for_provider_oneshot(
                harness_provider,
                reference_model=ref.current_model,
                override=config.judge_model,
            ),
            candidates=[
                model_id_for_provider(c, harness_provider) for c in candidates
            ],
            task_count=config.task_count,
            reference_n=1,
            candidate_n=config.candidate_n,
            provider=harness_provider,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            generation_concurrency=config.generation_concurrency,
        )
        if progress:
            progress(
                f"{ref.harness}: ref={ref.current_model} vs "
                f"{len(candidates)} candidate(s)"
            )
        # Nest the per-harness compare run under our own dir so the
        # JSONL/MD artifacts stay co-located with the route result.
        sub_run_id = f"{run_id}_{ref.harness}"
        compare_results[ref.harness] = run_compare(
            cmp_cfg, run_id=sub_run_id, progress=progress
        )

    decisions = [
        classify_route(ref, compare_results.get(ref.harness), references)
        for ref in references
    ]

    _write_json(
        run_dir / "decisions.json",
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
            for d in decisions
        ],
    )

    return RouteResult(
        run_id=run_id,
        run_dir=str(run_dir),
        config=config,
        references=references,
        compare_results=compare_results,
        decisions=decisions,
    )


def _filter_or_seed_references(
    detected: list[HarnessReference],
    user_harnesses: list[str],
) -> list[HarnessReference]:
    """If the user passed --harnesses, narrow to that list.  Otherwise
    return everything detected.  A user-listed harness that wasn't
    detected (no sessions in window) is silently dropped — we don't
    invent a baseline."""
    if not user_harnesses:
        return detected
    wanted = {h.replace("-", "_") for h in user_harnesses}
    return [r for r in detected if r.harness in wanted]


# ─── Decision logic ──────────────────────────────────────────────────

def classify_route(
    ref: HarnessReference,
    compare_result: CompareResult | None,
    all_references: list[HarnessReference],
) -> RouteDecision:
    """Convert one harness's compare summary into a route decision.

    Quality-guard labels (``invalid``, ``unstable``, ``truncated``,
    ``dominated``) come directly from compare.  We only invent new ones
    when the compare result names a clear, replaceable winner — and we
    distinguish:

      - downshift: winner is in the same provider family, lower-cost
      - upshift: winner is in the same provider family, higher-cost
      - switch-harness: winner is another harness's current model
        (only meaningful when --cross-harness fed cross-harness candidates
        into the compare pool, since otherwise candidates_for_harness
        already restricted to same-family)
    """
    if compare_result is None:
        return RouteDecision(
            harness=ref.harness,
            current_model=ref.current_model,
            recommended_model=None,
            label="no-data",
            note="no candidate pool was available for this harness",
            avg_score=0.0,
            cost_vs_current=None,
        )

    summaries = compare_result.summaries
    ref_row = next((s for s in summaries if s.role == "reference"), None)
    candidates = [s for s in summaries if s.role == "candidate"]
    if not candidates or ref_row is None:
        return RouteDecision(
            harness=ref.harness,
            current_model=ref.current_model,
            recommended_model=None,
            label="no-data",
            note="compare produced no candidate rows",
            avg_score=ref_row.avg_score if ref_row else 0.0,
            cost_vs_current=None,
        )

    # Honour compare's own quality guards on the candidate pool: if every
    # candidate is `invalid` / `unstable` / `truncated` / `dominated`, we
    # bubble that label up rather than picking a damaged winner.
    healthy = [
        c for c in candidates
        if c.decision not in {"invalid output", "unstable output", "truncated output", "dominated"}
    ]
    if not healthy:
        worst = sorted(candidates, key=lambda c: c.decision)[0]
        return RouteDecision(
            harness=ref.harness,
            current_model=ref.current_model,
            recommended_model=None,
            label=worst.decision.replace(" output", "").replace(" ", "-"),
            note=worst.decision_note or "all candidates failed quality guards",
            avg_score=ref_row.avg_score,
            cost_vs_current=None,
            summary=worst,
        )

    # Pick the highest-quality healthy candidate.  Tie-break on cost.
    winner = sorted(
        healthy,
        key=lambda c: (-c.avg_score, c.cost_usd or 0.0),
    )[0]

    # If the winner isn't materially better than the reference (avg score
    # within 0.02), recommend `stay` — saves users from churn over noise.
    if winner.avg_score < ref_row.avg_score + 0.02 and (
        winner.cost_vs_reference is None or winner.cost_vs_reference >= 0.95
    ):
        return RouteDecision(
            harness=ref.harness,
            current_model=ref.current_model,
            recommended_model=None,
            label="stay",
            note=(
                f"best candidate {winner.model} scored "
                f"{winner.avg_score:.3f} vs ref {ref_row.avg_score:.3f}; "
                "not worth a swap"
            ),
            avg_score=ref_row.avg_score,
            cost_vs_current=winner.cost_vs_reference,
            summary=winner,
        )

    # cross-harness winner?  Match against the other harnesses' current
    # models.
    cross = next(
        (
            r for r in all_references
            if r.harness != ref.harness and r.current_model == winner.model
        ),
        None,
    )
    if cross is not None:
        return RouteDecision(
            harness=ref.harness,
            current_model=ref.current_model,
            recommended_model=winner.model,
            label="switch-harness",
            note=(
                f"another harness's current model ({cross.harness}'s "
                f"{winner.model}) outscores this harness's reference"
            ),
            avg_score=winner.avg_score,
            cost_vs_current=winner.cost_vs_reference,
            summary=winner,
        )

    cost_ratio = winner.cost_vs_reference
    if cost_ratio is not None and cost_ratio < 1.0:
        label = "downshift"
        note = f"cheaper at {cost_ratio:.2f}x with better quality"
    elif cost_ratio is not None and cost_ratio > 1.0:
        label = "upshift"
        note = f"pricier at {cost_ratio:.2f}x but higher quality"
    else:
        # Cost data missing.  Treat as upshift since the winner isn't
        # cheaper-known.
        label = "upshift"
        note = "higher quality; cost comparison unavailable"

    return RouteDecision(
        harness=ref.harness,
        current_model=ref.current_model,
        recommended_model=winner.model,
        label=label,
        note=note,
        avg_score=winner.avg_score,
        cost_vs_current=cost_ratio,
        summary=winner,
    )


# ─── Helpers ─────────────────────────────────────────────────────────

def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _config_to_dict(config: RouteConfig) -> dict[str, Any]:
    return {
        "project_key": config.project_key,
        "bucket": config.bucket,
        "harnesses": list(config.harnesses),
        "since_days": config.since_days,
        "cross_harness": config.cross_harness,
        "user_candidates": list(config.user_candidates),
        "task_count": config.task_count,
        "candidate_n": config.candidate_n,
        "judge_model": config.judge_model,
        "provider": config.provider,
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
        "generation_concurrency": config.generation_concurrency,
    }



def _default_judge_for_provider_oneshot(
    provider: str,
    *,
    reference_model: str,
    override: str | None = None,
) -> str:
    """Pick a judge model for this compare call.

    Default policy: judge with the **reference model** — the same model
    the user already runs day-to-day on this harness. Two reasons:

      1. **Quota alignment.** The user already pays for inference on the
         reference model (it's their daily driver). Using a separate
         "stronger" judge means burning quota on a model they don't
         actually run, which on subscription tiers (claude-pro, chatgpt)
         is often the model that's *most* quota-constrained.
      2. **Anchoring.** Scoring by the reference model anchors decisions
         in what the user already trusts. A judge they don't run wouldn't
         give them confidence to swap anything.

    `--judge` always wins when explicitly set (override is non-empty).
    """
    if override and override.strip():
        return override
    return model_id_for_provider(reference_model, provider)
