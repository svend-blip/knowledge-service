"""Single writer for the ``knowledge_retrieval_log`` append-only table.

This module is the one place that writes rows to ``knowledge_retrieval_log``.
Every search that reaches a provider records its retrieval through
``record_retrieval`` so every real provider call leaves exactly one auditable
row behind. The read side lives here too: ``query_retrievals`` pages the log
newest-first and ``summarise_retrievals`` totals it, both against the
configured database opened read-only and both answering with the empty state
instead of raising when the database or table is missing.
``prune_retrievals`` is the one deliberate deletion path: dry by default, and
every real deletion appends one line to ``RETRIEVAL-LEDGER.md`` beside the
learning ledger. Deletion is an operator act at the host, so there is
deliberately no HTTP route for it.

Provider-neutral by construction: the module imports only this package's
config and database helpers, the standard library, and FastAPI's
``HTTPException``. It never names or imports a concrete knowledge provider.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from knowledge_service import config, db

__all__ = [
    "record_retrieval",
    "query_retrievals",
    "summarise_retrievals",
    "prune_retrievals",
    "retrieval_ledger_path",
    "RETRIEVAL_LEDGER_NAME",
]

logger = logging.getLogger(__name__)


def record_retrieval(
    *,
    provider: str,
    scope: str | None,
    query: str,
    results: list,
    duration_ms: int,
    agent_role: str | None,
    run_id: str | None,
    handoff_id: str | None,
    flow_key: str | None = None,
) -> None:
    """Append one ``knowledge_retrieval_log`` row for a real retrieval.

    ``flow_key`` is the caller's flow key (nullable): it makes the retrieval
    auditable per workspace without a second query join. The remaining columns
    are the provider key, scope, query, result count, JSON source list,
    retrieved token count, duration, agent role, run id and handoff id.

    Parameterized SQL only (``?`` placeholders, never string concatenation).
    A database failure is logged and surfaced to the caller as a 500 rather
    than swallowed silently.
    """
    sources = json.dumps([item["path"] for item in results])
    token_count = sum(len(item.get("content", "").split()) for item in results)

    conn = None
    try:
        conn = db.connect()
        conn.execute(
            "INSERT INTO knowledge_retrieval_log "
            "(provider, scope, query, result_count, sources, "
            "retrieved_token_count, retrieval_duration_ms, "
            "agent_role, run_id, handoff_id, flow_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                provider,
                scope or "",
                query,
                len(results),
                sources,
                token_count,
                duration_ms,
                agent_role,
                run_id,
                handoff_id,
                flow_key,
            ),
        )
        conn.commit()
    except Exception as exc:
        logger.error("knowledge retrieval log insert failed: %s", exc)
        raise HTTPException(
            status_code=500,
            detail="Failed to record knowledge retrieval",
        ) from exc
    finally:
        if conn is not None:
            conn.close()


# ── reading the log ──────────────────────────────────────────────────────

_EQUAL_FILTERS = ("run_id", "handoff_id", "flow_key", "agent_role", "scope")


def _conditions(
    *,
    run_id: str | None,
    handoff_id: str | None,
    flow_key: str | None,
    agent_role: str | None,
    scope: str | None,
    since: str | None,
    until: str | None,
) -> tuple[str, list]:
    """Build the AND-combined WHERE clause; unknown filters stay absent."""
    values = {
        "run_id": run_id,
        "handoff_id": handoff_id,
        "flow_key": flow_key,
        "agent_role": agent_role,
        "scope": scope,
    }
    clauses: list[str] = []
    params: list = []
    for key in _EQUAL_FILTERS:
        if values[key] is not None:
            clauses.append(f"{key} = ?")
            params.append(values[key])
    if since is not None:
        clauses.append("replace(created_at, ' ', 'T') >= ?")
        params.append(str(since).replace(" ", "T"))
    if until is not None:
        clauses.append("replace(created_at, ' ', 'T') <= ?")
        params.append(str(until).replace(" ", "T"))
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def _decode_sources(value) -> list:
    """Decode the stored JSON source list; anything odd yields ``[]``."""
    if isinstance(value, list):
        return value
    if not isinstance(value, str):
        return []
    try:
        parsed = json.loads(value)
    except ValueError:
        return []
    if isinstance(parsed, list):
        return parsed
    if parsed is None:
        return []
    return [parsed]


def _open_log_database():
    """Open the configured database read-only; ``None`` when unusable."""
    try:
        return db.connect_read_only(config.get_db_path())
    except (sqlite3.Error, OSError):
        return None


def query_retrievals(
    *,
    run_id: str | None = None,
    handoff_id: str | None = None,
    flow_key: str | None = None,
    agent_role: str | None = None,
    scope: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Page the retrieval log newest-first; empty state, never an exception.

    Every filter is optional and combines with AND; ``since``/``until``
    compare against ``created_at``. ``limit`` clamps to 1..500, ``offset``
    to >= 0. Each row carries every column with ``sources`` decoded into a
    list; ``total`` counts the matching rows before the limit. The database
    opens read-only and is never created: a missing database or table is
    ``{"rows": [], "total": 0, ...}``. Parameterized SQL only.
    """
    try:
        size = int(limit)
    except (TypeError, ValueError):
        size = 50
    try:
        start = int(offset)
    except (TypeError, ValueError):
        start = 0
    size = min(max(size, 1), 500)
    start = max(start, 0)

    where, params = _conditions(
        run_id=run_id, handoff_id=handoff_id, flow_key=flow_key,
        agent_role=agent_role, scope=scope, since=since, until=until,
    )
    conn = _open_log_database()
    if conn is None:
        return {"rows": [], "total": 0, "limit": size, "offset": start}
    try:
        total = conn.execute(
            f"SELECT COUNT(*) FROM knowledge_retrieval_log{where}", params
        ).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM knowledge_retrieval_log{where}"
            " ORDER BY replace(created_at, ' ', 'T') DESC, id DESC"
            " LIMIT ? OFFSET ?",
            [*params, size, start],
        ).fetchall()
    except sqlite3.Error:
        return {"rows": [], "total": 0, "limit": size, "offset": start}
    finally:
        conn.close()

    decoded: list[dict] = []
    for row in rows:
        item = dict(row)
        item["sources"] = _decode_sources(item.get("sources"))
        decoded.append(item)
    return {
        "rows": decoded,
        "total": int(total),
        "limit": size,
        "offset": start,
    }


