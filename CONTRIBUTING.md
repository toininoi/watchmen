# Contributing to watchmen

watchmen is a small but ambitious project: a local-first session-intelligence
layer that turns your Claude Code (and Codex, and pi.dev) sessions into
durable skill bundles. Most contributors are users who hit something and
want to fix it — these notes are aimed at making that fix easy to ship.

## TL;DR

```bash
git clone https://github.com/firstbatchxyz/watchmen.git
cd watchmen
uv sync --extra dev           # editable watchmen + pytest + pytest-cov
uv run pytest tests/          # full test suite (~4s)
```

That's the whole loop. Edit code, rerun pytest. If it passes and your
manual `watchmen status` / `watchmen show` calls still work, open a PR.

Useful pytest invocations:

```bash
uv run pytest tests/ -k name           # subset by test name
uv run pytest tests/test_agent.py -v   # one file, verbose
uv run pytest --lf                     # rerun last failures only
uv run pytest --cov=watchmen tests/    # coverage report
```

## Project layout (recap from README)

```
src/watchmen/                 the package — every Python module lives here
src/watchmen/hooks/           shell hooks the package ships
plugin/                       separate Claude Code plugin (distributed via GitHub)
tests/test_smoke.py           cold-start + regression sweep (the original smoke suite)
tests/test_adapter_*.py       per-adapter parser tests (claude_code, codex, pi)
tests/test_agent.py           mocked OpenRouter retry/cost-ceiling tests
tests/conftest.py             shared sys.path setup + ROOT/SRC constants
tests/fixtures/               adapter fixtures (jsonl session traces)
```

If you're touching `state.py`, `metrics.py`, `corpus.py`, or `onboard.py`,
expect the test suite to gate your merge — those have the densest coverage.

## Local setup

We use [uv](https://github.com/astral-sh/uv) for Python tooling. It's a hard
requirement; there's no pip/poetry support today.

```bash
# Install uv (one-time)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Inside the repo
uv sync                                # creates .venv, installs deps + editable watchmen
uv tool install --editable .           # makes `watchmen` available globally (optional)

# Set your OpenRouter key if you'll run the agent loop
echo "OPENROUTER_API_KEY=sk-or-v1-..." > ~/.config/watchmen/.env
chmod 600 ~/.config/watchmen/.env
```

You can run the CLI two ways:

```bash
uv run watchmen <command>      # always works from anywhere in the repo
watchmen <command>             # works after `uv tool install --editable .`
```

## Running tests

```bash
uv run pytest tests/
```

That's it. The tests:

- Build cleanly without persisted state (they use temp dirs).
- Cover state.py, corpus.py, curate.py, metrics.py, the CLI dispatch, the
  viewer, all three adapters (cc / cd / pi), and the OpenRouter agent loop.
- Don't make network calls. `agent.call_openrouter` is exercised via
  `unittest.mock` stubs of `httpx.Client.post`.

Phase 5 of the handover will port these to pytest with proper fixtures. For
now: just one big script, one entry point.

## Branch + PR conventions

- Branch naming: `<area>/<short-slug>` is encouraged (`pricing/fix-per-token-unit`,
  `viewer/add-recordings-page`). Not enforced.
- PR title: short, imperative, no leading verb prefix needed. `Fix per-token
  pricing convention in model_prices` beats `feat: fix bug`.
- PR description: one sentence on *why*, one sentence on *what*, a checkbox
  list of testing notes if non-trivial. The template prompts for these.
- Co-author lines (`Co-Authored-By:`) are welcome but not required.

## Commit style

Follow what's already in `git log`:

```
<type>(<scope>): one-line summary in imperative mood

Body explaining *why*. Wrap at 72 columns. Reference issue numbers if relevant.
```

Common types: `feat`, `fix`, `refactor`, `chore`, `docs`, `test`. Common
scopes: `cli`, `curate`, `analyze`, `viewer`, `corpus`, `metrics`, `pricing`,
`adapters`, `daemon`, `tests`. Not strict — pick what reads clearest.

## What needs help

Look at:

- **Windows support / WSL polish** — daemon now supports macOS (launchd) and
  Linux (systemd --user). Windows hasn't been tested — likely needs a Task
  Scheduler backend or a "just run in foreground" path. WSL should already
  work since under WSL it's just Linux.
- **Cursor adapter** — Cursor stores sessions in a SQLite db (`state.vscdb`).
  An adapter at `src/watchmen/adapters/cursor.py` would round out the
  coverage. There are no hooks (post-session polling only).
- **OpenCode adapter** — File-based sessions with a clean `opencode export`
  CLI; should be a straightforward addition.
- **Diff view UX** — the viewer's per-run diff page works but is plain. A
  GitHub-style file tree, syntax highlighting, and "what changed in CLAUDE.md"
  callouts would be high-impact.
- **Tests for adapters** — codex.py and pi.py have one regression fixture
  each. claude_code.py has none. The fixtures in `tests/fixtures/` make it
  easy to add more.

## Reporting bugs

[Open an issue](https://github.com/firstbatchxyz/watchmen/issues/new/choose)
using the Bug Report template. The template prompts for:

- The command you ran (e.g. `watchmen curate kai-frontend`)
- The output (paste the failing lines)
- Your OS + Python version
- Whether you're on the latest `main`

The single most useful attachment is the relevant tail of
`~/Library/Logs/watchmen.log` (macOS) — `watchmen logs daemon` prints it.

## Reporting security issues

Don't open a public issue for security reports — use
[GitHub Security Advisories](https://github.com/firstbatchxyz/watchmen/security/advisories/new)
or email security@dria.co.

## Code of conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md) v2.1.
Be kind, be technical, be specific. Disagreement is fine; rudeness isn't.

## License

By contributing, you agree your changes ship under the [MIT License](LICENSE)
that already covers the rest of the project.
