"""Tests for the deterministic (non-LLM) scaffolding in watchmen.curate.

These cover the parts of the curator pipeline that have nothing to do with the
model: the skill-curator write-scope guard, the changelog labelling, SKILL.md
frontmatter extraction, and the per-run git commit. The LLM agents themselves
are out of scope here — this is the plumbing the agents run on top of.
"""

from __future__ import annotations

import subprocess

import pytest

from watchmen import curate


# ─── _scope_skill_path — the curator's write-scope security boundary ────────
#
# A skill curator agent may only write under its own bundle dir. This guard is
# what enforces that. It's pure (no FS), so we can hammer the policy directly.

SLUG = "deploy"
PREFIX = "skills/deploy/"


def test_scope_path_in_scope_passes_through_verbatim():
    scoped, note, error = curate._scope_skill_path("skills/deploy/SKILL.md", slug=SLUG, expected_prefix=PREFIX)
    assert scoped == "skills/deploy/SKILL.md"
    assert note is None and error is None


def test_scope_path_plain_relative_is_auto_scoped():
    scoped, note, error = curate._scope_skill_path("SKILL.md", slug=SLUG, expected_prefix=PREFIX)
    assert scoped == "skills/deploy/SKILL.md"
    assert error is None
    assert note is not None and "auto-scoped" in note


def test_scope_path_nested_relative_is_auto_scoped():
    scoped, note, _ = curate._scope_skill_path("scripts/run.py", slug=SLUG, expected_prefix=PREFIX)
    assert scoped == "skills/deploy/scripts/run.py"
    assert note is not None


def test_scope_path_redundant_slug_prefix_is_stripped():
    # The agent sometimes types `<slug>/SKILL.md`; that must resolve the same
    # as a bare `SKILL.md`, not nest into skills/deploy/deploy/.
    scoped, _, error = curate._scope_skill_path("deploy/SKILL.md", slug=SLUG, expected_prefix=PREFIX)
    assert scoped == "skills/deploy/SKILL.md"
    assert error is None


def test_scope_path_dot_slash_prefix_is_cleaned():
    scoped, _, error = curate._scope_skill_path("./SKILL.md", slug=SLUG, expected_prefix=PREFIX)
    assert scoped == "skills/deploy/SKILL.md"
    assert error is None


@pytest.mark.parametrize(
    "bad",
    [
        "skills/other/SKILL.md",   # a sibling skill
        "_pending/deploy/SKILL.md",  # the approval queue
        "/etc/passwd",             # absolute
        "../../etc/passwd",        # traversal
        "scripts/../../escape",    # traversal mid-path
    ],
)
def test_scope_path_rejects_out_of_scope(bad):
    scoped, note, error = curate._scope_skill_path(bad, slug=SLUG, expected_prefix=PREFIX)
    assert scoped is None and note is None
    assert error is not None and PREFIX in error


def test_scope_path_pending_mode_allows_its_own_prefix():
    # In approval mode the bundle lands under _pending/<slug>/, so a path
    # already in that scope must pass even though `_pending/` is otherwise
    # a forbidden prefix.
    scoped, _, error = curate._scope_skill_path(
        "_pending/deploy/SKILL.md", slug=SLUG, expected_prefix="_pending/deploy/"
    )
    assert scoped == "_pending/deploy/SKILL.md"
    assert error is None


# ─── _changelog_label ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "rel,label",
    [
        ("CLAUDE.md", "CLAUDE.md"),
        ("skills/foo/SKILL.md", "skills/foo"),
        ("skills/foo/scripts/run.py", "skills/foo"),
        ("AGENTS.md", "AGENTS.md"),   # passthrough for non-skill paths
        ("skills", "skills"),         # too few segments → passthrough
    ],
)
def test_changelog_label(rel, label):
    assert curate._changelog_label(rel) == label


# ─── _extract_frontmatter_field ─────────────────────────────────────────────

_FM = (
    "name: deploy\n"
    "description: Ship the app safely\n"
    "when_to_use:\n"
    "  - when deploying to prod\n"
    "  - before a release cut\n"
)


def test_frontmatter_single_line_field():
    assert curate._extract_frontmatter_field(_FM, "description") == "Ship the app safely"


def test_frontmatter_bullet_list_flattened_to_text():
    assert curate._extract_frontmatter_field(_FM, "when_to_use") == "when deploying to prod before a release cut"


def test_frontmatter_missing_field_returns_empty():
    assert curate._extract_frontmatter_field(_FM, "nonexistent") == ""


# ─── _git_commit_artifacts ──────────────────────────────────────────────────


def _commit_count(d) -> int:
    r = subprocess.run(["git", "-C", str(d), "rev-list", "--count", "HEAD"], capture_output=True, text=True)
    return int(r.stdout.strip()) if r.returncode == 0 else 0


def test_git_commit_initializes_and_commits(tmp_path):
    proj = tmp_path / "bundle"
    (proj / "skills" / "foo").mkdir(parents=True)
    (proj / "skills" / "foo" / "SKILL.md").write_text("# foo\n")

    sha = curate._git_commit_artifacts(proj, "full curator", "2026-01-01 10:00", ["skills/foo"], [], [])

    assert sha and len(sha) == 40
    assert (proj / ".git").exists()
    msg = subprocess.run(
        ["git", "-C", str(proj), "log", "-1", "--format=%B"], capture_output=True, text=True
    ).stdout
    assert "full curator @ 2026-01-01 10:00" in msg
    assert "Added:" in msg and "skills/foo" in msg
    # .gitignore must keep mtime bookkeeping out of the diff.
    assert "_manifest.json" in (proj / ".gitignore").read_text()


def test_git_commit_noop_when_nothing_changed_returns_same_head(tmp_path):
    proj = tmp_path / "bundle"
    proj.mkdir()
    (proj / "SKILL.md").write_text("x")
    first = curate._git_commit_artifacts(proj, "full curator", "t1", [], [], [])
    assert _commit_count(proj) == 1
    # No new files → no new commit, HEAD unchanged.
    second = curate._git_commit_artifacts(proj, "full curator", "t2", [], [], [])
    assert second == first
    assert _commit_count(proj) == 1


def test_git_commit_returns_none_when_git_missing(tmp_path, monkeypatch):
    proj = tmp_path / "bundle"
    proj.mkdir()
    monkeypatch.setattr(curate.shutil, "which", lambda _name: None)
    assert curate._git_commit_artifacts(proj, "full curator", "t", [], [], []) is None


def test_git_commit_returns_none_when_dir_absent(tmp_path):
    assert curate._git_commit_artifacts(tmp_path / "nope", "full curator", "t", [], [], []) is None
