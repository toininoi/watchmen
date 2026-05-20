"""`watchmen subagents` — surface subagent usage from the corpus.

The CLI side of `watchmen.subagents`. Two display modes:

  watchmen subagents                  # global table: per-project + per-agent breakdown
  watchmen subagents --project <key>  # detail: cost split + top delegation candidates

The output is intentionally compact and leads with **cost share** (not
session count), because subagents tend to dominate session count by a
huge margin while still being a minority of actual spend. We don't want
"97% of your sessions are subagents" to read as "you delegate plenty"
when the underlying truth is that 83% of your money is still flowing
through monolithic main-thread sessions.
"""

from __future__ import annotations

from watchmen import subagents as wm_subagents
from watchmen.ui import bold, dim, yellow


def _pct(val: float | None) -> str:
    return "—" if val is None else f"{val:.0f}%"


def _money(x: float) -> str:
    return f"${x:,.2f}"


def _short_path(p: str) -> str:
    """Shorten ~/Users/<me>/... to ~/... so the project table fits 100 cols."""
    import os
    home = os.path.expanduser("~")
    if p and p.startswith(home):
        return "~" + p[len(home):]
    return p or ""


def _short_sid(session_id: str) -> str:
    # Adapters use UUID-ish or rollout-stem ids; first 12 chars is enough to
    # eyeball and grep, full id is always one query away.
    return session_id[:12]


def cmd_subagents(args) -> int:
    """Entry point for `watchmen subagents [--project <key>]`."""
    from rich.console import Console
    from rich.table import Table

    console = Console()
    project = getattr(args, "project", None)

    if project:
        return _render_project_detail(console, project)
    return _render_overview(console, Table)


# ─── Overview (no --project): per-agent + per-project tables ──────────────


def _render_overview(console, Table) -> int:
    totals = wm_subagents.aggregate_totals()
    if totals["sessions"] == 0:
        console.print(yellow("No sessions in corpus.db yet — run `watchmen ingest` first."))
        return 0

    # Headline
    console.print()
    console.print(bold("Subagent usage"))
    share = totals["cost_share_pct"]
    console.print(dim(
        f"  across {totals['sessions']:,} sessions  "
        f"·  total {_money(totals['total_cost'])}  "
        f"·  subagent share {_pct(share)}"
    ))
    console.print()

    # Per-agent
    t = Table(title="By agent", title_style="bold", header_style="cyan", show_lines=False, expand=False)
    t.add_column("agent"); t.add_column("sessions", justify="right"); t.add_column("sub", justify="right")
    t.add_column("main $", justify="right"); t.add_column("sub $", justify="right"); t.add_column("share", justify="right")
    for a in wm_subagents.aggregate_by_agent():
        if a.sessions == 0:
            continue
        t.add_row(
            a.agent,
            f"{a.sessions:,}", f"{a.subagent_sessions:,}",
            _money(a.main_cost), _money(a.sub_cost),
            _color_share(a.cost_share_pct),
        )
    console.print(t)

    # Per-project (sorted by total cost, hide projects with no data)
    rows = [m for m in wm_subagents.aggregate_per_project() if m.has_data]
    if not rows:
        console.print()
        console.print(dim("No tracked projects have sessions yet."))
        return 0

    t = Table(title="By project (top 15 by cost)", title_style="bold",
              header_style="cyan", show_lines=False, expand=False)
    t.add_column("project"); t.add_column("sessions", justify="right"); t.add_column("sub", justify="right")
    t.add_column("main $", justify="right"); t.add_column("sub $", justify="right"); t.add_column("share", justify="right")
    for m in rows[:15]:
        t.add_row(
            m.project_key,
            f"{m.sessions:,}", f"{m.subagent_sessions:,}",
            _money(m.main_cost), _money(m.sub_cost),
            _color_share(m.cost_share_pct),
        )
    console.print()
    console.print(t)

    # Helpful next-step hint pointed at the worst-case project.
    worst = next((m for m in rows if m.total_cost >= 10.0 and (m.cost_share_pct or 0) < 10), None)
    if worst:
        console.print()
        console.print(dim(
            f"  Tip: {bold(worst.project_key)} has {_pct(worst.cost_share_pct)} subagent share over "
            f"{_money(worst.total_cost)} total — likely a delegation gap.  "
            f"`watchmen subagents --project {worst.project_key}` for top main sessions."
        ))
    return 0


def _color_share(pct: float | None) -> str:
    """Color the share % so the eye lands on the gaps.

    Red below 10%, yellow 10-30%, green above 30%. These thresholds are
    intuition not science — we'll calibrate once we have ROI numbers on
    how much a delegated session actually saves.
    """
    if pct is None:
        return dim("—")
    s = f"{pct:.0f}%"
    if pct < 10:
        return f"[red]{s}[/red]"
    if pct < 30:
        return f"[yellow]{s}[/yellow]"
    return f"[green]{s}[/green]"


# ─── Detail (--project <key>): per-project breakdown + candidates ─────────


def _render_project_detail(console, project_key: str) -> int:
    from watchmen import state
    p = state.get_project(project_key)
    if p is None:
        console.print(yellow(f"project '{project_key}' is not tracked."))
        console.print(dim("  Run `watchmen list` to see tracked projects."))
        return 1
    m = wm_subagents.aggregate_for_project(project_key, p["source_repo"], candidates_limit=10)

    console.print()
    console.print(bold(f"Subagent usage — {project_key}"))
    console.print(dim(f"  repo: {_short_path(p['source_repo'])}"))
    console.print()

    if m.sessions == 0:
        console.print(dim("No sessions captured for this project."))
        return 0

    # Two-column compact summary.
    console.print(f"  Total cost     {_money(m.total_cost):>14}  ({m.sessions:,} sessions)")
    console.print(f"  Main thread    {_money(m.main_cost):>14}  ({m.sessions - m.subagent_sessions:,} sessions)")
    console.print(f"  Subagents      {_money(m.sub_cost):>14}  ({m.subagent_sessions:,} sessions)")
    console.print(f"  Cost share     {_color_share(m.cost_share_pct):>14}")
    console.print()

    if not m.candidates:
        console.print(dim("No main-thread sessions with cost > $0 to rank."))
        return 0

    from rich.table import Table
    t = Table(title="Top main sessions (delegation candidates)",
              title_style="bold", header_style="cyan", show_lines=False, expand=False)
    t.add_column("session"); t.add_column("agent"); t.add_column("started")
    t.add_column("cost", justify="right"); t.add_column("tokens", justify="right")
    t.add_column("tools", justify="right")
    for c in m.candidates:
        t.add_row(
            _short_sid(c.session_id),
            c.agent,
            (c.started_at or "")[:10],
            _money(c.cost_usd),
            f"{c.total_tokens/1e6:.1f}M" if c.total_tokens >= 1_000_000 else f"{c.total_tokens/1000:.0f}k",
            f"{c.tool_use_count:,}",
        )
    console.print(t)
    console.print()
    console.print(dim(
        "  These are main-thread sessions ordered by cost. Sessions with many tool calls "
        "and large token counts are the strongest candidates for delegating work to subagents."
    ))
    return 0
