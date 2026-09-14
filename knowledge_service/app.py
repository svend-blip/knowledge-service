"""The service's HTTP contract (``/v1``).

Provider-neutral by construction: this module imports only
``knowledge_service.search`` plus the plain helpers, and never names or
imports a concrete provider. ``top_k`` and ``token_budget`` fall back to the
configured values and are clamped to the configured ceilings (a caller may
lower them, never raise them); the endpoint still keeps the defensive
post-truncation of the returned list to ``top_k`` so a misbehaving provider
can never make the endpoint exceed the configured result bound.

Response shapes and status codes are the ones DPMtF's
``/api/knowledge/search`` carries today: the disabled envelope, 403 with
``detail`` on a denied scope, 503 with ``detail`` on ``ProviderNotReady``,
and 400 on bad input. When ``[service] token`` is set, every route except
``/v1/health`` requires the ``X-Knowledge-Token`` header. Every search that
reaches a provider writes exactly one retrieval-log row.

The HTTP framework is imported inside :func:`create_app`, not at module
scope, so importing this module on a machine without the optional server
dependency still works: the route logic is plain Python and only the
framework adapter needs the dependency.
"""

import contextlib
import io
import logging
import time
from pathlib import Path

from knowledge_service import config
from knowledge_service import db
from knowledge_service import maintenance as knowledge_maintenance
from knowledge_service import indexer as knowledge_indexer
from knowledge_service import retrieval_log, scope_guard, search
from knowledge_service.provider import ProviderNotReady
from knowledge_service.scopes import scope_for_path

logger = logging.getLogger(__name__)

__all__ = ["app", "create_app"]


def _disabled_envelope(provider_key: str) -> dict:
    """Return the stable envelope of a disabled installation."""
    return {
        "enabled": False,
        "provider": provider_key,
        "results": [],
        "bounded": True,
    }


def _stderr_from_system_exit(exc: SystemExit) -> str:
    """Return the error text a ``_fail`` call printed before ``SystemExit``.

    ``maintenance`` and ``indexer`` report fatal errors by printing to stderr
    and then raising ``SystemExit(1)``. The refresh endpoint turns that into
    an HTTP 400 with the captured message instead of letting the traceback
    (or an empty detail) reach the client.
    """
    try:
        message = str(exc)
    except Exception:
        message = ""
    if message:
        return message
    return "operation failed"


def _run_capturing_stderr(func, *args):
    """Run ``func`` with stderr captured and return ``(ok, message)``.

    ``message`` is the captured stderr (stripped) on failure and the tuple
    ``(result, captured_stderr)`` on success.
    """
    stream = io.StringIO()
    with contextlib.redirect_stderr(stream):
        try:
            result = func(*args)
        except SystemExit as exc:
            captured = stream.getvalue().strip()
            return False, captured or _stderr_from_system_exit(exc)
    captured = stream.getvalue().strip()
    return True, (result, captured)


def search_knowledge(
    q: str,
    scope: str | None = None,
    top_k: int | None = None,
    token_budget: int | None = None,
    agent_role: str | None = None,
    run_id: str | None = None,
    handoff_id: str | None = None,
    flow_key: str | None = None,
) -> dict:
    """Search the configured knowledge provider and record the retrieval.

    Disabled mode (enabled false, or configured provider ``none``) returns
    the stable disabled envelope and writes no log row. Enabled mode resolves
    the configured provider, searches it, and appends one retrieval-log row.
    ``flow_key`` is passed to the scope guard for grant matching and is not
    recorded in the retrieval log. ``top_k`` and ``token_budget`` fall back
    to the configured values and are then clamped to the configured ceilings,
    so a caller may lower them but never raise them; the returned list is
    still defensively truncated to ``top_k``.
    """
    from fastapi import HTTPException

    provider_key = config.get_provider()

    if not config.get_enabled() or provider_key == "none":
        return _disabled_envelope(provider_key)

    if top_k is None:
        top_k = config.get_top_k()
    if token_budget is None:
        token_budget = config.get_max_context_tokens()

    # Configured values are both fallback and ceiling: a caller may only
    # lower them, never raise them. A non-positive bound is clamped to the
    # minimum sensible value (1) so the defensive slice below can never
    # mis-bound (e.g. ``results[:-5]`` dropping the tail instead of the head).
    top_k = max(1, min(top_k, config.get_top_k()))
    token_budget = max(1, min(token_budget, config.get_max_context_tokens()))

    try:
        scope_guard.require_scope_access(
            scope,
            agent_role=agent_role,
            flow_key=flow_key,
        )
    except scope_guard.ScopeAccessDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    provider_cls = search.resolve_provider(provider_key, scope=scope)
    provider = provider_cls()

    try:
        provider.preflight()
    except ProviderNotReady as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    start = time.perf_counter()
    results = provider.search(
        q,
        scope=scope,
        top_k=top_k,
        token_budget=token_budget,
    )
    duration_ms = int((time.perf_counter() - start) * 1000)

    # Defensive bound: never return more than the configured ``top_k``,
    # even if the provider ignores its own contract. Result items pass
    # through unchanged (no fields stripped, no scores invented).
    results = results[:top_k]

    retrieval_log.record_retrieval(
        provider=provider_key,
        scope=scope,
        query=q,
        results=results,
        duration_ms=duration_ms,
        agent_role=agent_role,
        run_id=run_id,
        handoff_id=handoff_id,
    )

    return {
        "enabled": True,
        "provider": provider_key,
        "results": results,
        "bounded": True,
    }


