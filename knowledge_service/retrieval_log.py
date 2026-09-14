"""Single writer for the ``knowledge_retrieval_log`` append-only table.

This module is the one place that writes rows to ``knowledge_retrieval_log``.
Every search that reaches a provider records its retrieval through
``record_retrieval`` so every real provider call leaves exactly one auditable
row behind.

Provider-neutral by construction: the module imports only this package's
config and database helpers, the standard library, and FastAPI's
``HTTPException``. It never names or imports a concrete knowledge provider.
"""

from __future__ import annotations

import json
import logging

from knowledge_service import config, db

__all__ = ["record_retrieval"]

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
) -> None:
    """Append one ``knowledge_retrieval_log`` row for a real retrieval.

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
            "agent_role, run_id, handoff_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
