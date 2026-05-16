# Changelog

All notable changes to watchmen are listed here. The CLI surfaces the latest
release notes once per version bump (CLI + web viewer) so a `git pull` is
never silent. Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Fixed — stale Claude Code hook entries from older watchmen installs
- `watchmen hooks install` is now self-healing: it scrubs any existing
  watchmen entry from `~/.claude/settings.json` (matched by script
  basename — `watchmen_observe.sh`, `watchmen_brief.sh`) before writing
  the canonical set fresh. Previously, both install and uninstall
  matched by exact absolute path, so when the package layout moved in
  0.1.x → 0.5.x (top-level `hooks/` → `src/watchmen/hooks/`) the old
  entries became invisible to the CLI and silently failed with "No
  such file or directory" on every PreToolUse / PostToolUse / etc.
- `watchmen hooks uninstall` shares the same basename-matching path
  now, so it fully detaches every watchmen-shipped hook regardless of
  which absolute path it was written with.
- Same fix retires the `_scrub_legacy_hooks` + `_LEGACY_HOOK_PATHS`
  surface; both got folded into the new `_scrub_watchmen_hooks` helper
  driven by `WATCHMEN_SCRIPT_NAMES` (every basename watchmen has ever
  shipped, including retired ones).

### Changed — agent-agnostic framing
- README, CLI help text, viewer copy, onboard wizard prompts, and curator LLM
  prompts no longer single out Claude Code where the behavior is multi-adapter.
  Strings that genuinely refer to data attribution (per-agent stats, hook
  installer steps that wire `~/.claude/settings.json`) keep their specific
  labels; everything else reads as "coding-agent session" / "coding-agent
  transcripts".
- `watchmen ingest` help text now reads "re-scan all coding-agent transcripts
  into corpus.db" — accurate, since the ingest path walks every adapter
  (`adapters/claude_code.py`, `adapters/codex.py`, `adapters/pi.py`), not
  just `~/.claude/projects/`.

### Added — `AGENTS.md` mirror alongside `CLAUDE.md`
- After Stage 3 of the curator writes `bundles/<project>/CLAUDE.md`, watchmen
  also writes an identical `bundles/<project>/AGENTS.md`. Codex reads
  `AGENTS.md`; Claude Code reads `CLAUDE.md`. Identical content, single source
  of truth, both agents pick up the same workspace brief without a
  per-project copy step.
- `_changelog.md` manifest tracking widened to follow `AGENTS.md` mtime
  alongside `CLAUDE.md` so regen events show up in `watchmen recent`.

### Fixed — stale `~/.watchmen/kai_claude/` directory
- The 0.5 rename `kai_claude → bundles` left some installs with both
  directories on disk (an older daemon recreated the alias after the
  migration ran, or a stale checkout copy lingered). On next import,
  `runtime_path` now archives a coexisting `kai_claude/` to
  `kai_claude.legacy/` once and prints a one-line stderr notice. Idempotent
  — the rename is self-deleting, so subsequent runs are silent.

### Added — per-coding-agent metric surfaces
- New `metrics.adapter_breakdown_all(days, tracked_only)` aggregator
  rolls up sessions, projects, prompts, tool errors, and cost per `agent`
  column in `corpus.db.sessions`. Stable order (sessions desc, agent
  alpha) so the surface is deterministic across CLI + viewer.
- Viewer's `/metrics` page gains a "By coding agent (last 30 days)"
  section: friendly labels ("Claude Code", "Codex", "pi.dev"), bar chart
  for relative session volume, columns for projects / prompts / tool
  errors / cost USD. Section hides itself on empty corpora.
- CLI's `watchmen metrics` table renamed from "Sessions by adapter" to
  "By coding agent — last Nd" and gains columns for prompts, tool errors,
  and cost USD on top of the existing sessions / share / projects.
- New `ADAPTER_LABELS` mapping + `adapter_label(slug)` helper exported
  from `metrics.py` so every UI surface can render friendly names
  consistently and unknown slugs fall through verbatim.