def refresh_knowledge(scope: str, repo_path: str) -> dict:
    """Re-index a scope when its repository changed since the last index.

    Maintenance-only: it performs no scope-guard check and writes no retrieval-log
    row. Validation and HTTP mapping stay here; the detect/index/provider/record
    work is delegated to ``maintenance.refresh_scope``.
    """
    from fastapi import HTTPException

    provider_key = config.get_provider()

    # Disabled short-circuit first: mirror the search endpoint's envelope and
    # do no validation, no indexer call, no provider call, no DB write.
    if not config.get_enabled() or provider_key == "none":
        return _disabled_envelope(provider_key)

    resolved_repo = Path(repo_path).expanduser().resolve()
    if not resolved_repo.exists() or not resolved_repo.is_dir():
        raise HTTPException(
            status_code=400,
            detail="repo_path must be an existing directory",
        )

    clean_scope = scope.strip()
    if not clean_scope:
        raise HTTPException(status_code=400, detail="scope must not be empty")

    index_dir = Path(config.get_index_dir())
    try:
        index_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(
            status_code=400, detail=f"cannot create index dir: {exc}"
        ) from exc

    manifest_path = (index_dir / f"{clean_scope}.jsonl").resolve()
    if manifest_path.is_relative_to(resolved_repo):
        raise HTTPException(
            status_code=400,
            detail="manifest must be outside repo_path",
        )

    try:
        ok, message = _run_capturing_stderr(
            lambda: knowledge_maintenance.refresh_scope(clean_scope, str(resolved_repo))
        )
    except ProviderNotReady as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except knowledge_indexer.RepoExclusionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=400, detail=f"refresh failed: {exc}"
        ) from exc
    if not ok:
        raise HTTPException(status_code=400, detail=message)

    result, _captured_stderr = message
    return result


def list_scopes() -> list[dict]:
    """Return one entry per registry row, scope name first."""
    conn = None
    try:
        conn = db.connect()
        rows = conn.execute(
            "SELECT scope, provider, status, document_count, updated_at"
            " FROM knowledge_indexes ORDER BY scope"
        ).fetchall()
    finally:
        if conn is not None:
            conn.close()
    return [
        {
            "scope": row["scope"],
            "provider": row["provider"],
            "status": row["status"],
            "document_count": row["document_count"],
            "indexed_at": row["updated_at"],
        }
        for row in rows
    ]


def health() -> dict:
    """Report the configured provider, enabled state, and preflight result."""
    provider_key = config.get_provider()
    enabled = bool(config.get_enabled()) and provider_key != "none"

    provider_ok = True
    detail = ""
    try:
        provider_cls = search.resolve_provider(provider_key, scope=config.get_scope())
        provider_cls().preflight()
    except ProviderNotReady as exc:
        provider_ok = False
        detail = str(exc)
    except Exception as exc:
        provider_ok = False
        detail = str(exc) or exc.__class__.__name__

    return {
        "status": "ok",
        "provider": provider_key,
        "enabled": enabled,
        "preflight": {"ok": provider_ok, "detail": detail},
    }


def create_app():
    """Build the FastAPI application exposing the five ``/v1`` routes."""
    from fastapi import Depends, FastAPI, Header
    from pydantic import BaseModel

    class RefreshRequest(BaseModel):
        """Body for POST /v1/refresh."""

        scope: str
        repo_path: str

    def require_token(x_knowledge_token: str | None = Header(default=None)) -> None:
        """Require the shared token header when the installation configures one."""
        from fastapi import HTTPException

        expected = config.get_token()
        if not expected:
            return None
        if x_knowledge_token != expected:
            raise HTTPException(
                status_code=403,
                detail="missing or invalid X-Knowledge-Token header",
            )
        return None

    application = FastAPI(title="Knowledge Service", version="1")

    @application.get("/v1/search")
    async def search_route(
        q: str,
        scope: str | None = None,
        top_k: int | None = None,
        token_budget: int | None = None,
        agent_role: str | None = None,
        run_id: str | None = None,
        handoff_id: str | None = None,
        flow_key: str | None = None,
        _token: None = Depends(require_token),
    ) -> dict:
        """Search the configured knowledge provider and record the retrieval."""
        return search_knowledge(
            q, scope, top_k, token_budget, agent_role, run_id, handoff_id, flow_key
        )

    @application.post("/v1/refresh")
    async def refresh_route(
        body: RefreshRequest,
        _token: None = Depends(require_token),
    ) -> dict:
        """Re-index one scope when its repository changed since the last index."""
        return refresh_knowledge(body.scope, body.repo_path)

    @application.get("/v1/scopes")
    async def scopes_route(_token: None = Depends(require_token)) -> list:
        """List the known scopes with document counts and last refresh."""
        return list_scopes()

    @application.get("/v1/scope-for-path")
    async def scope_for_path_route(
        path: str = "",
        _token: None = Depends(require_token),
    ) -> dict:
        """Resolve a repository path to its scope slug."""
        return {"scope": scope_for_path(path)}

    @application.get("/v1/health")
    async def health_route() -> dict:
        """Report provider, enabled state, and preflight result."""
        return health()

    return application


def __getattr__(name: str):
    """Expose the built application lazily so imports never need FastAPI."""
    if name == "app":
        application = create_app()
        globals()["app"] = application
        return application
    raise AttributeError(name)
