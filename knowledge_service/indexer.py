"""Provider-neutral repository indexer.

Scans one repository read-only and writes a JSONL manifest of indexable
documents. The indexer's own code never writes into ``--repo``; the only
write this program performs is the manifest at the caller-supplied ``--out``
path.

Read-only-safe invocation::

    python -B -m knowledge_service.indexer --repo <path> --scope <name> --out <file>

``-B`` is equivalent to setting ``PYTHONDONTWRITEBYTECODE=1`` or a
``PYTHONPYCACHEPREFIX`` temp directory. Python's import machinery may write
bytecode caches for this package unless it is invoked with ``-B`` (or one of
the equivalent environment settings); those caches are interpreter
behaviour, not writes performed by the indexer's own code.

Repository-specific exclusions come from ``knowledge_exclusions`` rows in
this service's own database, read read-only. Default exclusions are code
constants below, documented in ``README.md``.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sqlite3
import sys
from collections.abc import Generator
from datetime import datetime, timezone
from pathlib import Path

# The scanned repository may be this project itself. Never let Python write
# bytecode caches while the indexer runs, or a scan of the project checkout
# would violate the read-only guarantee.
sys.dont_write_bytecode = True

from knowledge_service import config  # noqa: E402


# ── Default exclusions (code constants) ─────────────────────────────────

_DEFAULT_EXCLUDED_NAMES = frozenset(
    {
        ".git",
        ".knowledgeignore",
        ".env",
        "__pycache__",
        "node_modules",
        "venv",
        ".venv",
        "logs",
        "jobs",
        ".flowrunner",
        ".pytest_cache",
        ".playwright-mcp",
        ".superpowers",
        ".ruff_cache",
        ".mypy_cache",
        ".claude",
        "knowledge_index",
        "dist",
        "build",
        "exports",
        "backups",
    }
)
_DEFAULT_EXCLUDED_PREFIXES = (".aider",)
_DEFAULT_EXCLUDED_SUFFIXES = frozenset(
    {
        ".pyc",
        ".db",
        ".sqlite",
        ".bin",
        ".log",
        ".jsonl",
        ".bak",
        ".tar",
        ".gz",
        ".zip",
        ".whl",
        ".parquet",
        ".leann",
        ".idx",
    }
)
_IMAGE_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".bmp", ".tiff"}
)

_SECRET_ASSIGNMENT_PREFIXES = ("SECRET=", "API_KEY=", "PASSWORD=", "TOKEN=")
_PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")


class RepoExclusionError(Exception):
    """Raised when repository-specific exclusions cannot be loaded."""


def _fail(message: str) -> None:
    """Print a clear error and exit nonzero without touching the scan target."""
    print(f"knowledge_service.indexer: error: {message}", file=sys.stderr)
    raise SystemExit(1)


def _default_name_excluded(name: str) -> bool:
    """Return True when a file or directory name matches a default exclusion."""
    if name in _DEFAULT_EXCLUDED_NAMES:
        return True
    # Covers .env and backup/template variants such as .env.bak or .env.example.
    if name.startswith(".env."):
        return True
    if name.startswith(_DEFAULT_EXCLUDED_PREFIXES):
        return True
    lowered = name.lower()
    return any(
        lowered.endswith(suffix)
        for suffix in _DEFAULT_EXCLUDED_SUFFIXES | _IMAGE_SUFFIXES
    )


def _contains_secret_markers(content: str) -> bool:
    """Return True when decoded content carries a private-key or secret marker."""
    if _PRIVATE_KEY_RE.search(content):
        return True
    for line in content.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(_SECRET_ASSIGNMENT_PREFIXES):
            return True
    return False


def cap_content(content: str, max_chars: int) -> tuple[str, bool]:
    """Return ``(stored_content, truncated)`` for one document.

    ``truncated`` is True exactly when ``len(content)`` exceeds ``max_chars``.
    ``stored_content`` is ``content[:max_chars]`` when truncated, otherwise the
    full ``content`` unchanged.
    """
    if len(content) > max_chars:
        return content[:max_chars], True
    return content, False


def _load_repo_exclusions(scope: str) -> list[tuple[str, str]]:
    """Load enabled ``knowledge_exclusions`` rows for ``scope``, read-only.

    The schema is inspected before querying so the column names are taken
    from the table that actually exists. The database path always comes from
    ``config.get_db_path()``. A database file that is not there yet means no
    exclusions have been configured, so the scan proceeds without them; a
    missing table or unreadable database is a hard error — repository-specific
    exclusions are never silently skipped.
    """
    db_path = config.get_db_path()
    if not Path(db_path).expanduser().exists():
        return []
    try:
        conn = sqlite3.connect(Path(db_path).as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise RepoExclusionError(
            f"cannot open knowledge database read-only at {db_path}: {exc}"
        ) from exc

    try:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name = 'knowledge_exclusions'"
        )
        if cur.fetchone() is None:
            raise RepoExclusionError(
                "knowledge_exclusions table is missing from "
                f"{db_path}; refusing to index without repository-specific exclusions"
            )

        cur.execute("PRAGMA table_info(knowledge_exclusions)")
        columns = {row[1] for row in cur.fetchall()}
        missing = {"scope", "pattern", "kind", "enabled"} - columns
        if missing:
            raise RepoExclusionError(
                "knowledge_exclusions is missing required columns: "
                f"{', '.join(sorted(missing))}"
            )

        cur.execute(
            "SELECT pattern, kind FROM knowledge_exclusions "
            "WHERE scope = ? AND enabled = 1",
            (scope,),
        )
        return [(row["kind"], row["pattern"]) for row in cur.fetchall()]
    except sqlite3.Error as exc:
        raise RepoExclusionError(
            f"cannot read knowledge_exclusions for scope {scope!r}: {exc}"
        ) from exc
    finally:
        conn.close()


def _matches_repo_exclusion(
    rel_path: str, is_dir: bool, exclusions: list[tuple[str, str]]
) -> bool:
    """Apply name and path repository-specific exclusions to a path."""
    name = rel_path.rsplit("/", 1)[-1]
    parts = rel_path.split("/")
    for kind, pattern in exclusions:
        if kind == "name":
            if name == pattern:
                return True
        elif kind == "path":
            normalized = pattern.rstrip("/")
            if not normalized:
                continue
            if (
                rel_path == normalized
                or rel_path.startswith(normalized + "/")
                or normalized in parts
            ):
                return True
    return False


def _load_knowledgeignore(repo_path: Path) -> list[tuple[str, bool]]:
    """Load ``<repo>/.knowledgeignore`` patterns, or [] when the file is absent.

    One pattern per line; blank lines and ``#`` comments are ignored. A
    trailing ``/`` marks a directory pattern. A present-but-unreadable file is
    a hard error, never a silent skip.
    """
    path = repo_path / ".knowledgeignore"
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        _fail(f"cannot read .knowledgeignore at {path}: {exc}")

    patterns: list[tuple[str, bool]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        is_dir = line.endswith("/")
        pattern = line.rstrip("/") if is_dir else line
        if pattern:
            patterns.append((pattern, is_dir))
    return patterns


def _matches_knowledgeignore(
    rel_path: str, is_dir: bool, patterns: list[tuple[str, bool]]
) -> bool:
    """Return True when ``rel_path`` matches a repository ``.knowledgeignore`` rule.

    Directory patterns apply only to directories; file patterns apply only to
    files. Patterns match the repository-relative path and the basename.
    """
    name = rel_path.rsplit("/", 1)[-1]
    for pattern, pattern_is_dir in patterns:
        if pattern_is_dir != is_dir:
            continue
        if fnmatch.fnmatch(rel_path, pattern) or fnmatch.fnmatch(name, pattern):
            return True
    return False


def _iter_documents(
    repo_path: Path,
    exclusions: list[tuple[str, str]],
    knowledgeignore_patterns: list[tuple[str, bool]] | None = None,
) -> Generator[tuple[str, str, int], None, None]:
    """Yield ``(relative_path, content, size_bytes)`` for indexable files.

    ``knowledgeignore_patterns`` may be preloaded by the caller; when it is
    ``None`` the repository's ``.knowledgeignore`` is loaded here so callers
    that reuse this walk (``knowledge_service.maintenance.detect_changes``)
    see the same files a fresh index sees.
    """
    if knowledgeignore_patterns is None:
        knowledgeignore_patterns = _load_knowledgeignore(repo_path)
    content_patterns = [
        pattern for kind, pattern in exclusions if kind == "content"
    ]

    for dirpath, dirnames, filenames in os.walk(repo_path):
        dirnames.sort()
        filenames.sort()

        kept_dirs = []
        for dirname in dirnames:
            dir_abs = os.path.join(dirpath, dirname)
            if os.path.islink(dir_abs):
                continue
            rel_dir = Path(dir_abs).relative_to(repo_path).as_posix()
            if (
                _default_name_excluded(dirname)
                or _matches_repo_exclusion(rel_dir, True, exclusions)
                or _matches_knowledgeignore(
                    rel_dir, True, knowledgeignore_patterns
                )
            ):
                continue
            kept_dirs.append(dirname)
        dirnames[:] = kept_dirs

        for filename in filenames:
            file_abs = os.path.join(dirpath, filename)
            if os.path.islink(file_abs):
                continue
            rel_path = Path(file_abs).relative_to(repo_path).as_posix()
            if (
                _default_name_excluded(filename)
                or _matches_repo_exclusion(rel_path, False, exclusions)
                or _matches_knowledgeignore(
                    rel_path, False, knowledgeignore_patterns
                )
            ):
                continue

            try:
                content = Path(file_abs).read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                # Non-text/binary content and unreadable files are excluded.
                continue

            if _contains_secret_markers(content):
                continue
            if any(pattern in content for pattern in content_patterns):
                continue

            try:
                size_bytes = os.path.getsize(file_abs)
            except OSError:
                continue

            yield rel_path, content, size_bytes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Index a repository read-only and write a JSONL manifest to --out."
        )
    )
    parser.add_argument("--repo", required=True, help="repository path to scan")
    parser.add_argument("--scope", required=True, help="scope recorded on every document")
    parser.add_argument("--out", required=True, help="manifest file to write (JSONL)")
    parser.add_argument(
        "--max-document-chars",
        type=int,
        default=None,
        help=(
            "maximum characters retained from one indexed document "
            "(defaults to the configured value)"
        ),
    )
    args = parser.parse_args(argv)

    max_chars = (
        args.max_document_chars
        if args.max_document_chars is not None
        else config.get_max_document_chars()
    )
    if max_chars < 1:
        _fail("--max-document-chars must be positive")

    repo_path = Path(args.repo).expanduser()
    if not repo_path.exists():
        _fail(f"--repo does not exist: {repo_path}")
    if not repo_path.is_dir():
        _fail(f"--repo is not a directory: {repo_path}")
    repo_path = repo_path.resolve()

    out_path = Path(args.out).expanduser()
    if not out_path.parent.exists():
        _fail(f"--out parent directory does not exist: {out_path.parent}")
    if not out_path.parent.is_dir():
        _fail(f"--out parent is not a directory: {out_path.parent}")
    out_path = out_path.resolve()
    if out_path.exists() and out_path.is_dir():
        _fail(f"--out is a directory, not a file: {out_path}")
    if out_path.is_relative_to(repo_path):
        _fail(
            "--out must be outside --repo: the indexer never writes inside "
            "the scanned repository"
        )

    try:
        exclusions = _load_repo_exclusions(args.scope)
    except RepoExclusionError as exc:
        _fail(str(exc))
    knowledgeignore_patterns = _load_knowledgeignore(repo_path)

    count = 0
    try:
        with open(out_path, "w", encoding="utf-8", newline="\n") as handle:
            for rel_path, content, size_bytes in _iter_documents(
                repo_path, exclusions, knowledgeignore_patterns
            ):
                stored_content, truncated = cap_content(content, max_chars)
                record = {
                    "scope": args.scope,
                    "path": rel_path,
                    "content": stored_content,
                    "size_bytes": size_bytes,
                    "indexed_at": datetime.now(timezone.utc).isoformat(),
                }
                if truncated:
                    record["truncated"] = True
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                count += 1
    except OSError as exc:
        _fail(f"could not write manifest at {out_path}: {exc}")

    print(f"indexed {count} document(s) from {repo_path} into {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    main()