### Changed — viewer aesthetic upgrade (Tailwind via CDN, shadcn-inspired)
- Switched the viewer's body font from system fonts to Inter (loaded via
  Bunny Fonts, a GDPR-safe Google Fonts mirror). Sets a consistent visual
  baseline across macOS, Linux, and Windows.
- Introduced a small design-token layer in `base.html` (CSS custom
  properties for `--background` / `--foreground` / `--card` / `--border`
  / `--accent`) so future dark-mode + theme variants only need to swap
  the HSL triples.
- New utility classes: `.wm-card` (the polished card baseline), `.wm-card-table`
  (a card that hosts a flush table with no edge padding), `.wm-pill` +
  `.wm-pill-ok`/`-running`/`-fail`/`-muted` (shadcn-style status badges),
  `.wm-nav-link` (pill-style nav with active-route highlight via
  `request.url.path`). Existing `.stat-card` rules promoted to use the
  same tokens so legacy templates pick up the upgrade without rewriting.
- Templates migrated to the new primitives: dashboard, runs, insights,
  metrics_all. Per-template `<style>` blocks that duplicated the global
  card baseline got pruned; only page-specific bits stay local
  (`.daily` table on metrics, `.badge-curated`/`.badge-candidate` on
  insights, etc.).
- Nav now highlights the active section. Bigger border radius on cards
  (12px → "rounded-card") + subtler shadows + a smoother hover state
  on table rows pull the look closer to shadcn's default light theme
  without dragging in a Node build step or React.

### Changed — sharpened the Metrics vs Insights split
- `/insights` is now the LLM-driven view only: cross-repo digest,
  friction signals, untapped corpora, frustration samples, deep digest
  cache. Raw aggregations moved out.
- Removed from `/insights` (now live exclusively on `/metrics`):
  the 4 stat tiles (sessions / tool errors / frustration / cost), the
  adapter mix card, and the skill bundles count card. A top-of-page
  link `↗ aggregated metrics` points users to the raw breakdown.
- Repos table, friction signal charts, and the hour-of-day heatmap
  remain on `/insights` for LLM-context — they back the deep digest.

## [0.5.0] — 2026-05-15

First PyPI release. This rolls up everything since 0.4.0 — Linux daemon
backend, hardened internals, pytest migration, and 30 new tests filling
the biggest coverage holes. The CLI surface is unchanged; existing users
upgrade in place.

Install: `uv tool install dria-watchmen` (or `pip install dria-watchmen`).
The CLI binary is still `watchmen` and the import path is still
`from watchmen import ...` — only the package name on PyPI is
`dria-watchmen` (the plain `watchmen` namespace was already claimed).

### Added — Linux daemon backend (systemd --user)
- `watchmen daemon install` / `watchmen viewer install` now work on Linux,
  writing systemd `--user` units to `~/.config/systemd/user/` and calling
  `systemctl --user enable --now`. macOS continues to use launchd; the CLI
  is identical on either platform.
- New `watchmen.service` module dispatches to the right backend
  (`launchd_setup` on Darwin, `systemd_setup` on Linux) based on
  `platform.system()`. Status rows show "(launchd)" or "(systemd)"
  contextually. `BACKEND_NAME` is exported for UI strings.
- On Linux, install output prints `loginctl enable-linger $USER` as the
  one-time setup if you want the daemon to run after logout (otherwise
  user units stop with the session, matching launchd's user-agent
  semantics).
- Linux logs go to `~/.watchmen/logs/{daemon,viewer}.{out,err}.log` (also
  available via `journalctl --user -u watchmen-daemon.service`).
- `--dry-run` for both backends now skips all preflight + filesystem
  side effects so you can audit the generated unit/plist on any platform.

### Added — pytest migration + adapter / agent coverage
- Test suite migrated from a hand-rolled `def check()` driver in
  `tests/smoke.py` to standard pytest discovery. `pytest tests/` is now
  the canonical entry point; existing `def test_*` functions kept their
  bodies — only the driver and module-level boilerplate moved out.
- `tests/conftest.py` owns the `src/` `sys.path` nudge + exports
  `ROOT` / `SRC` constants so individual tests can read package files.
