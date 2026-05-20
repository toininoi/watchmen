"""Subagent usage aggregations.

Watchmen has tracked `sessions.is_subagent` + `parent_session_id` across all
four adapters since the early adapter rewrite, but nothing surfaced that
signal until now. This module is the read side: aggregations that drive the
`watchmen subagents` CLI and the per-project section in the web viewer.

The shape of the question we're answering is: **how much of your coding-
agent spend goes through subagents vs. through monolithic main-thread
sessions, broken down per project, and which main sessions are the
biggest "missed delegation" candidates?**

The first metric is honest measurement; the second is the seed for the
intervention layer that will follow (statusline nudges, generated subagent
specs as skills, cross-agent subagent translation). Get the numbers right
before designing the intervention.

Notes on what's countable:

- Claude Code is the only agent in our supported set that publishes a
  first-class subagent primitive in its transcript layout (the
  `<encoded-cwd>/<session>/subagents/*.jsonl` directory). For Codex,
  pi.dev, and OpenCode the on-disk format doesn't expose nested agent
  invocations, so `is_subagent=0` is the truthful reading there until
  upstream changes. Don't paper over the gap by inventing heuristics —
  surface it so the user knows the score.
- Token totals are summed across all four token columns
  (input + cache_creation + cache_read + output) for a single
  "context volume" number. Cost is just `cost_usd` (the per-row total
  already accounting for cache discounts and reasoning tokens).
- Session count is wildly misleading on its own — for a heavy user,
  subagents can be 97% of session *count* but only 17% of *cost*, since
  each subagent is small and short-lived while a main session keeps
  accumulating tokens turn after turn. The CLI and viewer both lead
  with cost share, not count share.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from watchmen.paths import CORPUS_DB
from watchmen.state import _project_dir_predicate, list_projects


# ─── Connection helper ─────────────────────────────────────────────────────


def _conn_ro() -> sqlite3.Connection | None:
    """Open corpus.db read-only. Returns None when the file is missing.

    Mirrors `watchmen.viewer.homepage._conn_ro` rather than importing it —
    keeps this module independent of the viewer subtree so CLI users without
    fastapi installed can still get subagent metrics.
    """
    if not CORPUS_DB.exists():
        return None
    c = sqlite3.connect(f"file:{CORPUS_DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def _total_tokens_sql(prefix: str = "") -> str:
    """SQL expression summing the four token columns into one volume number.

    `prefix` is the table alias plus dot if joining (`s.` or `''`).
    """
    return (
        f"({prefix}input_tokens + {prefix}cache_creation_tokens + "
        f"{prefix}cache_read_tokens + {prefix}output_tokens)"
    )


# ─── Result dataclasses ────────────────────────────────────────────────────


@dataclass
class AgentMetrics:
    """Subagent share for a single agent across all tracked projects."""
    agent: str
    sessions: int
    subagent_sessions: int
    main_cost: float
    sub_cost: float
    main_tokens: int
    sub_tokens: int

    @property
    def total_cost(self) -> float:
        return self.main_cost + self.sub_cost

    @property
    def total_tokens(self) -> int:
        return self.main_tokens + self.sub_tokens

    @property
    def cost_share_pct(self) -> float | None:
        if self.total_cost <= 0:
            return None
        return 100.0 * self.sub_cost / self.total_cost

    @property
    def count_share_pct(self) -> float | None:
        if self.sessions <= 0:
            return None
        return 100.0 * self.subagent_sessions / self.sessions


@dataclass
class ProjectMetrics:
    """Subagent share for a tracked project, summed across all agents."""
    project_key: str
    source_repo: str
    sessions: int
    subagent_sessions: int
    main_cost: float
    sub_cost: float
    main_tokens: int
    sub_tokens: int
    # Top main sessions by cost — the "delegation candidates" surface.
    candidates: list["DelegationCandidate"] = field(default_factory=list)

    @property
    def total_cost(self) -> float:
        return self.main_cost + self.sub_cost

    @property
    def total_tokens(self) -> int:
        return self.main_tokens + self.sub_tokens

    @property
    def cost_share_pct(self) -> float | None:
        if self.total_cost <= 0:
            return None
        return 100.0 * self.sub_cost / self.total_cost

    @property
    def has_data(self) -> bool:
        return self.sessions > 0


@dataclass
class DelegationCandidate:
    """A main session that probably should have delegated.

    No magic scoring yet — we just rank by cost descending. The intuition:
    a single main session that cost $X with hundreds of tool calls and no
    subagents is exactly the shape of session that would have benefitted
    from delegation, and the user knows what they were working on.
    """
    session_id: str
    agent: str
    started_at: str | None
    cost_usd: float
    total_tokens: int
    tool_use_count: int


# ─── Aggregation queries ───────────────────────────────────────────────────


def aggregate_by_agent() -> list[AgentMetrics]:
    """Global per-agent breakdown.

    Doesn't filter by tracked project; gives the truthful "across your
    entire corpus" view. The CLI's headline number.
    """
    c = _conn_ro()
    if c is None:
        return []
    tok_expr = _total_tokens_sql()
    rows = c.execute(
        f"""
        SELECT
          agent,
          COUNT(*)                                                                AS sessions,
          SUM(is_subagent)                                                        AS sub_n,
          COALESCE(SUM(CASE WHEN is_subagent=0 THEN cost_usd ELSE 0 END), 0)      AS main_cost,
          COALESCE(SUM(CASE WHEN is_subagent=1 THEN cost_usd ELSE 0 END), 0)      AS sub_cost,
          COALESCE(SUM(CASE WHEN is_subagent=0 THEN {tok_expr} ELSE 0 END), 0)    AS main_tokens,
          COALESCE(SUM(CASE WHEN is_subagent=1 THEN {tok_expr} ELSE 0 END), 0)    AS sub_tokens
        FROM sessions
        GROUP BY agent
        ORDER BY (main_cost + sub_cost) DESC
        """
    ).fetchall()
    return [
        AgentMetrics(
            agent=r["agent"],
            sessions=r["sessions"],
            subagent_sessions=r["sub_n"] or 0,
            main_cost=float(r["main_cost"] or 0.0),
            sub_cost=float(r["sub_cost"] or 0.0),
            main_tokens=int(r["main_tokens"] or 0),
            sub_tokens=int(r["sub_tokens"] or 0),
        )
        for r in rows
    ]


def aggregate_for_project(project_key: str, source_repo: str, *, candidates_limit: int = 5) -> ProjectMetrics:
    """Per-project breakdown for a single tracked project.

    `source_repo` is the absolute path watchmen has on file for the
    project (from `state.list_projects`). We use the same predicate the
    rest of the app uses (`project_dir = ?` OR `project_dir LIKE root/%`)
    so sub-directory sessions are counted with their root.
    """
    c = _conn_ro()
    empty = ProjectMetrics(
        project_key=project_key, source_repo=source_repo,
        sessions=0, subagent_sessions=0,
        main_cost=0.0, sub_cost=0.0,
        main_tokens=0, sub_tokens=0,
    )
    if c is None:
        return empty
    predicate, params = _project_dir_predicate(source_repo)
    tok = _total_tokens_sql("s.")
    row = c.execute(
        f"""
        SELECT
          COUNT(*)                                                                  AS sessions,
          SUM(s.is_subagent)                                                        AS sub_n,
          COALESCE(SUM(CASE WHEN s.is_subagent=0 THEN s.cost_usd ELSE 0 END), 0)    AS main_cost,
          COALESCE(SUM(CASE WHEN s.is_subagent=1 THEN s.cost_usd ELSE 0 END), 0)    AS sub_cost,
          COALESCE(SUM(CASE WHEN s.is_subagent=0 THEN {tok} ELSE 0 END), 0)         AS main_tokens,
          COALESCE(SUM(CASE WHEN s.is_subagent=1 THEN {tok} ELSE 0 END), 0)         AS sub_tokens
        FROM sessions s
        WHERE {predicate}
        """,
        params,
    ).fetchone()

    metrics = ProjectMetrics(
        project_key=project_key,
        source_repo=source_repo,
        sessions=row["sessions"] or 0,
        subagent_sessions=row["sub_n"] or 0,
        main_cost=float(row["main_cost"] or 0.0),
        sub_cost=float(row["sub_cost"] or 0.0),
        main_tokens=int(row["main_tokens"] or 0),
        sub_tokens=int(row["sub_tokens"] or 0),
    )
    if candidates_limit > 0 and metrics.main_cost > 0:
        metrics.candidates = _top_main_sessions(
            c, predicate, params, limit=candidates_limit
        )
    return metrics


def aggregate_per_project() -> list[ProjectMetrics]:
    """Every tracked project, sorted by total cost descending.

    Projects with zero sessions are still returned (with all-zero rows) so
    the CLI/viewer can list everything tracked, not just the ones with
    data. The display side filters as it sees fit.
    """
    out: list[ProjectMetrics] = []
    for p in list_projects():
        m = aggregate_for_project(
            project_key=p["project_key"],
            source_repo=p["source_repo"],
            candidates_limit=0,  # don't load candidates for the bulk list
        )
        out.append(m)
    out.sort(key=lambda m: m.total_cost, reverse=True)
    return out


def _top_main_sessions(
    c: sqlite3.Connection,
    predicate: str,
    params: tuple,
    *,
    limit: int,
) -> list[DelegationCandidate]:
    tok = _total_tokens_sql("s.")
    rows = c.execute(
        f"""
        SELECT s.session_id, s.agent, s.started_at, s.cost_usd,
               {tok} AS total_tokens,
               s.tool_use_count
        FROM sessions s
        WHERE {predicate} AND s.is_subagent = 0 AND s.cost_usd > 0
        ORDER BY s.cost_usd DESC
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()
    return [
        DelegationCandidate(
            session_id=r["session_id"],
            agent=r["agent"],
            started_at=r["started_at"],
            cost_usd=float(r["cost_usd"] or 0.0),
            total_tokens=int(r["total_tokens"] or 0),
            tool_use_count=int(r["tool_use_count"] or 0),
        )
        for r in rows
    ]


