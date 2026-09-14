"""This service's own SQLite store.

The service owns ``knowledge.db`` (path from ``config.get_db_path()``) and
creates its schema itself. The four tables are exactly the ones DPMtF
created through its migrations 107, 109 and 110, kept column-for-column so a
one-time ``import-registry`` can copy rows straight across:

* ``knowledge_indexes``       per-scope index registry (migration 107, with
                              migration 110's status triggers).
* ``knowledge_exclusions``    repository-specific exclusion rules (107).
* ``knowledge_retrieval_log`` append-only retrieval history (107, plus run
                              034's nullable ``flow_key``, which records the
                              caller's flow key for per-workspace auditing).
* ``knowledge_scope_grants``  explicit grants for internal scopes (109).

Every statement is idempotent (``IF NOT EXISTS``), so ``connect()`` can be
called from any entry point without a migration step. ``ensure_schema`` also
adds ``flow_key`` to an older retrieval-log table in place, so a database from
before run 034 keeps working and keeps its rows. Parameterized SQL only
everywhere else in the package.

A database whose grant table is still empty gets one baseline grant: the
configured default scope for this installation's supervisor role, with no
flow restriction (see :func:`ensure_baseline_grant`). After that the table is
owned by ``grant`` and ``revoke``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from knowledge_service import config

__all__ = [
    "SCHEMA_SQL",
    "REGISTRY_TABLES",
    "SUPERVISOR_ROLE",
    "connect",
    "connect_read_only",
    "ensure_schema",
    "ensure_baseline_grant",
]

# Role that reads the installation's own default scope without a grant row
# having to be written by hand first.
SUPERVISOR_ROLE = "dsh"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS knowledge_indexes (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    scope          TEXT NOT NULL UNIQUE,
    provider       TEXT NOT NULL,
    location       TEXT NOT NULL DEFAULT '',
    document_count INTEGER NOT NULL DEFAULT 0 CHECK (document_count >= 0),
    status         TEXT NOT NULL DEFAULT 'unknown',
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_knowledge_indexes_provider
    ON knowledge_indexes (provider);

CREATE TABLE IF NOT EXISTS knowledge_exclusions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    scope      TEXT NOT NULL,
    pattern    TEXT NOT NULL,
    kind       TEXT NOT NULL CHECK (kind IN ('path', 'name', 'content')),
    enabled    INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (scope, pattern)
);

CREATE INDEX IF NOT EXISTS idx_knowledge_exclusions_scope
    ON knowledge_exclusions (scope, enabled);

CREATE TABLE IF NOT EXISTS knowledge_retrieval_log (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    provider              TEXT NOT NULL,
    scope                 TEXT NOT NULL,
    query                 TEXT NOT NULL,
    result_count          INTEGER NOT NULL DEFAULT 0 CHECK (result_count >= 0),
    sources               TEXT NOT NULL DEFAULT '[]',
    retrieved_token_count INTEGER NOT NULL DEFAULT 0 CHECK (retrieved_token_count >= 0),
    retrieval_duration_ms INTEGER NOT NULL DEFAULT 0 CHECK (retrieval_duration_ms >= 0),
    agent_role            TEXT,
    run_id                TEXT,
    handoff_id            TEXT,
    created_at            TEXT NOT NULL DEFAULT (datetime('now')),
    -- Added after ``handoff_id`` (run 034). It stays last so an older database
    -- upgraded with ``ALTER TABLE ... ADD COLUMN`` ends up in the same column
    -- order as one created here; see ``_upgrade_retrieval_log``.
    flow_key              TEXT DEFAULT NULL
);

CREATE INDEX IF NOT EXISTS idx_knowledge_retrieval_log_scope_time
    ON knowledge_retrieval_log (scope, created_at);

CREATE INDEX IF NOT EXISTS idx_knowledge_retrieval_log_run
    ON knowledge_retrieval_log (run_id, handoff_id);

CREATE TABLE IF NOT EXISTS knowledge_scope_grants (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    scope      TEXT NOT NULL,
    agent_role TEXT,
    flow_key   TEXT,
    granted_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (scope, agent_role, flow_key)
);

DROP TRIGGER IF EXISTS knowledge_indexes_status_check_insert;
CREATE TRIGGER IF NOT EXISTS knowledge_indexes_status_check_insert
BEFORE INSERT ON knowledge_indexes
FOR EACH ROW
WHEN NEW.status IS NULL OR NEW.status NOT IN ('noop', 'changed', 'missing', 'empty')
BEGIN
    SELECT RAISE(ABORT,
        'knowledge_indexes.status must be ''noop'' or ''changed'' or ''missing'' or ''empty''');
END;

DROP TRIGGER IF EXISTS knowledge_indexes_status_check_update;
CREATE TRIGGER IF NOT EXISTS knowledge_indexes_status_check_update
BEFORE UPDATE OF status ON knowledge_indexes
FOR EACH ROW
WHEN NEW.status IS NULL OR NEW.status NOT IN ('noop', 'changed', 'missing', 'empty')
BEGIN
    SELECT RAISE(ABORT,
        'knowledge_indexes.status must be ''noop'' or ''changed'' or ''missing'' or ''empty''');
END;
"""

