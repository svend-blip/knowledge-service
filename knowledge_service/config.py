"""Configuration for the standalone knowledge service.

One INI file carries everything the service needs. The path is taken from
the environment variable ``KNOWLEDGE_SERVICE_INI``; when that is unset the
first existing candidate is ``~/.config/knowledge-service/knowledge.ini``
and the last is ``./knowledge.ini`` in the current working directory. A
missing file is not an error: every getter has the default its key had in
DPMtF's ``[knowledge]`` section, so the service runs on a bare host with no
INI at all. The environment is consulted on every getter call, so changing
``KNOWLEDGE_SERVICE_INI`` takes effect immediately — a test can point the whole
service at its own temporary directories at any point.

The ``[knowledge]`` keys and defaults are the ones DPMtF carries today
(enabled/provider/scope/top_k/max_context_tokens/max_document_chars/
index_dir/min_free_vram_mib/leann_use_daemon). The ``[service]`` section is
this service's own: host, port, db_path, token and father_root. The
``[portable]`` section holds the CPU provider's model directory and model id.
Nothing in the package reads a path from anywhere else, so tests can point the
whole service at temp directories through ``KNOWLEDGE_SERVICE_INI``.
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
    "get_portable_model_dir",
    "get_portable_model_id",
    "get_learning_dir",
    "get_learning_runs_root",
]

ENV_INI_PATH = "KNOWLEDGE_SERVICE_INI"

_KNOWLEDGE_SECTION = "knowledge"
_SERVICE_SECTION = "service"
_PORTABLE_SECTION = "portable"
_LEARNING_SECTION = "learning"

_INDEX_DIR_DEFAULT = ".local/share/dpmtf/knowledge_index"
_DB_PATH_DEFAULT = ".local/share/knowledge-service/knowledge.db"
_MODEL_DIR_DEFAULT = (
    ".local/share/knowledge-service/models/"
    "paraphrase-multilingual-MiniLM-L12-v2"
)
_MODEL_ID_DEFAULT = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

_PARSER: ConfigParser | None = None
_LOADED_ENV: str | None = None
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


def _current_env() -> str:
    """The value of the INI-path variable right now (``""`` when unset)."""
    return os.environ.get(ENV_INI_PATH, "").strip()


def _parser() -> ConfigParser:
    """Return the loaded parser, reloading it whenever the env changed.

    The parser is cached, but the cache belongs to the environment value it was
    loaded under. When ``KNOWLEDGE_SERVICE_INI`` changes — a second test, a new
    process environment, a supervisor pointing at another installation — the
    next getter call re-reads instead of continuing to serve the previous
    caller's paths. Without that check the first getter call in a process would
    pin every later one to whichever INI happened to exist at that moment, and
    a test that isolated itself afterwards would still write into the
    operator's database. ``reload()`` stays available for an explicit drop.
    """
    global _PARSER, _LOADED_ENV
    env = _current_env()
    if _PARSER is None or env != _LOADED_ENV:
        _PARSER = _load()
        _LOADED_ENV = env
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
    """Drop the cached parser so the next getter call re-reads the INI.

    Only needed when the INI *file* changed under an unchanged environment; a
    different ``KNOWLEDGE_SERVICE_INI`` value is picked up automatically.
    """
    global _PARSER, _LOADED_ENV
    _PARSER = None
    _LOADED_ENV = None
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


# ── [portable] keys (the CPU provider's own model) ──────────────────────


def get_portable_model_dir() -> str:
    """Directory holding the portable provider's ONNX model files.

    ``onnx/model.onnx`` and ``tokenizer.json`` are read from here. An absolute
    value is returned unchanged; a relative value resolves against
    ``Path.home()``, so the default sits beside the service database.
    """
    configured = _parser().get(
        _PORTABLE_SECTION, "model_dir", fallback=_MODEL_DIR_DEFAULT
    )
    return _resolved_under_home(configured)


def get_portable_model_id() -> str:
    """Model id recorded on every passage the portable provider writes."""
    return (
        _parser().get(_PORTABLE_SECTION, "model_id", fallback=_MODEL_ID_DEFAULT).strip()
    )


# ── [learning] keys (validated learning artifacts) ──────────────────────


def get_learning_dir() -> str:
    """Directory holding learning artifacts, manifests and the ledger.

    ``[learning] dir``; the default is ``learning`` inside the shared index
    directory. An absolute value is returned unchanged; a relative value
    resolves against ``Path.home()``, like every other path here.
    """
    configured = _parser().get(_LEARNING_SECTION, "dir", fallback="").strip()
    if configured:
        return _resolved_under_home(configured)
    return str(Path(get_index_dir()) / "learning")


def get_learning_runs_root() -> str:
    """Root that proves a run closed SUCCESS (``[learning] runs_root``).

    The closure check reads ``<runs_root>/<family>/runs/<run>/END-REPORT.md``
    there. With no value configured the default is the installation's father
    root plus ``.flowrunner`` — the run directories live beside the main
    checkout — falling back to ``.flowrunner`` under the home directory when
    no father root is configured. When the directory does not exist, the
    check is skipped with a warning so a foreign machine can still admit by
    hand.
    """
    configured = _parser().get(_LEARNING_SECTION, "runs_root", fallback="").strip()
    if configured:
        return _resolved_under_home(configured)
    father_root = get_father_root()
    if father_root:
        return str(Path(father_root).expanduser() / ".flowrunner")
    return str(Path.home() / ".flowrunner")
