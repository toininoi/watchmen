"""`watchmen skills` — per-skill usage + outcome association from the corpus.

The CLI side of `watchmen.metrics.skill_effectiveness`. Two questions, one
table:

  watchmen skills                  # global: every curated skill that fired
  watchmen skills --project <key>  # scope to one project
  watchmen skills --json           # raw rows for scripting

It leads with **usage** (how much is the skill actually used, is it cooling
off, what has it cost) because a skill that never fires is dead weight no
matter how good its outcomes look. The right-hand columns add the **outcome
association** — fired vs comparable non-fired sessions in the same projects.

That comparison is ASSOCIATION, NOT CAUSATION: a session invokes a skill
because the task suited it, so fired sessions self-select for harder or more
procedural work. The header says so and the per-skill verdict never claims
the skill *caused* the delta. Same discipline as the project impact card.
"""

from __future__ import annotations

import json as _json

from watchmen import metrics as wm_metrics
from watchmen.ui import bold, dim, yellow


def _money(x: float) -> str:
    return f"${x:,.2f}"


def _trend(recent: int, prior: int) -> str:
    """Cheap decay glyph from the recent-half vs leading-half fire split."""
    if prior == 0 and recent == 0:
        return dim("—")
    if recent == 0 and prior > 0:
        return "[red]cold[/red]"
    if recent > prior:
        return "[green]↑[/green]"
    if recent < prior:
        return "[yellow]↓[/yellow]"
    return dim("→")


def _delta(val: float | None, *, money: bool = False, good_when_negative: bool = True) -> str:
    """Render a signed delta, green when it's the direction we want."""
    if val is None:
        return dim("n/a")
    txt = f"{'-' if val < 0 else '+'}{_money(abs(val)) if money else f'{abs(val) * 100:.1f}pp'}"
    if val == 0:
        return dim(txt)
    helps = (val < 0) == good_when_negative
    return f"[green]{txt}[/green]" if helps else f"[red]{txt}[/red]"


def cmd_skills(args) -> int:
    """Entry point for `watchmen skills [--project <key>] [--days N] [--json]`."""
    project = getattr(args, "project", None)
    days = getattr(args, "days", 90)
    rows = wm_metrics.skill_effectiveness(days=days, project_key=project)

    if getattr(args, "json", False):
        print(_json.dumps(rows, indent=2))
        return 0

    from rich.console import Console
    from rich.table import Table

    console = Console()
    if not rows:
        console.print(
            yellow(
                "No skill invocations captured"
                + (f" for project '{project}'" if project else "")
                + " in the window."
            )
        )
        console.print(
            dim("  Skills are logged once they fire in a real session. Run `watchmen ingest` to refresh.")
        )
        return 0

    scope = f" — {project}" if project else ""
    console.print()
    console.print(bold(f"Skill usage & outcomes{scope}"))
    console.print(dim(f"  last {days} days  ·  {len(rows)} skill(s) fired"))
    console.print()

    t = Table(header_style="cyan", show_lines=False, expand=False)
    t.add_column("skill")
    t.add_column("fires", justify="right")
    t.add_column("sess", justify="right")
    t.add_column("proj", justify="right")
    t.add_column("last", justify="right")
    t.add_column("trend", justify="center")
    t.add_column("cost", justify="right")
    t.add_column("err Δ", justify="right")
    t.add_column("cost Δ", justify="right")
    for r in rows:
        t.add_row(
            r["skill_name"],
            f"{r['fires']:,}",
            f"{r['sessions_fired']:,}",
            f"{r['projects']:,}",
            r["last_fired"] or dim("never"),
            _trend(r["fires_recent"], r["fires_prior"]),
            _money(r["cost_usd"]),
            _delta(r["delta_error_rate"]),
            _delta(r["delta_cost"], money=True),
        )
    console.print(t)
    console.print()
    console.print(
        dim(
            "  err Δ / cost Δ = fired sessions minus comparable non-fired sessions in the same "
            "projects (median). Green = the direction you'd want. ASSOCIATION ONLY — skills fire "
            "on harder tasks, so this is a triage signal, not proof the skill caused the change."
        )
    )

    # Surface the strongest verdicts (the ones with enough samples to compare).
    judged = [r for r in rows if r["delta_error_rate"] is not None]
    if judged:
        console.print()
        for r in judged[:5]:
            console.print(dim(f"  {bold(r['skill_name'])}: {r['verdict']}"))
    return 0
