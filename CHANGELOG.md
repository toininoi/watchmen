# Changelog

All notable changes to watchmen are listed here. The CLI surfaces the latest
release notes once per version bump (CLI + web viewer) so a `git pull` is
never silent. Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

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