- 30 new tests added across two previously zero-coverage modules:
  - `tests/test_adapter_claude_code.py` (11 tests) — exercises the
    Claude Code transcript parser with synthetic JSONL: user-prompt
    extraction, tool_use accounting, tool_error counting, cache-bucket
    handling, malformed-line tolerance, subagent threading.
  - `tests/test_agent.py` (19 tests) — fully mocked OpenRouter loop.
    Covers `load_api_key` (env → file → raise), `_backoff_seconds`
    (exponential + Retry-After floor), `call_openrouter` retry policy
    (429 / 500 retry, 400 fail-fast, RequestError exhaustion), Agent
    attribution headers, terminal-tool dispatch, non-terminal handler
    dispatch, `max_cost_usd` budget ceiling, max_iter exhaustion.
- CI workflow renamed `smoke` → `tests`; runs `pytest tests/ -q` across
  ubuntu × macos × py3.11/3.12 with `WATCHMEN_HOME` redirected to a temp
  dir and `OPENROUTER_API_KEY` explicitly unset so the api-key-resolution
  tests exercise their raise branch on a CI secret-free environment.
- `pytest>=8.0` + `pytest-cov>=5.0` added as `[project.optional-dependencies].dev`.
  Install once via `uv sync --extra dev`.

### Added — release plumbing
- `.github/workflows/release.yml` fires on `v*.*.*` tag pushes. Builds
  wheel + sdist, smoke-tests the wheel in a fresh venv, attaches both to
  a GitHub Release with auto-generated notes, and (when the
  `PUBLISH_TO_PYPI` repo variable is `true`) publishes to PyPI via OIDC
  trusted publishing — no API tokens stored anywhere.
- Added "Releasing" section to CONTRIBUTING.md documenting the tag flow.

### Changed
- README + CONTRIBUTING updated with the new `pytest tests/` workflow,
  pytest layout, `--cov` invocation, and cross-platform daemon notes.

## [0.4.0] — 2026-05-13

### Added — HTML insights viewer page (`/insights`)
- New page at `http://127.0.0.1:8979/insights` mirroring `watchmen
  insights` with the richer stats + charts that don't fit a terminal:
  - 4 headline tiles (sessions / tool errors / frustration markers /
    cost, each with a 30-day sparkline).
  - Adapter mix + skill-bundle coverage at a glance.
  - Per-repo table with skills, pending bundles, 30-day activity
    sparkline, adapter pills, tool-error count, frustration count,
    unanalyzed prompts.
  - Two horizontal bar charts: tool errors by repo + frustration
    markers by repo (server-rendered SVG, same aesthetic as the
    existing metrics page).
  - Cross-repo patterns table (slug × repos with ✓ curated / · candidate
    badges) and untapped corpora list.
  - Collapsible frustration samples — actual quoted prompts per repo,
    pulled straight from corpus.db.
  - Hour-of-day × day-of-week heatmap from `metrics.activity_by_hour_dow_all`.
  - The latest cached deep digest from `~/.watchmen/insights/` rendered
    as markdown (same content as `watchmen insights --view`).
- New nav link "Insights" in the viewer header, visible from every page.
- `metrics.hbar_chart_svg(rows, …)` reusable horizontal-bar SVG helper
  (XML-escaped labels, empty-input safe).

### Fixed
- Removed stale hardcoded `127.0.0.1:8888` from `base.html` footer —
  shows a generic "local viewer" label now (the actual port is whatever
  `watchmen settings port` is set to).

## [0.3.0] — 2026-05-13

### Added — harness-aware curator
- The candidate-finder now reads the user's installed Claude Code skills
  from `~/.claude/skills/*/SKILL.md` and is instructed to (a) prefer
  proposing an **enhancement** of an existing skill over a brand-new
  bundle when the trigger overlaps, and (b) compose existing skills
  rather than reinventing them. Each candidate may carry an optional
  `enhancement_of: <slug>` field that flows to Stage 2.
- New `harness.py` module: `installed_skills()`, `overlaps_existing()`,
  `format_for_prompt()`. Tiny surface area; tolerant of missing or
  malformed `SKILL.md` files.