def summarise_retrievals(
    *,
    run_id: str | None = None,
    handoff_id: str | None = None,
    flow_key: str | None = None,
    agent_role: str | None = None,
    scope: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> dict:
    """Total every matching row; no paging, empty state on failure."""
    empty = {
        "retrievals": 0,
        "results": 0,
        "tokens": 0,
        "duration_ms": 0,
        "scopes": {},
        "agent_roles": {},
        "first": None,
        "last": None,
    }
    where, params = _conditions(
        run_id=run_id, handoff_id=handoff_id, flow_key=flow_key,
        agent_role=agent_role, scope=scope, since=since, until=until,
    )
    conn = _open_log_database()
    if conn is None:
        return dict(empty)
    try:
        totals = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(result_count), 0),"
            " COALESCE(SUM(retrieved_token_count), 0),"
            " COALESCE(SUM(retrieval_duration_ms), 0),"
            f" MIN(created_at), MAX(created_at) FROM knowledge_retrieval_log{where}",
            params,
        ).fetchone()
        scopes = conn.execute(
            "SELECT COALESCE(scope, ''), COUNT(*) FROM knowledge_retrieval_log"
            f"{where} GROUP BY COALESCE(scope, '')"
            " ORDER BY COUNT(*) DESC, COALESCE(scope, '')",
            params,
        ).fetchall()
        roles = conn.execute(
            "SELECT COALESCE(agent_role, ''), COUNT(*)"
            f" FROM knowledge_retrieval_log{where} GROUP BY COALESCE(agent_role, '')"
            " ORDER BY COUNT(*) DESC, COALESCE(agent_role, '')",
            params,
        ).fetchall()
    except sqlite3.Error:
        return dict(empty)
    finally:
        conn.close()

    return {
        "retrievals": int(totals[0]),
        "results": int(totals[1]),
        "tokens": int(totals[2]),
        "duration_ms": int(totals[3]),
        "scopes": {str(row[0]): int(row[1]) for row in scopes},
        "agent_roles": {str(row[0]): int(row[1]) for row in roles},
        "first": totals[4],
        "last": totals[5],
    }


