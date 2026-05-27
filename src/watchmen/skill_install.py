"""Install curated skill bundles into coding-agent discovery paths.

The curator writes skills to ``BUNDLES_DIR/<project_key>/skills/<slug>/``, but
coding agents only discover skills from their own directories
(``~/.claude/skills/``, ``~/.codex/skills/``). Until a curated skill is
installed there, the agent never sees it and it can never fire. This module
bridges that gap.

Install is **symlink-based**: ``~/.claude/skills/<slug>`` points back at the
bundle skill directory, so the bundle stays the single source of truth and
curator edits propagate without re-copying. A small manifest under
``WATCHMEN_HOME`` records every link watchmen creates, so uninstall is precise
and a skill directory the user made by hand is never touched.

Conflict policy: a target slug that watchmen created (recorded in the manifest,
or a symlink already pointing into ``BUNDLES_DIR``) is replaced on reinstall; a
target the user created themselves is skipped unless ``force=True``.

Surface:
    bundle_skills(project_key)                  -> list[BundleSkill]
    harness_skill_dir(harness)                  -> Path | None
    install_skill(skill, harness, force=...)    -> InstallResult
    install_project(project_key, ...)           -> list[InstallResult]
    uninstall_skill(slug, harness)              -> InstallResult
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from watchmen.paths import BUNDLES_DIR, WATCHMEN_HOME

# Discovery directories per harness. Only harnesses that read a flat
# `<dir>/<slug>/SKILL.md` layout belong here; multi-provider harnesses without
# a local skill dir are intentionally absent (harness_skill_dir returns None).
HARNESS_SKILL_DIRS: dict[str, Path] = {
    "claude_code": Path.home() / ".claude" / "skills",
    "codex": Path.home() / ".codex" / "skills",
}

MANIFEST_PATH = WATCHMEN_HOME / "install_manifest.json"

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
_KV_RE = re.compile(r"^([a-zA-Z_][a-zA-Z0-9_-]*)\s*:\s*(.*)$")


@dataclass
class BundleSkill:
    slug: str
    name: str
    description: str
    skill_dir: Path
    source_md: Path


@dataclass
class InstallResult:
    slug: str
    harness: str
    target: Path | None
    action: str  # installed | replaced | skipped_conflict | skipped_no_dir | uninstalled | not_installed | missing
    reason: str = ""


def _parse_description(text: str) -> str:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return ""
    for line in m.group(1).splitlines():
        m2 = _KV_RE.match(line.strip())
        if m2 and m2.group(1).strip().lower() == "description":
            return m2.group(2).strip().strip('"').strip("'")
    return ""


def bundle_skills(project_key: str) -> list[BundleSkill]:
    """Enumerate curated skills in a project's bundle. Empty list if the
    project has no bundle or no skills dir yet."""
    skills_root = BUNDLES_DIR / project_key / "skills"
    if not skills_root.exists() or not skills_root.is_dir():
        return []
    out: list[BundleSkill] = []
    for skill_dir in sorted(skills_root.iterdir()):
        if not skill_dir.is_dir():
            continue
        md = skill_dir / "SKILL.md"
        if not md.exists():
            continue
        try:
            text = md.read_text(encoding="utf-8")
        except OSError:
            text = ""
        out.append(
            BundleSkill(
                slug=skill_dir.name,
                name=skill_dir.name,
                description=_parse_description(text),
                skill_dir=skill_dir,
                source_md=md,
            )
        )
    return out


def harness_skill_dir(harness: str) -> Path | None:
    return HARNESS_SKILL_DIRS.get(harness)


# ── Manifest: the record of every link watchmen created ────────────────────

def _load_manifest() -> list[dict]:
    if not MANIFEST_PATH.exists():
        return []
    try:
        data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    links = data.get("links") if isinstance(data, dict) else None
    return links if isinstance(links, list) else []


def _save_manifest(links: list[dict]) -> None:
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = MANIFEST_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"links": links}, indent=2), encoding="utf-8")
    tmp.replace(MANIFEST_PATH)


def _manifest_entry(links: list[dict], target: Path) -> dict | None:
    target_s = str(target)
    for link in links:
        if link.get("target") == target_s:
            return link
    return None


def _is_managed(target: Path, links: list[dict]) -> bool:
    """True if watchmen owns this target — either it's in the manifest, or it's
    a symlink already pointing inside BUNDLES_DIR (covers manifest loss)."""
    if _manifest_entry(links, target) is not None:
        return True
    if target.is_symlink():
        try:
            dest = target.readlink()
        except OSError:
            return False
        dest_abs = dest if dest.is_absolute() else (target.parent / dest)
        try:
            dest_abs.resolve().relative_to(BUNDLES_DIR.resolve())
            return True
        except (ValueError, OSError):
            return False
    return False


def _record(links: list[dict], *, slug: str, harness: str, target: Path,
            source: Path, project_key: str) -> list[dict]:
    # Mutate in place so a `links` list shared across install_project's calls
    # accumulates every entry before the single save.
    target_s = str(target)
    links[:] = [link for link in links if link.get("target") != target_s]
    links.append({
        "slug": slug,
        "harness": harness,
        "target": target_s,
        "source": str(source),
        "project_key": project_key,
        "installed_at": datetime.now(timezone.utc).isoformat(),
    })
    return links


def _forget(links: list[dict], target: Path) -> list[dict]:
    target_s = str(target)
    links[:] = [link for link in links if link.get("target") != target_s]
    return links


# ── Install / uninstall ────────────────────────────────────────────────────

def install_skill(
    skill: BundleSkill,
    harness: str,
    *,
    project_key: str = "",
    force: bool = False,
    _links: list[dict] | None = None,
    _persist: bool = True,
) -> InstallResult:
    """Symlink one bundle skill into a harness discovery dir.

    Returns an InstallResult describing what happened. The conflict policy:
    replace a watchmen-managed target, skip a user-made one unless ``force``.
    """
    base = harness_skill_dir(harness)
    if base is None:
        return InstallResult(skill.slug, harness, None, "skipped_no_dir",
                             f"no skill dir defined for harness '{harness}'")

    links = _load_manifest() if _links is None else _links
    target = base / skill.slug

    exists = target.exists() or target.is_symlink()
    replaced = False
    if exists:
        if _is_managed(target, links):
            replaced = True
        elif not force:
            return InstallResult(skill.slug, harness, target, "skipped_conflict",
                                 "target exists and was not created by watchmen")
        _remove_path(target)

    base.mkdir(parents=True, exist_ok=True)
    target.symlink_to(skill.skill_dir.resolve(), target_is_directory=True)
    links = _record(links, slug=skill.slug, harness=harness, target=target,
                    source=skill.skill_dir, project_key=project_key)
    if _persist:
        _save_manifest(links)
    return InstallResult(skill.slug, harness, target,
                         "replaced" if replaced else "installed")


def install_project(
    project_key: str,
    *,
    harnesses: list[str] | None = None,
    slugs: list[str] | None = None,
    force: bool = False,
) -> list[InstallResult]:
    """Install all (or a slug-filtered subset of) a project's bundle skills
    into the given harnesses (defaults to every known harness dir)."""
    targets = harnesses if harnesses is not None else list(HARNESS_SKILL_DIRS)
    wanted = set(slugs) if slugs is not None else None
    skills = [s for s in bundle_skills(project_key)
              if wanted is None or s.slug in wanted]

    links = _load_manifest()
    results: list[InstallResult] = []
    for skill in skills:
        for harness in targets:
            results.append(install_skill(
                skill, harness, project_key=project_key, force=force,
                _links=links, _persist=False,
            ))
    _save_manifest(links)
    return results


def uninstall_skill(slug: str, harness: str) -> InstallResult:
    """Remove a watchmen-installed link. Never removes a target watchmen
    doesn't own."""
    base = harness_skill_dir(harness)
    if base is None:
        return InstallResult(slug, harness, None, "skipped_no_dir",
                             f"no skill dir defined for harness '{harness}'")
    links = _load_manifest()
    target = base / slug
    if not (target.exists() or target.is_symlink()):
        return InstallResult(slug, harness, target, "not_installed")
    if not _is_managed(target, links):
        return InstallResult(slug, harness, target, "skipped_conflict",
                             "target was not created by watchmen")
    _remove_path(target)
    _save_manifest(_forget(links, target))
    return InstallResult(slug, harness, target, "uninstalled")


def installed_targets(project_key: str | None = None) -> list[dict]:
    """Manifest entries, optionally filtered to one project. Used by the viewer
    and CLI to show install status."""
    links = _load_manifest()
    if project_key is None:
        return links
    return [link for link in links if link.get("project_key") == project_key]


def _remove_path(target: Path) -> None:
    """Unlink a symlink or remove a directory/file target."""
    if target.is_symlink() or target.is_file():
        target.unlink()
    elif target.is_dir():
        import shutil
        shutil.rmtree(target)
