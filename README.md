# watchmen

Local Claude Code session intelligence. Observes your sessions, analyzes how you use coding agents over time, and auto-generates skill bundles + `CLAUDE.md` per project — all on your Mac, no cloud.

The premise: every Claude Code session leaves a JSONL transcript in `~/.claude/projects/`. Across weeks of work this becomes a corpus of how you actually use the agent. watchmen mines that corpus, runs a longitudinal LLM analyst day by day with a carry-forward thesis, then curates the recurring procedural patterns into runnable skill bundles plus a workspace-level `CLAUDE.md` for each repo.

Runs continuously via a launchd daemon. Comes with a local web viewer at `http://127.0.0.1:8888`.

## What you get

For each tracked repo, watchmen produces under `kai_claude/<repo>/`:

```
CLAUDE.md                # 12-section workspace brief auto-generated from session evidence
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

## Requirements

- macOS
- [`uv`](https://github.com/astral-sh/uv) (Python toolchain)
- Python 3.11+
- An OpenRouter API key (`OPENROUTER_API_KEY`)
- Claude Code CLI in active use (the corpus is your `~/.claude/projects/` directory)

Models: defaults to `deepseek/deepseek-v4-flash` (fast, cheap, capable of multi-turn tool calling). Configurable per command.

## Install

```bash
git clone https://github.com/firstbatchxyz/watchmen.git
cd watchmen
uv sync                      # install Python deps
uv tool install --editable . # makes the `watchmen` CLI available system-wide
```

Set your API key (one of):

```bash
# Option A — env var (recommended for development)
export OPENROUTER_API_KEY=sk-or-v1-...

# Option B — .env in the project root
echo "OPENROUTER_API_KEY=sk-or-v1-..." > .env

# Option C — global config
mkdir -p ~/.config/watchmen
echo "OPENROUTER_API_KEY=sk-or-v1-..." > ~/.config/watchmen/.env
```

## Quickstart

```bash
# 1. Wire watchmen into your Claude Code hook config (live event capture)
watchmen install-hooks

# 2. Start the hook server in a long-lived terminal (captures every session event)
uv run python server.py
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
watchmen viewer                              # http://127.0.0.1:8888
```

Outputs land in `kai_claude/my-project/` and `analyses/my-project/`. Both are gitignored — they're your data, not the source.

## Run it continuously

To run watchmen autonomously (incremental analyzer + auto-regen of CLAUDE.md when new prompts come in):

```bash
watchmen install-daemon                      # launchd agent, autostart on login, keepalive
watchmen install-viewer                      # also autostart the viewer at :8888
watchmen launchd-status                      # verify
```

Default cadence:

| What | When |
|---|---|
| Re-ingest `~/.claude/projects/` + incremental analyst | Every **2 hours** |
| `CLAUDE.md` regen (stage 3 only, light) | After an analyst run if last regen >24h ago |
| **Full curator** (skill bundles + CLAUDE.md, expensive) | **02:00 and 14:00 local time**, min 8h between runs per project |

The analyst check is cheap — usually a no-op when there's nothing new. The full curator runs twice a day at 2am and 2pm to refresh skill bundles + the full CLAUDE.md. You can override these defaults via flags on `watchmen daemon` (see `--curator-hours`, `--interval`, `--full-curator-min-age`).

Logs:

```
~/Library/Logs/watchmen.log                  # primary daemon log
~/Library/Logs/watchmen.daemon.out.log       # launchd stdout
~/Library/Logs/watchmen.daemon.err.log       # launchd stderr
~/Library/Logs/watchmen.viewer.{out,err}.log # viewer logs
```

To stop:

```bash
watchmen uninstall-daemon
watchmen uninstall-viewer
watchmen uninstall-hooks
```

## Command reference

```
# Inspection
watchmen status                  Dashboard view of tracked projects
watchmen list                    Auto-detect projects from corpus
watchmen runs                    Recent run history
watchmen hooks-status            Show wired-up hook events
watchmen launchd-status          Show daemon/viewer agent state

# Project lifecycle
watchmen track <key> --repo <path>
watchmen sync                    Bootstrap state from on-disk artifacts (no LLM calls)

# Manual operations
watchmen ingest                  Re-scan ~/.claude/projects → corpus.db
watchmen analyze <key>           Run analyst (incremental, only new days)
watchmen analyze <key> --full    Full re-run (ignores prior thesis)
watchmen curate <key>            Full curator: candidates → skills → CLAUDE.md
watchmen curate <key> --regen-claude    Stage 3 only (regenerate CLAUDE.md)

# Continuous mode (foreground)
watchmen daemon                  Run scheduling loop in foreground
watchmen daemon --once           Single cycle, then exit (testing)
watchmen viewer                  FastAPI viewer in foreground

