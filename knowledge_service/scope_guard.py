"""Provider-neutral scope permissions guard for the knowledge layer.

Repository boundaries are enforced at the knowledge layer instead of merely
assumed by the provider. This module is that guard: it decides, for one
(scope, agent_role, flow_key) triple, whether knowledge access is allowed,
and it does so without importing or naming any concrete provider.

The rule is small and deliberately conservative:

* Non-internal scopes are always allowed. They are public repository
  knowledge and need no grant.
* Internal scopes (``"dpmtf"`` and anything beginning ``"dpmtf-"``) default
  to denied. They are DPMtF-internal development memory, and a caller may
  read them only when the Human has recorded an explicit grant row in the
  ``knowledge_scope_grants`` table.
* A grant row matches ``scope`` and ``agent_role`` exactly. Its ``flow_key``
  is a wildcard when NULL (it matches any caller flow) and an exact filter
  when not NULL (it matches only that flow). Absence of any matching grant
  still denies.
* A missing or unreadable database is treated as "no grants": internal
  scopes are denied and the guard never creates or writes a database file.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from knowledge_service import config

__all__ = [
    "ScopeAccessDenied",
    "is_internal_scope",
    "can_access_scope",
    "require_scope_access",
]


class ScopeAccessDenied(Exception):
    """Raised when access to an internal knowledge scope is denied.

    The message carries the exact triple that was denied so the caller and
    the operator can see which grant is missing.
    """

    def __init__(self, scope: str, agent_role: str | None, flow_key: str | None) -> None:
        self.scope = scope
        self.agent_role = agent_role
        self.flow_key = flow_key
        message = (
            "Scope access denied for scope=%r agent_role=%r flow_key=%r: "
            "no explicit grant matches this internal knowledge scope."
        ) % (scope, agent_role, flow_key)
        super().__init__(message)


def is_internal_scope(scope) -> bool:
    """Return True only for DPMtF-internal development scopes.

    Internal means exactly ``"dpmtf"`` or a string beginning ``"dpmtf-"``.
    Anything else — including ``None`` and non-strings — is not internal.
    There is no other prefix or substring rule.
    """
    if not isinstance(scope, str):
        return False
    return scope == "dpmtf" or scope.startswith("dpmtf-")


def can_access_scope(scope, *, agent_role=None, flow_key=None, db_path=None) -> bool:
    """Return True when the (scope, agent_role, flow_key) triple may be read.

    Non-internal scopes are always allowed, without touching the database.
    Internal scopes require an explicit grant row in ``knowledge_scope_grants``
    matching ``scope`` and ``agent_role`` exactly; a grant row whose
    ``flow_key`` is NULL is a wildcard that matches any caller flow, while a
    non-NULL grant ``flow_key`` matches only that flow. A missing or
    unreadable database means no grants exist, so access is denied and no
    database file is ever created.
    """
    if not is_internal_scope(scope):
        return True

    if not db_path:
        db_path = config.get_db_path()

    if not os.path.isfile(db_path):
        return False

    conn = None
    try:
        db_uri = Path(db_path).resolve().as_uri()
        conn = sqlite3.connect(db_uri + "?mode=ro", uri=True)
        row = conn.execute(
            "SELECT 1 FROM knowledge_scope_grants"
            " WHERE scope = ? AND agent_role = ?"
            " AND (flow_key = ? OR flow_key IS NULL)"
            " LIMIT 1",
            (scope, agent_role, flow_key),
        ).fetchone()
        return row is not None
    except (OSError, sqlite3.Error):
        return False
    finally:
        if conn is not None:
            conn.close()


def require_scope_access(scope, *, agent_role=None, flow_key=None, db_path=None) -> None:
    """Raise ScopeAccessDenied when the triple may not be read, else do nothing."""
    if not can_access_scope(
        scope,
        agent_role=agent_role,
        flow_key=flow_key,
        db_path=db_path,
    ):
        raise ScopeAccessDenied(scope, agent_role, flow_key)
    return None
