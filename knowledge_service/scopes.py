"""Deterministic knowledge-scope resolution for every client.

Each caller asks for knowledge about the repository it operates on. This
module turns a filesystem path into the scope slug so no client has to
duplicate the rule: ``GET /v1/scope-for-path`` exposes exactly this function.

The rule is platform-neutral on purpose. Windows and POSIX paths are both
accepted: trailing separators of either kind are stripped, the final
directory name is lowercased, and drive-letter case is ignored, so
``C:\\Projects\\FlowRunner\\`` and ``/srv/projects/FlowRunner/`` resolve to the
same ``flowrunner``. A path equal to the configured ``father_root`` (the
installation's own repository) keeps the configured default scope, because
that repository's knowledge is the default scope, not a foreign one.

Provider-neutral by design: it imports nothing from the provider layer and
only resolves the scope string. The provider and the scope guard receive
that string unchanged.
"""

from __future__ import annotations

from pathlib import Path

from knowledge_service import config

__all__ = ["scope_for_path"]


def _strip_separators(text: str) -> str:
    """Return ``text`` without trailing POSIX or Windows separators."""
    stripped = text.rstrip("/\\")
    # A lone drive letter (``C:``) or the POSIX root must not collapse to "".
    return stripped if stripped else text[:1]


def _final_component(text: str) -> str:
    """Return the lowercased final directory name of either path style."""
    parts = [part for part in text.replace("\\", "/").split("/") if part]
    return parts[-1].lower() if parts else ""


def _canonical_location(text: str) -> str:
    """Return a comparable location string for either path style.

    Home is expanded, trailing separators are stripped, and only the drive
    letter's case is normalised — the rest of the path keeps its case so two
    genuinely different POSIX directories are not folded onto each other.
    """
    value = _strip_separators(str(text).strip())
    if len(value) > 1 and value[1:2] == ":" and value[0].isalpha():
        value = value[0].lower() + value[1:]
    try:
        return str(Path(value).expanduser().resolve())
    except OSError:
        return str(Path(value).expanduser())


def scope_for_path(path: str | None) -> str:
    """Return the knowledge scope a repository path belongs to.

    The scope is the lowercased final directory name with trailing ``/`` or
    ``\\`` stripped and drive-letter case ignored. A path whose final
    component is empty after stripping returns the configured default scope,
    and so does a path that resolves to the configured ``father_root``.
    Never raises.
    """
    text = "" if path is None else str(path).strip()

    father_root = config.get_father_root()
    if father_root and text and _canonical_location(text) == _canonical_location(
        father_root
    ):
        return config.get_scope()

    component = _final_component(_strip_separators(text))
    if not component:
        return config.get_scope()
    return component