# ── pruning the log ──────────────────────────────────────────────────────

#: The service's own audit trail for prunings, sibling of the learning
#: ledger's ``LEDGER.md`` under the configured learning directory.
RETRIEVAL_LEDGER_NAME = "RETRIEVAL-LEDGER.md"


def retrieval_ledger_path() -> Path:
    """Return the path of the retrieval ledger under the learning dir."""
    return Path(config.get_learning_dir()) / RETRIEVAL_LEDGER_NAME


def _normalise_stamp(value) -> str:
    """``YYYY-MM-DD HH:MM:SS`` and ``YYYY-MM-DDTHH:MM:SS`` compare alike."""
    return str(value).replace(" ", "T")[:19]


def _older_than_threshold(value) -> str:
    """Resolve ``older_than`` to an ISO stamp.

    A plain day count (``"30"``) means that many days before now, UTC;
    anything else must parse as ISO-8601. A value that is neither is a
    ``ValueError`` naming it — the same refusal shape the routes use.
    """
    text = str(value).strip()
    if text.isdigit():
        cutoff = datetime.now(timezone.utc) - timedelta(days=int(text))
        return cutoff.isoformat(timespec="seconds")[:19]
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(
            f"older_than must be ISO-8601 or a plain day count, got {value!r}"
        ) from None
    return parsed.isoformat()[:19]


def _prune_empty(dry_run: bool) -> dict:
    """The empty prune state: nothing selected, nothing deleted."""
    return {
        "selected": 0,
        "deleted": 0,
        "dry_run": bool(dry_run),
        "oldest": None,
        "newest": None,
        "by_scope": {},
        "by_agent_role": {},
        "remaining": 0,
    }