# The four tables a registry import copies, in dependency order.
REGISTRY_TABLES = (
    "knowledge_indexes",
    "knowledge_scope_grants",
    "knowledge_exclusions",
    "knowledge_retrieval_log",
)


def _upgrade_retrieval_log(conn: sqlite3.Connection) -> None:
    """Add ``flow_key`` to a retrieval log created before that column existed.

    A database written by the 3A-1 schema has the table without ``flow_key``.
    ``CREATE TABLE IF NOT EXISTS`` leaves such a table untouched, so the
    missing column is detected through ``pragma table_info`` and added in
    place; existing rows keep their values and read back with a NULL
    ``flow_key``. Detection by column name (not by a version stamp) means the
    upgrade is a no-op on any database that already has the column.
    """
    columns = [row[1] for row in conn.execute(
        "PRAGMA table_info(knowledge_retrieval_log)"
    ).fetchall()]
    if not columns:
        # No table at all in this database: the statements above created it
        # with the column already present.
        return None
    if "flow_key" not in columns:
        conn.execute(
            "ALTER TABLE knowledge_retrieval_log ADD COLUMN flow_key TEXT DEFAULT NULL"
        )
    return None


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the four knowledge tables and upgrade the retrieval log."""
    conn.executescript(SCHEMA_SQL)
    _upgrade_retrieval_log(conn)
    conn.commit()
    return None


def ensure_baseline_grant(conn: sqlite3.Connection) -> None:
    """Give this installation's own supervisor role its default scope.

    A fresh database has no grants at all, which would lock the service's own
    role out of the scope configured as ``[knowledge] scope``. So when the
    grant table is still empty, one grant is recorded for that scope with the
    supervisor role and no flow restriction. Once any grant exists the table
    is owned by ``grant``/``revoke`` and nothing is added here. Checked by the
    exact triple (``flow_key IS NULL``) rather than by the ``UNIQUE``
    constraint, because SQLite treats ``NULL`` values as distinct there.
    """
    if conn.execute(
        "SELECT 1 FROM knowledge_scope_grants LIMIT 1"
    ).fetchone() is not None:
        return None

    scope = config.get_scope()
    existing = conn.execute(
        "SELECT 1 FROM knowledge_scope_grants"
        " WHERE scope = ? AND agent_role = ? AND flow_key IS NULL"
        " LIMIT 1",
        (scope, SUPERVISOR_ROLE),
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO knowledge_scope_grants (scope, agent_role, flow_key)"
            " VALUES (?, ?, NULL)",
            (scope, SUPERVISOR_ROLE),
        )
        conn.commit()
    return None


def connect(db_path: str | None = None) -> sqlite3.Connection:
    """Return a connection to this service's database with schema ensured."""
    path = Path(db_path or config.get_db_path()).expanduser()
    if str(path.parent):
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    ensure_baseline_grant(conn)
    return conn


def connect_read_only(db_path: str) -> sqlite3.Connection:
    """Return a read-only connection to another database file.

    Used by the one-time registry import. Never creates or mutates the
    source; a missing file raises so the CLI can report it cleanly.
    """
    path = Path(db_path).expanduser().resolve()
    uri = path.as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn
