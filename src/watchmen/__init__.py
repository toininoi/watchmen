"""watchmen — local Claude Code session intelligence.

Observes your coding-agent sessions, analyzes them longitudinally, and
auto-generates skill bundles + CLAUDE.md per project. All runs locally on
your machine.

Public entry points:

    watchmen.cli.main         CLI dispatch (the `watchmen` console script)
    watchmen.daemon.main      background scheduler
    watchmen.viewer.server    FastAPI viewer at 127.0.0.1:8979
    watchmen.hook_server      hook capture server at 127.0.0.1:8765

Most users interact via the `watchmen` console script. Most contributors
won't import this package directly — the modules are intended to be invoked
as scripts via the CLI dispatcher.
"""

from __future__ import annotations

# PyPI dist name is `dria-watchmen` (the plain `watchmen` namespace was
# already claimed). Try both names so editable installs from before the
# rename still resolve a real version.
try:
    from importlib.metadata import PackageNotFoundError, version as _pkg_version

    for _name in ("dria-watchmen", "watchmen"):
        try:
            __version__ = _pkg_version(_name)
            break
        except PackageNotFoundError:
            continue
    else:
        __version__ = "0.0.0+local"
except Exception:  # pragma: no cover — happens when run from a non-installed checkout
    __version__ = "0.0.0+local"
