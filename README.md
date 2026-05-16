# watchmen

Local coding-agent session intelligence. Observes your Claude Code, Codex, and pi.dev sessions, analyzes how you actually use them over time, and auto-generates skill bundles + a workspace brief (`CLAUDE.md` for Claude Code, mirrored to `AGENTS.md` for Codex) per project — all on your machine, no cloud.

The premise: every coding-agent session leaves a transcript on disk (`~/.claude/projects/` for Claude Code, `~/.codex/sessions/` for Codex, pi.dev's session export). Across weeks of work this becomes a corpus of how you actually use those agents. watchmen mines that corpus, runs a longitudinal LLM analyst day by day with a carry-forward thesis, then curates the recurring procedural patterns into runnable skill bundles plus a workspace brief for each repo (`CLAUDE.md` + `AGENTS.md`, identical content).

Runs continuously via the host's native scheduler — launchd on macOS, systemd --user on Linux, Task Scheduler on Windows. Comes with a local web viewer at `http://127.0.0.1:8979` (changeable via `watchmen settings port <N>`).

## What you get

For each tracked repo, watchmen produces under `bundles/<repo>/`:

```
CLAUDE.md                # 12-section workspace brief auto-generated from session evidence
AGENTS.md                # identical mirror of CLAUDE.md so Codex picks up the same brief
skills/
  <skill-name>/
    SKILL.md             # frontmatter + trigger phrases + procedure + examples
    scripts/             # actual runnable Python/bash extracted from your repo
    references/          # supporting docs
_curation_log.md         # agent's decisions + critic feedback
_candidates.json         # which skills were considered, which got built
_index.md                # summary of generated artifacts
```

Plus per-project `analyses/<repo>/`:

```
2026-04-09.md            # day-by-day thesis snapshots
2026-04-10.md            # each refines the running thesis with that day's sessions
...
_running.md              # latest aggregated thesis
```

## vs `/insights` — how watchmen differs

Claude Code shipped a native `/insights` command in v2.1.117 (Apr 2026) that produces an LLM-narrated HTML report from your `~/.claude/projects/` transcripts. It's good. watchmen is **complementary**, not a replacement:

| | `/insights` | watchmen |
|---|---|---|
| **Output** | One-shot HTML report (read once, close tab) | Git-tracked skill bundles + CLAUDE.md (load into next session) |
| **Adapters** | Claude Code transcripts only | cc + cd (Codex) + pi (pi.dev) |
| **Scope** | Global, flat aggregate | Per-project bundles + cross-repo digest |
| **Cadence** | On-demand, manual | Cron + statusLine brief on next session |
| **Provenance** | LLM narrates, no traceable source | `watchmen why <skill>` → source sessions, curator log, adapter tags |
| **Curation** | LLM proposes ~5 CLAUDE.md rules globally | Rule-based extraction → reusable skill markdown per project |
| **Privacy** | LLM call to Anthropic on full corpus | Local-only by default (LLM only during scheduled curator runs, via your OpenRouter key) |
| **Persistence** | Report file (regenerate to refresh) | `_curation_log.md`, `_candidates.json`, diff against `last_commit` |
| **Coverage view** | "1,108 messages, 41 sessions" globally | Per-repo: skills count, candidates not promoted, stale runs |

**One-line pitch**: `/insights` *narrates your whole history once*; watchmen *operationalizes it across every adapter and every repo, continuously, with traceable artifacts.*

Run both. They cover different jobs:

```bash
/insights                       # in Claude Code — LLM-narrated overview
watchmen insights               # static aggregation + view-or-regenerate prompt
watchmen insights --regenerate  # force a fresh deep digest (skip prompt)
watchmen insights --view        # render the latest cached digest (skip prompt)
watchmen insights --list        # list every cached digest in ~/.watchmen/insights/
watchmen insights --no-llm      # static aggregation only, no API call (instant)
```

The same digest is also available as an HTML page in the local viewer at `http://127.0.0.1:8979/insights` — same data, richer charts (sparklines, horizontal bars, hour-of-day heatmap, deep-digest markdown rendered with code highlighting). The CLI is the regenerate path; the viewer is the read-and-share path.

`watchmen insights` is a two-stage pipeline mirroring the analyst/curator architecture:

1. **Static aggregation** (instant, no LLM): repo table with skills/adapter/pending/activity sparkline, cross-repo candidate-slug overlaps, untapped corpora.
2. **Stage 1 — per-repo synthesis** (parallel): for each repo with a thesis on disk, read `analyses/<repo>/_running.md` + `bundles/<repo>/_curation_log.md` + `_candidates.json` and produce a structured per-repo summary (themes / friction / user signals / skill gaps).
3. **Stage 2 — cross-repo synthesis**: feeds the Stage 1 outputs + static facts into a final markdown digest with 5 sections — themes across your work, friction patterns, skill gaps, underused capabilities (CLAUDE.md rules / hooks / MCP), concrete next moves with copy-pastable commands.

Each run is cached at `~/.watchmen/insights/<timestamp>.md` with YAML frontmatter (model + repos synthesized + timestamp). When a cached digest exists, `watchmen insights` shows the latest run's age and prompts `(v)iew · (r)egenerate · (q)uit`. Non-interactive contexts (piped, CI) default to view so a script can't silently spend API credit.

Cost: ~$0.05-0.10 per regeneration with deepseek-flash. Time: ~30-60s depending on how many repos have a thesis. `--no-llm` skips both stages entirely.

## Requirements

- macOS, Linux, or Windows 10/11
- [`uv`](https://github.com/astral-sh/uv) (Python toolchain)
- Python 3.11+
- An OpenRouter API key (`OPENROUTER_API_KEY`)
- At least one supported coding agent in active use — Claude Code (`~/.claude/projects/`), Codex (`~/.codex/sessions/`), or pi.dev — for there to be a session corpus to mine

Models: defaults to `deepseek/deepseek-v4-flash` (fast, cheap, capable of multi-turn tool calling). Configurable per command.

## Install — for the team

Three commands and a wizard. Total wall time ~10 min + 30–90 min per project of LLM passes.

```bash
git clone https://github.com/firstbatchxyz/watchmen.git
cd watchmen
uv sync && uv tool install --editable .
watchmen init
```

The wizard handles everything: prompts for your `OPENROUTER_API_KEY` (saves to `~/.config/watchmen/.env`, chmod 600), ingests your `~/.claude/projects/` history, lets you pick which projects to analyze, previews the cost, runs analyze + curate with live progress, installs the daemon + viewer into the host's scheduler for autostart, and shows you the exact `/plugin` commands to paste inside Claude Code.

Runtime data lives under `~/.watchmen/` by default (`state.db`, `corpus.db`, `analyses/`, `bundles/`, event logs). Set `WATCHMEN_HOME=/path/to/dir` if you need an alternate data directory for testing or a separate install. On first use, watchmen copies any legacy source-tree runtime files into the new location so existing local data is preserved.

If anything looks wrong afterwards, `watchmen doctor` does a one-screen ✓/✗ check across API key, corpus, daemon, viewer, and hooks.

When the wizard finishes, install the plugin inside any Claude Code session:

```
/plugin marketplace add firstbatchxyz/watchmen
/plugin install watchmen@watchmen
/reload-plugins
```

Then wire the in-TUI indicator (one-time, picks up the newest plugin version automatically):

```bash
watchmen statusline install
```

That's the whole install. The rest of this README is for understanding what's happening + manual control.

### API key — manual options (the wizard handles this for you)

```bash
# Option A — env var
export OPENROUTER_API_KEY=sk-or-v1-...

# Option B — global config (what the wizard writes)
mkdir -p ~/.config/watchmen && echo "OPENROUTER_API_KEY=sk-or-v1-..." > ~/.config/watchmen/.env

# Option C — repo-local .env
echo "OPENROUTER_API_KEY=sk-or-v1-..." > .env
```

### After we push a plugin update

```bash
watchmen plugin update               # git pulls the marketplace clone
# then inside Claude Code:
/plugin uninstall watchmen@watchmen
/plugin install watchmen@watchmen
/reload-plugins
watchmen statusline install          # refresh the version in the statusLine path
```

## Quickstart (manual)

If you'd rather drive the steps by hand instead of via the wizard:

```bash
# 1. Wire watchmen into your Claude Code hook config (live event capture)
watchmen hooks install

# 2. Start the hook server in a long-lived terminal (captures every session event)
uv run python -m watchmen.server
# Leave this running. Or run in tmux/screen.

# 3. In another terminal: build the historical corpus
watchmen ingest
watchmen list                                # see auto-detected projects

# 4. Track a project (start with one of your active repos)
watchmen track my-project --repo /path/to/repo

# 5. Run the pipeline
watchmen analyze my-project                  # longitudinal LLM analyst, day-by-day
watchmen curate my-project                   # skill bundles + CLAUDE.md

# 6. Browse the output
watchmen viewer run                          # http://127.0.0.1:8979
```

Outputs land in `bundles/my-project/` and `analyses/my-project/`. Both are gitignored — they're your data, not the source.

## The Claude Code plugin

Once installed (`/plugin install watchmen@watchmen` after `/plugin marketplace add firstbatchxyz/watchmen`), the plugin gives you:

- **`/watchmen:brief`** — pull the latest curator state for your current workspace. Claude reads what changed since the last run (new skills, CLAUDE.md updates), summarizes it, and asks if you want to load a suggested skill. Your decision; nothing auto-loads.
- **`💡 watchmen` statusLine indicator** — appears bottom-right of the Claude Code TUI when there's something new for the project you're in. Acknowledges itself when you invoke `/watchmen:brief`.
- **In-flight skill suggestions** — every prompt you submit gets matched against your project's skill index (FTS5, sub-millisecond). If a strong match exists, the statusLine refreshes after the assistant's response with "💡 you could have used /<skill> to save time & tokens on this task". Retrospective hint; no agent context injection.
- **Diff view in the viewer** — every curator run becomes a git commit in `bundles/<project>/`. The viewer (`http://127.0.0.1:8979/p/<project>/runs`) shows a side-by-side diff per run, GitHub-style.

The plugin reads `~/.watchmen/state/<project>.json` (written by the engine at end of every curator run) and `~/.watchmen/projects.json` (index of tracked projects). It never reaches into the engine's install dir.

## Run it continuously

To run watchmen autonomously (incremental analyzer + auto-regen of CLAUDE.md when new prompts come in):

```bash
watchmen daemon install                      # autostart on login, keepalive
watchmen viewer install                      # also autostart the viewer at :8979
watchmen launchd status                      # verify (also reports systemd --user / Task Scheduler state)
```

Same CLI on every platform; under the hood it installs a launchd agent on macOS, a systemd --user unit on Linux, or a Task Scheduler task on Windows. On Linux, run `sudo loginctl enable-linger $USER` once if you want the daemon to keep running after you log out.

Default cadence:

| What | When |
|---|---|
| Re-ingest all coding-agent transcripts (Claude Code / Codex / pi.dev) + incremental analyst | Every **2 hours** |
| `CLAUDE.md` regen (stage 3 only, light) | After an analyst run if last regen >24h ago |
| **Full curator** (skill bundles + CLAUDE.md, expensive) | **02:00 and 14:00 local time**, min 8h between runs per project |

The analyst check is cheap — usually a no-op when there's nothing new. The full curator runs twice a day at 2am and 2pm to refresh skill bundles + the full CLAUDE.md. You can override these defaults via flags on `watchmen daemon` (see `--curator-hours`, `--interval`, `--full-curator-min-age`).

Logs:

```
# macOS (launchd)
~/Library/Logs/watchmen.log                          # primary daemon log
~/Library/Logs/watchmen.daemon.{out,err}.log         # launchd stdout/stderr
~/Library/Logs/watchmen.viewer.{out,err}.log         # viewer logs

# Linux (systemd --user)
~/.watchmen/logs/daemon.{out,err}.log                # systemd stdout/stderr
~/.watchmen/logs/viewer.{out,err}.log                # viewer logs
# also: `journalctl --user -u watchmen-daemon.service`

# Windows (Task Scheduler)
%LOCALAPPDATA%\watchmen\logs\watchmen.log            # primary daemon log
%LOCALAPPDATA%\watchmen\logs\daemon.{out,err}.log    # scheduler stdout/stderr
%LOCALAPPDATA%\watchmen\logs\viewer.{out,err}.log    # viewer logs
```

To stop:

```bash
watchmen daemon uninstall
watchmen viewer uninstall
watchmen hooks uninstall
```

## Command reference

Run `watchmen --help` for the grouped overview; `watchmen <command> -h` for per-command flags.

```
# Get started
watchmen init                    Interactive setup wizard (alias: onboard)
watchmen doctor                  One-screen ✓/✗ check of API key, corpus, services
watchmen settings api-key        Set or check the OpenRouter key (live-validated)
watchmen settings port [N]       Get or set the viewer port (default 8979)

# Pipeline
watchmen status                  Dashboard view of tracked projects
watchmen analyze <key>           Run analyst (incremental, only new days)
watchmen analyze <key> --full    Full re-run (ignores prior thesis)
watchmen curate <key>            Full curator: candidates → skills → CLAUDE.md
watchmen curate <key> --regen-claude    Stage 3 only (regenerate CLAUDE.md)
watchmen runs                    Recent run history
watchmen metrics                 Global rollup across all projects + adapter breakdown
watchmen metrics <key>           Daily token/cost/uptake rollup for one project

# Project inventory
watchmen list                    Auto-detect projects from corpus
watchmen track <key> --repo <path>
watchmen ingest                  Re-scan ~/.claude/projects → corpus.db
watchmen sync                    Bootstrap state from on-disk artifacts (no LLM calls)

# Inspect (terminal-native — web viewer is optional)
watchmen show                    List every curated project + skill count
watchmen show <key>              List a project's artifacts (CLAUDE.md, skills, _curation_log.md)
watchmen show <key> <skill|file> Dump a single SKILL.md or any project artifact
watchmen why <key> <skill>       Provenance: source sessions (w/ adapter), curator rationale
watchmen recent [<key>]          Git log of curator runs (last 7d by default)
watchmen insights                Cross-repo digest — pairs with Anthropic's /insights
watchmen open [<key>]            Open viewer in browser (jumps to project page)
watchmen logs [daemon|viewer]    Tail scheduler logs (-f to follow)

# Control (steer the curator)
watchmen pin <key> <skill>       Freeze a skill — next curator run skips it
watchmen unpin <key> <skill>     Remove a skill from the pin list
watchmen drop <key> <skill>      Remove bundle + blocklist the slug
watchmen restore <key> <skill>   Allow a blocked slug to be re-proposed
watchmen curate <key> --skip-overlap        Drop candidates that overlap with installed harness skills
watchmen curate <key> --approval-required   Route new bundles to _pending/ for review
watchmen settings set <key> approval_required true       Default new bundles to _pending/
watchmen settings set <key> skip_overlapping_skills true Default --skip-overlap on
watchmen learn <key>             Fast cycle: analyze + CLAUDE.md refresh (~$0.50)
watchmen learn <key> --full      Same, but with full curator (Stage 1+2+3)
watchmen review <key>            Interactive walk: pending (a/d/s/v/q) then approved (k/d/p/s/v/q)

# Background services
watchmen daemon run              Run scheduling loop in foreground
watchmen daemon run --once       Single cycle, then exit (testing)
watchmen viewer run              FastAPI viewer in foreground
watchmen {hooks,daemon,viewer,statusline} install
watchmen {hooks,daemon,viewer,statusline} uninstall
```

## Steering the curator

The curator is autonomous by default — every 12 hours it re-proposes and re-curates skill bundles based on the latest analyst thesis. For most users that's fine. When it isn't:

- **`watchmen pin <project> <skill>`** — you've hand-edited a SKILL.md and want it preserved through future runs. The curator treats pinned skills as forced cache hits and skips its per-skill agent for them.
- **`watchmen drop <project> <skill>`** — the curator keeps proposing a skill you don't want. Drop removes the bundle dir AND adds the slug to `_blocklist.json`. The candidate finder still proposes whatever it wants, but `curate.py` filters its output against the blocklist before Stage 2 — so dropped slugs stay gone.
- **`watchmen unpin` / `watchmen restore`** — reverse either decision.

State lives in `bundles/<project>/_pinned.json` and `_blocklist.json` — JSON lists of slugs. Empty lists delete the file. Both are git-tracked inside the project bundle, so pin/drop state survives across machines if you sync `bundles/` somewhere.

### Harness awareness

By default the candidate finder reads `~/.claude/skills/*/SKILL.md` and is instructed to (a) prefer proposing an **enhancement** of an existing harness skill when the trigger overlaps, and (b) compose existing skills rather than reinventing them. Each candidate may carry an optional `enhancement_of: <slug>` field — when set, Stage 2 prepends an ENHANCEMENT MODE preamble (with the existing SKILL.md content) to the per-skill curator's prompt so the new bundle is framed as a delta, not a duplicate.

If you'd rather drop overlapping candidates entirely (no enhancement, no proposal), set `skip_overlapping_skills` or pass `--skip-overlap` to a one-off run:

```bash
watchmen settings set kai-frontend skip_overlapping_skills true
# or one-off:
watchmen curate kai-frontend --skip-overlap
```

### Approval mode

The curator is autonomous by default. When you want a review gate before new bundles join the harness:

```bash
watchmen settings set kai-frontend approval_required true
# or one-off:
watchmen curate kai-frontend --approval-required
```

With `approval_required` on, **new** skill bundles route to `bundles/<project>/_pending/<slug>/` instead of `skills/<slug>/`. Already-approved skills keep updating in place — only first-time additions are gated. To review the queue:

```bash
watchmen review <project>      # walks _pending/ first (a)pprove / (d)rop / (s)kip / (v)iew / (q)uit,
                               # then the existing skills/ walk
```

Approving moves `_pending/<slug>/` → `skills/<slug>/`. If a previously-approved bundle exists at the destination, it's backed up to `<slug>.superseded/` for manual undo. Dropping removes the pending bundle and adds the slug to the blocklist so the finder doesn't re-propose it.

`watchmen show <project>` indicates pinned skills with 🔒 and lists blocked slugs in a separate section.

### Fast-cycle commands

- **`watchmen learn <project>`** closes the "did watchmen catch my latest session?" loop. Runs the analyst incrementally (only days since `last_analyst_day`) then a Stage-3-only curator pass to refresh CLAUDE.md. ~$0.50, 5-10 min. Add `--full` if you want the whole pipeline (Stage 1 finder + Stage 2 per-skill + Stage 3 CLAUDE.md, ~$3-8, 30-60 min) — useful when you expect new skill candidates to surface.
- **`watchmen review <project>`** walks every skill, prompts `(k)eep / (d)rop / (p)in / (s)kip / (v)iew / (q)uit`, and applies decisions through the same pin/drop helpers. Every walk appends to `bundles/<project>/review.md` so there's an audit trail of when each decision was made. Bails cleanly with a hint when stdin isn't a tty (e.g., piped).

## How it works

```
┌──────────────────────────────────────────────────────────────────┐
│  Hook layer (real-time capture, deterministic, no LLM)           │
│   ~/.claude/settings.json → hooks/watchmen_observe.sh            │
│   → POST to localhost:8765 → events.db + events.jsonl            │
└──────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│  Corpus layer (batch ingest)                                     │
│   corpus.py walks ~/.claude/projects/*.jsonl                     │
│   → corpus.db (sessions, prompts, tool_calls)                    │
└──────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│  Analyst (per-day LLM agent with carry-forward thesis)           │
│   analyze.py: for each day in order, agent reads prior thesis +  │
│   today's sessions → refined thesis. Tools: list_activity_on,    │
│   read_session_prompts, read_session_full, query_corpus.         │
│   Output: analyses/<project>/<date>.md, _running.md              │
└──────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│  Curator (4-stage, multi-turn agents with critic sub-agent)      │
│   1. candidate-finder reads thesis + scans repo                  │
│   2. per-skill curator (parallel) drafts SKILL.md + scripts,     │
│      spawns critic, refines                                      │
│   3. CLAUDE.md author reads thesis + skills + infra files        │
│   4. Index writer                                                │
│   Output: bundles/<project>/                                  │
└──────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│  Viewer + daemon                                                 │
│   FastAPI dashboard at 127.0.0.1:8979 renders all artifacts      │
│   Scheduler-managed daemon wakes every 30 min, runs pipeline     │
└──────────────────────────────────────────────────────────────────┘
```

Three lanes by latency budget:

| Lane | Latency | Triggers | What |
|---|---|---|---|
| Blocking real-time | <200ms | Hooks: SessionStart, UserPromptSubmit | Reserved for future context injection (no LLM allowed) |
| Async real-time | seconds | Hooks: PostToolUse, Stop | Logging only (today) |
| Batch | minutes-hours | Daemon schedule | Analyst + curator (LLM-heavy) |

## Cost

Per project, a full curator run (analyst + 6-8 skill bundles + CLAUDE.md) is typically `$3-8` in deepseek-v4-flash token costs. Incremental runs (daemon mode) are much cheaper — usually $0.10-0.50 per cycle since they only process new days. Set a daily cap if you want via the daemon's environment block in whichever scheduler unit it's installed under.

## Privacy

Everything lives locally. Your session transcripts live in `~/.claude/projects/` already (Anthropic puts them there). watchmen reads them, builds a SQLite corpus, and ships only the chunks needed for analysis to OpenRouter (your chosen LLM provider). The artifacts it generates (`bundles/`, `analyses/`) stay on your disk.

If you don't want certain repos analyzed, just don't track them — auto-detect only suggests, `watchmen track` is opt-in.

## Roadmap (coming soon)

| Source | Status | Notes |
|---|---|---|
| Claude Code **CLI** | ✅ shipped | Hooks + transcript ingest both work |
| Claude Code **desktop app** (Mac/Windows) | ✅ shipped | Same runtime as the CLI — no changes needed; same `~/.claude/projects/` + `~/.claude/settings.json` |
| **Codex** (OpenAI CLI / desktop) | ✅ shipped | Adapter reads `~/.codex/sessions/`. Sessions show up in `watchmen show`, `watchmen why` (with `cd` adapter tag), and the global `watchmen metrics` adapter breakdown. |
| **pi.dev** (CLI) | ✅ shipped | Adapter reads pi.dev's session export. Same ingest path as Codex; surfaces under the `pi` adapter tag. |
| **Cursor** | 🤔 considering | Stores sessions in SQLite (`state.vscdb`) with **no hook system** — only post-session polling is possible. Adapter is doable but realtime observation is impossible. |
| **OpenCode** | 🤔 considering | File-based sessions with a clean `opencode export` CLI. Straightforward adapter. |
| **Codex Cloud / Claude.ai web** | ❌ out of scope | No local files, no hooks. Would need an authenticated API that doesn't currently exist. |

Other roadmap items: diff view in the web UI (generated CLAUDE.md vs the repo's existing AGENTS.md), cross-project search, "promote artifact" button to copy a generated SKILL.md into the actual repo, live progress streaming during runs (SSE).

## Limitations + caveats

- **Hook server must run in a separate terminal.** It's a Python+FastAPI process. If you kill the terminal, hook capture stops. Use `tmux`/`screen`, a launchd job, a systemd --user unit, or a Task Scheduler task for it (we don't ship one for the hook server itself, only for the daemon + viewer).
- **Some skill curators occasionally run long (20+ min)** without calling the `finish_skill` terminal tool. The bundle still lands on disk; just no clean signal. ~15-20% of skills hit this. The artifacts are still usable.
- **Auto-detection of project_key from `~/.claude/projects/<encoded-dir>/` is heuristic.** Some path-encoded names (e.g., `my-business/marketing` vs `my-business-marketing`) can resolve ambiguously. Use `watchmen track <key> --repo <abs-path>` to be explicit.

## Layout

```
watchmen/
├── src/watchmen/             # Python package (src layout to avoid site-packages collisions)
│   ├── cli.py                # `watchmen` CLI entry
│   ├── agent.py              # shared OpenRouter tool-calling agent loop
│   ├── state.py              # state.db schema + helpers (run tracking, project state)
│   ├── tools_lib.py          # shared tool implementations for agents
│   ├── analyze.py            # longitudinal per-day analyst
│   ├── curate.py             # 4-stage skill + CLAUDE.md curator
│   ├── corpus.py             # ingest ~/.claude/projects/*.jsonl → corpus.db
│   ├── server.py             # hook capture server (`python -m watchmen.server`)
│   ├── daemon.py             # scheduling loop
│   ├── view.py               # CLI event browser (low-level)
│   ├── transcript.py         # CLI transcript renderer (low-level)
│   ├── viewer/               # FastAPI web dashboard + Jinja templates
│   ├── adapters/             # cc / cd / pi adapters for the corpus ingest path
│   ├── hooks/                # watchmen_observe.sh — POSTs hook stdin → localhost:8765
│   ├── hooks_setup.py        # install / uninstall the Claude Code hook entries
│   ├── service.py            # platform-dispatched install/uninstall (launchd ↔ systemd ↔ schtasks)
│   ├── launchd_setup.py      # macOS backend: ~/Library/LaunchAgents/*.plist
│   ├── systemd_setup.py      # Linux backend: ~/.config/systemd/user/*.service
│   └── schtasks_setup.py     # Windows backend: Task Scheduler XML via schtasks
├── plugin/                   # Claude Code plugin (separate distribution)
├── tests/                    # pytest-runnable smoke suite (Phase 5: full pytest port)
├── CHANGELOG.md
└── pyproject.toml
```

## Tests

Pytest-driven, no network. Smoke + regression coverage at `tests/test_smoke.py`, per-adapter tests at `tests/test_adapter_*.py`, OpenRouter agent loop with mocked httpx at `tests/test_agent.py`.

```bash
uv sync --extra dev          # install pytest + pytest-cov once
uv run pytest tests/         # full suite (~4s)
uv run pytest --cov=watchmen # with coverage
```

CI runs `pytest tests/` on every push to `main` and every PR (`.github/workflows/ci.yml`) across ubuntu × macos × py3.11/3.12. If you change anything in `state.py`, `metrics.py`, `corpus.py`, or `onboard.py`, expect the suite to gate the merge.

## License

MIT — see [LICENSE](LICENSE).