# ─── Convenience aggregate ────────────────────────────────────────────────


def aggregate_totals() -> dict:
    """Single dict summing the entire corpus.

    Used by the homepage and as the headline row in `watchmen subagents`.
    Safe with an empty / missing corpus.db (returns all-zero values).
    """
    agents = aggregate_by_agent()
    total_sessions    = sum(a.sessions for a in agents)
    total_subagents   = sum(a.subagent_sessions for a in agents)
    total_main_cost   = sum(a.main_cost for a in agents)
    total_sub_cost    = sum(a.sub_cost for a in agents)
    total_main_tokens = sum(a.main_tokens for a in agents)
    total_sub_tokens  = sum(a.sub_tokens for a in agents)
    total_cost   = total_main_cost + total_sub_cost
    total_tokens = total_main_tokens + total_sub_tokens
    return {
        "sessions": total_sessions,
        "subagent_sessions": total_subagents,
        "main_cost": total_main_cost,
        "sub_cost": total_sub_cost,
        "main_tokens": total_main_tokens,
        "sub_tokens": total_sub_tokens,
        "total_cost": total_cost,
        "total_tokens": total_tokens,
        "cost_share_pct": (100.0 * total_sub_cost / total_cost) if total_cost > 0 else None,
        "count_share_pct": (100.0 * total_subagents / total_sessions) if total_sessions > 0 else None,
    }