# Autostart (launchd)
watchmen install-{hooks,daemon,viewer}
watchmen uninstall-{hooks,daemon,viewer}
```

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
│   Output: kai_claude/<project>/                                  │
└──────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│  Viewer + daemon                                                 │
│   FastAPI dashboard at 127.0.0.1:8888 renders all artifacts      │
│   launchd daemon wakes every 30 min, runs incremental pipeline   │
└──────────────────────────────────────────────────────────────────┘
```

Three lanes by latency budget:

| Lane | Latency | Triggers | What |
|---|---|---|---|
| Blocking real-time | <200ms | Hooks: SessionStart, UserPromptSubmit | Reserved for future context injection (no LLM allowed) |
| Async real-time | seconds | Hooks: PostToolUse, Stop | Logging only (today) |
| Batch | minutes-hours | Daemon schedule | Analyst + curator (LLM-heavy) |

## Cost

Per project, a full curator run (analyst + 6-8 skill bundles + CLAUDE.md) is typically `$3-8` in deepseek-v4-flash token costs. Incremental runs (daemon mode) are much cheaper — usually $0.10-0.50 per cycle since they only process new days. Set a daily cap if you want via the launchd plist environment.

## Privacy

Everything lives locally. Your session transcripts live in `~/.claude/projects/` already (Anthropic puts them there). watchmen reads them, builds a SQLite corpus, and ships only the chunks needed for analysis to OpenRouter (your chosen LLM provider). The artifacts it generates (`kai_claude/`, `analyses/`) stay on your disk.

If you don't want certain repos analyzed, just don't track them — auto-detect only suggests, `watchmen track` is opt-in.

## Roadmap (coming soon)

| Source | Status | Notes |
|---|---|---|
| Claude Code **CLI** | ✅ shipped | Hooks + transcript ingest both work |
| Claude Code **desktop app** (Mac/Windows) | ✅ shipped | Same runtime as the CLI — no changes needed; same `~/.claude/projects/` + `~/.claude/settings.json` |
| **Codex** (OpenAI CLI / desktop) | 🔜 planned | Needs `corpus.py` adapter for `~/.codex/sessions/` + a `hooks/codex_observe.sh` + `install-hooks --codex` to patch `~/.codex/config.toml`. Schema is close enough that the analyst + curator stages will work unchanged. |
| **Cursor** | 🤔 considering | Stores sessions in SQLite (`state.vscdb`) with **no hook system** — only post-session polling is possible. Adapter is doable but realtime observation is impossible. |
| **OpenCode** | 🤔 considering | File-based sessions with a clean `opencode export` CLI. Straightforward adapter. |
| **Codex Cloud / Claude.ai web** | ❌ out of scope | No local files, no hooks. Would need an authenticated API that doesn't currently exist. |

Other roadmap items: diff view in the web UI (generated CLAUDE.md vs the repo's existing AGENTS.md), cross-project search, "promote artifact" button to copy a generated SKILL.md into the actual repo, live progress streaming during runs (SSE).

## Limitations + caveats

- **Hook server must run in a separate terminal.** It's a Python+FastAPI process. If you kill the terminal, hook capture stops. Use `tmux`, `screen`, or a launchd job for it (we don't ship one for the hook server itself, only for the daemon + viewer).
- **Some skill curators occasionally run long (20+ min)** without calling the `finish_skill` terminal tool. The bundle still lands on disk; just no clean signal. ~15-20% of skills hit this. The artifacts are still usable.
- **Auto-detection of project_key from `~/.claude/projects/<encoded-dir>/` is heuristic.** Some path-encoded names (e.g., `my-business/marketing` vs `my-business-marketing`) can resolve ambiguously. Use `watchmen track <key> --repo <abs-path>` to be explicit.
- **No tests yet.** Research-grade codebase. PRs welcome.

## Layout

```
watchmen/
├── cli.py                  # `watchmen` CLI entry
├── agent.py                # shared OpenRouter tool-calling agent loop
├── state.py                # state.db schema + helpers (run tracking, project state)
├── tools_lib.py            # shared tool implementations for agents
├── analyze.py              # longitudinal per-day analyst
├── curate.py               # 4-stage skill + CLAUDE.md curator
├── corpus.py               # ingest ~/.claude/projects/*.jsonl → corpus.db
├── server.py               # hook server (you run this in a terminal)
├── daemon.py               # scheduling loop
├── view.py                 # CLI event browser (low-level)
├── transcript.py           # CLI transcript renderer (low-level)
├── viewer/                 # FastAPI web dashboard
│   ├── server.py
│   └── templates/
├── hooks/
│   └── watchmen_observe.sh # POSTs hook stdin → localhost:8765
├── hooks_setup.py          # install-hooks / uninstall-hooks
├── launchd_setup.py        # install-daemon / install-viewer launchd integration
└── pyproject.toml
```

## License

MIT — see [LICENSE](LICENSE).
