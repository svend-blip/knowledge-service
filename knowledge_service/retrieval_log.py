"""Single writer for the ``knowledge_retrieval_log`` append-only table.

This module is the one place that writes rows to ``knowledge_retrieval_log``.
Every search that reaches a provider records its retrieval through
``record_retrieval`` so every real provider call leaves exactly one auditable
row behind. The read side lives here too: ``query_retrievals`` pages the log
newest-first and ``summarise_retrievals`` totals it, both against the
configured database opened read-only and both answering with the empty state
instead of raising when the database or table is missing.

Provider-neutral by construction: the module imports only this package's
config and database helpers, the standard library, and FastAPI's
``HTTPException``. It never names or imports a concrete knowledge provider.
"""

from __future__ import annotations

import json
import logging
import sqlite3

from knowledge_service import config, db

__all__ = ["record_retrieval", "query_retrievals", "summarise_retrievals"]

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