def prune_retrievals(
    *,
    older_than: str | None = None,
    keep_last: int | str | None = None,
    run_id: str | None = None,
    flow_key: str | None = None,
    agent_role: str | None = None,
    scope: str | None = None,
    dry_run: bool = True,
) -> dict:
    """Select and (only when asked) delete old retrieval-log rows.

    Two selection rules combine by intersection: ``older_than`` is an ISO-8601
    timestamp or a plain day count (days before now, UTC) and selects rows
    with ``created_at`` strictly older; ``keep_last`` is a row count and
    selects every candidate except the newest N. A row must satisfy **both**
    to be selected, so ``older_than="30", keep_last=1000`` keeps anything
    younger than 30 days *and* the newest thousand. ``run_id``, ``flow_key``,
    ``agent_role`` and ``scope`` narrow the candidate set exactly as
    ``query_retrievals`` narrows its rows. At least one selection rule is
    required: without one the call raises ``ValueError``, never a full wipe.

    ``dry_run`` defaults to true: the caller gets the honest report and an
    untouched table. With ``dry_run=False`` the selection is deleted in one
    transaction and one ledger line is appended to
    ``<learning_dir>/RETRIEVAL-LEDGER.md`` recording what went. Parameterized
    SQL only; a missing database or table is the empty state, never an
    exception, and reading never creates the file.
    """
    if older_than is None and keep_last is None:
        raise ValueError(
            "prune needs a selection rule: pass older_than and/or keep_last"
        )
    keep: int | None = None
    if keep_last is not None:
        try:
            keep = max(int(keep_last), 0)
        except (TypeError, ValueError):
            raise ValueError(
                f"keep_last must be an integer row count, got {keep_last!r}"
            ) from None
    older = None if older_than is None else _older_than_threshold(older_than)

    filters = " ".join(
        f"{name}={value}"
        for name, value in (
            ("older_than", older_than),
            ("keep_last", keep_last),
            ("run_id", run_id),
            ("flow_key", flow_key),
            ("agent_role", agent_role),
            ("scope", scope),
        )
        if value is not None
    )

    where, params = _conditions(
        run_id=run_id, handoff_id=None, flow_key=flow_key,
        agent_role=agent_role, scope=scope, since=None, until=None,
    )
    conn = _open_log_database()
    if conn is None:
        return _prune_empty(dry_run)
    try:
        candidates = conn.execute(
            f"SELECT id, created_at, scope, agent_role"
            f" FROM knowledge_retrieval_log{where}"
            " ORDER BY replace(created_at, ' ', 'T') DESC, id DESC",
            params,
        ).fetchall()
        total_all = conn.execute(
            "SELECT COUNT(*) FROM knowledge_retrieval_log"
        ).fetchone()[0]
    except sqlite3.Error:
        return _prune_empty(dry_run)
    finally:
        conn.close()

    # ``keep_last`` ranks within the filtered candidates; a row is selected
    # only when it also fails the age rule — the two rules intersect.
    ranked_ids = [row["id"] for row in candidates]
    beyond_keep = set(
        ranked_ids[keep:] if keep is not None else ranked_ids
    )
    selected = [
        row
        for row in candidates
        if row["id"] in beyond_keep
        and (older is None or _normalise_stamp(row["created_at"]) < older)
    ]

    by_scope: dict[str, int] = {}
    by_role: dict[str, int] = {}
    for row in selected:
        scope_key = str(row["scope"] or "")
        role_key = str(row["agent_role"] or "")
        by_scope[scope_key] = by_scope.get(scope_key, 0) + 1
        by_role[role_key] = by_role.get(role_key, 0) + 1

    oldest = selected[-1]["created_at"] if selected else None
    newest = selected[0]["created_at"] if selected else None
    deleted = len(selected)
    remaining = int(total_all) - deleted

    if dry_run:
        return {
            "selected": deleted,
            "deleted": 0,
            "dry_run": True,
            "oldest": oldest,
            "newest": newest,
            "by_scope": by_scope,
            "by_agent_role": by_role,
            "remaining": remaining,
        }

    ids = [row["id"] for row in selected]
    try:
        conn = db.connect()
        try:
            if ids:
                placeholders = ", ".join("?" for _ in ids)
                conn.execute(
                    f"DELETE FROM knowledge_retrieval_log"
                    f" WHERE id IN ({placeholders})",
                    tuple(ids),
                )
            conn.commit()
            remaining = int(conn.execute(
                "SELECT COUNT(*) FROM knowledge_retrieval_log"
            ).fetchone()[0])
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.error("knowledge retrieval log prune failed: %s", exc)
        return _prune_empty(dry_run)

    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _append_retrieval_ledger_line(
        f"- {stamp} | pruned | {deleted} rows | "
        f"{oldest or ''}..{newest or ''} | {filters}"
    )
    return {
        "selected": deleted,
        "deleted": deleted,
        "dry_run": False,
        "oldest": oldest,
        "newest": newest,
        "by_scope": by_scope,
        "by_agent_role": by_role,
        "remaining": remaining,
    }


def _append_retrieval_ledger_line(line: str) -> None:
    """Append one prune line to ``RETRIEVAL-LEDGER.md``, created on first use."""
    ledger = retrieval_ledger_path()
    ledger.parent.mkdir(parents=True, exist_ok=True)
    header_needed = not ledger.is_file()
    with ledger.open("a", encoding="utf-8") as handle:
        if header_needed:
            handle.write("# Retrieval log ledger\n\n")
        handle.write(line + "\n")
    return None