### Added — conservative candidate prompt
- The finder prompt now requires a **recurring** pattern (within a
  single session OR across multiple sessions) before promoting any
  candidate to a skill. No numeric thresholds — the LLM stays in
  judgment mode, but with a clear "be ruthless, don't ship marginal
  bundles" instruction.

### Added — approval queue (`approval_required` setting)
- New per-project setting `approval_required: 0/1` (default 0 = autonomy
  preserved). When 1, **new** skill bundles route to
  `kai_claude/<project>/_pending/<slug>/` instead of
  `kai_claude/<project>/skills/<slug>/`. Already-approved skills in
  `skills/` keep updating in place — only first-time additions are gated.
- `watchmen review` now walks the pending queue first with
  `(a)pprove / (d)rop / (s)kip / (v)iew / (q)uit` semantics before
  continuing to the existing approved-skills walk. Approving moves
  `_pending/<slug>/` → `skills/<slug>/` (existing approved bundles are
  backed up to `<slug>.superseded/` for manual undo).
- New per-project setting `skip_overlapping_skills: 0/1` (default 0).
  When 1, candidates that overlap with installed harness skills are
  dropped entirely (no enhancement proposal).

### Added — CLI surface
- `watchmen curate <p> --skip-overlap` overrides the per-project setting.
- `watchmen curate <p> --approval-required` overrides the per-project setting.
- `watchmen settings set <p> approval_required true|false`
- `watchmen settings set <p> skip_overlapping_skills true|false`
- `watchmen settings show <p>` displays both new settings.

### Fixed
- `state.init_db()` migrates legacy `projects` rows to include the two
  new boolean columns (`approval_required`, `skip_overlapping_skills`).
  Pull + rerun applies the migration automatically; no manual ALTER
  TABLE needed.

## [0.2.0] — 2026-05-13

### Added
- **Release notes on first run after a version bump.** The CLI prints a
  compact "what's new in vX.Y.Z" block on the first invocation after a
  pull; the web viewer dashboard shows the same banner with a dismiss
  button. Last-seen version is tracked at `~/.watchmen/last_seen_version`
  (CLI) and `localStorage["watchmen.lastSeenVersion"]` (viewer).
- New `watchmen changelog` command renders the full CHANGELOG.md anytime.

### Removed
- **macOS notification briefs.** Deleted `brief.py` and
  `hooks/watchmen_brief.sh` along with the SessionStart hook entry that
  fired the osascript popup on every Claude Code session start. The
  surfaces that remain are intentional and non-intrusive:
  - `💡 watchmen` statusLine indicator (passive, when curator has news)
  - `/watchmen:brief` plugin skill (user-invoked, never auto)
- `watchmen hooks install` and `watchmen hooks uninstall` both scrub
  stale `watchmen_brief.sh` references from existing `settings.json`
  files automatically — no manual JSON editing needed.

## [0.1.1] — 2026-05-13

### Fixed
- **Auto-migrate `agent` column on every CLI startup.** Users with
  `corpus.db` files predating multi-adapter support (Codex + pi.dev) no
  longer hit `OperationalError: no such column: agent` on
  `watchmen insights`. Pull + rerun now picks up schema changes
  automatically; no `watchmen ingest --full` required.

## [0.1.0] — 2026-05-12

### Added
- `watchmen insights` — cross-repo deep digest. Static aggregation (repo
  table with sparklines, cross-repo candidate-slug overlaps, untapped
  corpora, friction signals from corpus.db) plus a 2-stage LLM pipeline
  (parallel per-repo synthesis → cross-repo digest) with view/regenerate
  caching at `~/.watchmen/insights/`.
- `README` section comparing watchmen to Claude Code's native `/insights`
  command — positions watchmen as the multi-adapter, persistent,
  per-repo layer.
- Friction signal helpers: tool-error counts and
  frustration-marker prompt regex (`no wait / bruh / :( / wtf / nope / …`)
  surface as a static row and feed the LLM pipeline.
- Watchmen aesthetic for `insights`: `◷` banner, `◆` section glyphs,
  `◉` deep-digest header, closing tagline rotating from a Manhattan /
  Rorschach / Veidt canon pool.
