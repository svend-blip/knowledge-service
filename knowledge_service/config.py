"""Configuration for the standalone knowledge service.

One INI file carries everything the service needs. The path is taken from
the environment variable ``KNOWLEDGE_SERVICE_INI``; when that is unset the
first existing candidate is ``~/.config/knowledge-service/knowledge.ini``
and the last is ``./knowledge.ini`` in the current working directory. A
missing file is not an error: every getter has the default its key had in
DPMtF's ``[knowledge]`` section, so the service runs on a bare host with no
INI at all.

The ``[knowledge]`` keys and defaults are the ones DPMtF carries today
(enabled/provider/scope/top_k/max_context_tokens/max_document_chars/
index_dir/min_free_vram_mib/leann_use_daemon). The ``[service]`` section is
this service's own: host, port, db_path, token and father_root. Nothing in
the package reads a path from anywhere else, so tests can point the whole
service at temp directories through ``KNOWLEDGE_SERVICE_INI``.
"""

from __future__ import annotations

import os
from configparser import ConfigParser
from pathlib import Path

__all__ = [
    "ENV_INI_PATH",
    "ini_path",
    "reload",
    "get_enabled",
    "get_provider",
    "get_scope",
    "get_top_k",
    "get_max_context_tokens",
    "get_max_document_chars",
    "get_index_dir",
    "get_min_free_vram_mib",
    "get_leann_use_daemon",
    "get_host",
    "get_port",
    "get_db_path",
    "get_token",
    "get_father_root",
]

ENV_INI_PATH = "KNOWLEDGE_SERVICE_INI"

_KNOWLEDGE_SECTION = "knowledge"
_SERVICE_SECTION = "service"

_INDEX_DIR_DEFAULT = ".local/share/dpmtf/knowledge_index"
_DB_PATH_DEFAULT = ".local/share/knowledge-service/knowledge.db"

_PARSER: ConfigParser | None = None
_SOURCE: str = ""


def _candidate_paths() -> list[Path]:
    """Return the INI candidates in precedence order."""
    candidates: list[Path] = []
    configured = os.environ.get(ENV_INI_PATH, "").strip()
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.append(Path("~/.config/knowledge-service/knowledge.ini").expanduser())
    candidates.append(Path("knowledge.ini"))
    return candidates


def _parser() -> ConfigParser:
    """Return the loaded parser, loading it on first use."""
    global _PARSER
    if _PARSER is None:
        _PARSER = _load()
    return _PARSER


def _load() -> ConfigParser:
    """Read the first existing candidate INI into a fresh parser."""
    global _SOURCE
    parser = ConfigParser()
    for candidate in _candidate_paths():
        try:
            exists = candidate.is_file()
        except OSError:
            exists = False
        if exists:
            parser.read(candidate, encoding="utf-8")
            _SOURCE = str(candidate)
            return parser
    _SOURCE = ""
    return parser


def reload() -> None:
    """Drop the cached parser so the next getter call re-reads the INI."""
    global _PARSER
    _PARSER = None
    return None


def ini_path() -> str:
    """Return the INI file actually read, or ``""`` when defaults apply."""
    _parser()
    return _SOURCE


def _resolved_under_home(value: str) -> str:
    """Return an absolute path; a relative value resolves against the home dir."""
    path = Path(value).expanduser()
    if path.is_absolute():
        return str(path.resolve())
    return str((Path.home() / value).resolve())


# ── [knowledge] keys (same names and defaults as DPMtF today) ───────────


def get_enabled() -> bool:
    """Whether knowledge retrieval is enabled. Disabled by default."""
    return _parser().getboolean(_KNOWLEDGE_SECTION, "enabled", fallback=False)


def get_provider() -> str:
    """Knowledge provider key. ``none`` (no-op) by default."""
    return _parser().get(_KNOWLEDGE_SECTION, "provider", fallback="none")


def get_scope() -> str:
    """The default scope of this installation."""
    return _parser().get(_KNOWLEDGE_SECTION, "scope", fallback="dpmtf-webui")


def get_top_k() -> int:
    """Default number of retrieval results to return."""
    return _parser().getint(_KNOWLEDGE_SECTION, "top_k", fallback=8)


def get_max_context_tokens() -> int:
    """Default token budget for retrieved knowledge context."""
    return _parser().getint(_KNOWLEDGE_SECTION, "max_context_tokens", fallback=12000)


def get_max_document_chars() -> int:
    """Maximum characters retained from one indexed document."""
    return _parser().getint(_KNOWLEDGE_SECTION, "max_document_chars", fallback=20000)


def get_index_dir() -> str:
    """Knowledge index directory, shared by every scope of this installation.

    An absolute value in the INI is returned unchanged; a relative value is
    resolved against ``Path.home()``, never against the repository.
    """
    configured = _parser().get(
        _KNOWLEDGE_SECTION, "index_dir", fallback=_INDEX_DIR_DEFAULT
    )
    return _resolved_under_home(configured)


def get_min_free_vram_mib() -> int:
    """Minimum free GPU memory in MiB before retrieval may run."""
    return _parser().getint(_KNOWLEDGE_SECTION, "min_free_vram_mib", fallback=4096)


def get_leann_use_daemon() -> bool:
    """Whether LEANN should spawn the embedding-server daemon."""
    return _parser().getboolean(_KNOWLEDGE_SECTION, "leann_use_daemon", fallback=False)


# ── [service] keys (this service's own) ─────────────────────────────────


def get_host() -> str:
    """Bind address of the HTTP service."""
    return _parser().get(_SERVICE_SECTION, "host", fallback="127.0.0.1")


def get_port() -> int:
    """Bind port of the HTTP service."""
    return _parser().getint(_SERVICE_SECTION, "port", fallback=9140)


def get_db_path() -> str:
    """SQLite file owned by this service."""
    configured = _parser().get(_SERVICE_SECTION, "db_path", fallback=_DB_PATH_DEFAULT)
    return _resolved_under_home(configured)


def get_token() -> str:
    """Shared token required in ``X-Knowledge-Token``; empty means no auth."""
    return _parser().get(_SERVICE_SECTION, "token", fallback="").strip()


def get_father_root() -> str:
    """The repository path that maps to the configured default scope."""
    return _parser().get(_SERVICE_SECTION, "father_root", fallback="").strip()
