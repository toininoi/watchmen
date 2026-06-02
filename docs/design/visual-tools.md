# Visual tools for exploring coding-agent usage

A living design spec for the viewer's exploration surfaces. The goal: let a
user *see* how they actually use their coding agents — the relationships
between repos, agents, sessions, and skills, and how often they repeat the
same mistakes — without resorting to a decorative graph hairball.

## Principles

- **Tufte over chartjunk.** Small multiples, sparklines, direct labelling,
  high data-ink ratio, and *always show the n*. A global force-directed graph
  of "everything" looks profound and reveals nothing; the signal lives in
  *specific* relations. (Most Obsidian users turn the global graph off — the
  useful idea there is navigable, linked entities + a local graph on demand,
  not the whole picture at once.)
- **No causation claims.** Any fired-vs-not / before-vs-after comparison is
  confounded (skills and hard tasks co-occur). Field names and verdicts say
  "association", matching `viewer/homepage.py:project_impact`.
- **Local-first, no deadweight.** Server-rendered HTML/SVG where it suffices;
  the viewer's existing ECharts only where interactivity earns its keep.
  Semantic indexing, when added, runs on a **local** embedding model — no data
  leaves the machine.

## Staged data model (additive, one column per tier)

```
Tier 0 (today): sessions, prompts, tool_calls(tool_name,is_error,skill_name,cost_usd),
                state.runs(skill-landing dates)
Tier 1: + tool_calls.error_signature TEXT   → friction ledger
Tier 2: + tool_calls.tool_target  TEXT       → file-level thrash / error maps
```

Error-signature normalizer (ingest-time, `adapters/_shared.py`): first non-empty
line → lowercase → paths→`<path>`, digits→`<n>`, hex/uuid→`<hex>`,
quoted→`<str>` → collapse + truncate. Collapses "the same mistake in different
words" for grouping.

## Build sequence

1. **Work matrix** — repo × agent grid. *(SHIPPED — `metrics.work_matrix`, on
   `/metrics`.)*
2. **Project swimlane** — sessions over time per repo, coloured by agent, with
   skill-landing markers. *(next)*
3. **`tool_calls.error_signature` + friction ledger** — recurring mistakes,
   ranked by recurrence × recency, with a `⚠ no skill` flag feeding the curator
   and prune-on-evidence. Thin `watchmen mistakes` CLI (the one spatial view
   that's also glanceable in a terminal).
4. **Local embeddings** → dedup error signatures, then the **prompt-intent map**
   (the one justified spatial scatter; needs the interactivity build decision).
5. `tool_calls.tool_target` + file-level edit-thrash — only if the coarse
   "repeated Edit calls" proxy proves people want it.

## Views

### 1. Work matrix (shipped)

> *Where does my work happen, and where does each agent struggle?*

`metrics.work_matrix(days, tracked_only, top_repos, metric)` → repos (rows) ×
agents (columns, ordered by session volume). Each cell carries sessions / cost
/ tokens / tool errors / `error_rate`. `metric` ∈ {sessions, cost, tokens,
errors} picks which one drives the colour ramp, the headline number, and the
row ranking; the viewer exposes it as `?matrix=` tab links (server re-renders,
no JS). The error rate prints below each cell **only when non-zero** (red
≥10%) so a clean, high-volume cell doesn't carry a misleading "0%" next to its
shading. Truncates to `top_repos` and *surfaces* it ("showing 20 busiest of
27").

```
                  Claude Code        Codex            pi.dev
   kai            ▇ 142   4.2%      ▃ 38   1.1%       ▁ 6    0.0%
   watchmen       ▅  88   6.8%      ▂ 21   9.0%       ·
   dria-sdk       ▂  23   2.1%      ▇ 96   3.3%       ▃ 30   5.0%
```

### 2. Project swimlane (shipped)

> *The story of one repo over time, with the skill-landing marker and a
> watchmen-usage overlay.*

`metrics.repo_swimlane(project_key, weeks, source_repo)` → one **daily lane per
agent** (busiest first) over a 16-week window; each `(agent, day)` cell carries
sessions / cost / tool errors / skill-fires. `repo_swimlane_svg()` renders it
server-side on `/p/{key}`: lane cells shade by session count in the agent's
colour, a red dashed `│` marks the first curator run (the same treatment date
as the Impact card, via a local `_treatment_day`), and an **amber dot** marks
each day a curated skill actually *fired* (`tool_calls.skill_name`) — so you
watch watchmen's output go from *landed* → *used*. Switches between lanes are
cross-agent **handoffs**. The before/after *outcome* numbers stay in the Impact
card above (no duplication).

```
  Claude Code  ▇▅ ·  ●▃        │            ▂●  ▁
  Codex        ·  ▃  ·   ▇▇    │skills          ▅
               Mar      Apr   landed      May
  cell shade = sessions · ● = a curated skill fired · │ = skills landed
```

### 3. Friction ledger (Tier 1)

> *Do I keep making the same mistake — and did a skill ever stop it?*

Group `tool_calls` where `is_error` by `error_signature`; per signature: a
weekly-frequency sparkline (reuse `metrics.sparkline_svg`), occurrences,
sessions, repos, recency. Overlay the skill-landing marker — does the curve
fall after? Severity is **labelled proxies only** (`turns-to-recover`,
amortized session-cost-share), never a fake per-error dollar figure (the
`cost_usd` column lives on skill rows, not error rows). A recurring signature
with no covering skill → `⚠` → curator candidate.

Companion (Tier 0, no schema change): **rephrase loops** — two adjacent user
prompts with zero tool calls between them, per session. High rephrase + high
error on one repo = that repo's `CLAUDE.md` brief is failing.

### 4. Prompt-intent map (Tier: local embeddings)

Embed user prompts with a local sentence-transformer, project to 2D, colour by
outcome (errored / expensive / led to a rephrase). The one place a spatial map
is justified — semantic space is inherently projectable. Also powers the
missed-uptake view (prompts that semantically match a skill's `when_to_use` but
never fired it).

## Open decisions

- Swimlane/ledger rendering: server SVG (chosen for v1, consistent with
  `card_svg`/heatmap builders) vs the viewer's ECharts (already a dependency).
  Lean SVG first; adopt ECharts only if a view genuinely needs pan/zoom/hover.
- `watchmen mistakes` CLI scope: ledger only (it's the glanceable one); matrix
  and swimlane stay viewer-only.
