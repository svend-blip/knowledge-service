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
``detail`` on a denied scope, 404 with ``detail`` on a scope the registry
does not know, 503 with ``detail`` when a store or the provider is missing
or not ready, and 400 on bad input. When ``[service] token`` is set, every
route except ``/v1/health`` requires the ``X-Knowledge-Token`` header. Every
search that reaches a provider writes exactly one retrieval-log row.

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
from knowledge_service import learning
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


def _learning_scope_is_empty(scope: str) -> bool:
    """Whether a learning scope has nothing to retrieve.

    Empty is: the registry row exists and counts zero documents, or the
    index dir holds no store file for the scope. A row with documents and a
    present store is not empty, so ordinary searches keep their path.
    """
    try:
        conn = db.connect()
        try:
            row = conn.execute(
                "SELECT document_count FROM knowledge_indexes WHERE scope = ?",
                (scope,),
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        row = None
    if row is not None and int(row["document_count"]) == 0:
        return True
    return not knowledge_maintenance.learning_store_present(scope)


def _registry_has_scope(scope: str) -> bool:
    """Whether the registry knows this scope at all.

    A database the service cannot open at all is no evidence against the
    scope, so the normal search path continues; only a database that answers
    "no row" produces the 404 below.
    """
    try:
        conn = db.connect()
        try:
            return conn.execute(
                "SELECT 1 FROM knowledge_indexes WHERE scope = ?", (scope,)
            ).fetchone() is not None
        finally:
            conn.close()
    except Exception:
        return True


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
    evidence_level: str | None = None,
    include_history: bool = False,
) -> dict:
    """Search the configured knowledge provider and record the retrieval.

    Disabled mode (enabled false, or configured provider ``none``) returns
    the stable disabled envelope and writes no log row. Enabled mode resolves
    the configured provider, searches it, and appends one retrieval-log row.
    ``flow_key`` is both matched against the grants by the scope guard and
    stored on the logged row, so retrievals are auditable per flow.
    ``top_k`` and ``token_budget`` fall back
    to the configured values and are then clamped to the configured ceilings,
    so a caller may lower them but never raise them; the returned list is
    still defensively truncated to ``top_k``.

    For the learning scopes (``experience``, and ``experience-history`` when
    ``include_history`` is true) ``evidence_level`` is the minimum strength —
    defaulting to ``approved_architecture``, i.e. the three strongest levels
    pass — applied through the provider's metadata filters as an equality set
    over the admitted levels. Other scopes ignore both parameters. A
    learning scope whose registry row counts zero documents, or whose store
    files are absent, answers with an empty result list and no provider
    call. Every other scope follows the error contract: no registry row is a
    404, a registered scope whose store files are gone under the index dir
    is a 503, and an ``OSError`` out of the provider's search is a 503 with
    the exception text as ``detail`` — none of these write a retrieval-log
    row, and no learning scope ever takes the 404 path.

    Every result item carries ``metadata``: the passage's stored extras with
    the identity keys removed (``{}`` for repository passages), passed
    through from the provider unchanged.
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

    # History is only searched when it is asked for explicitly; the level
    # filter exists for the learning scopes alone and reaches the provider
    # as LEANN-style operator syntax (a set-membership equality over the
    # admitted levels), which the portable provider understands as well.
    effective_scope = scope
    if include_history and scope == "experience":
        effective_scope = "experience-history"

    # Checks below work on the concrete scope name: an omitted scope means
    # the configured one, exactly like the provider loaders bind their store.
    lookup_scope = effective_scope if effective_scope is not None else config.get_scope()

    filters: dict | None = None
    if effective_scope in learning.LEARNING_SCOPES:
        filters = {"evidence_level": {"in": learning.levels_at_least(evidence_level)}}

        if _learning_scope_is_empty(effective_scope):
            # An empty or missing learning store has nothing to retrieve.
            # Answer with the empty envelope — logged like any other
            # retrieval — instead of letting a missing LEANN meta file
            # surface out of the route as an error.
            retrieval_log.record_retrieval(
                provider=provider_key,
                scope=effective_scope,
                query=q,
                results=[],
                duration_ms=0,
                agent_role=agent_role,
                run_id=run_id,
                handoff_id=handoff_id,
                flow_key=flow_key,
            )
            return {
                "enabled": True,
                "provider": provider_key,
                "results": [],
                "bounded": True,
            }

    elif not _registry_has_scope(lookup_scope):
        # A scope the registry does not know has nothing to retrieve and no
        # store to rebuild: answer the contract directly, before touching a
        # provider, and write no retrieval-log row.
        raise HTTPException(
            status_code=404, detail=f"unknown scope: {lookup_scope}"
        )

    provider_cls = search.resolve_provider(provider_key, scope=effective_scope)
    provider = provider_cls()

    if (
        effective_scope not in learning.LEARNING_SCOPES
        and not provider.store_exists(lookup_scope)
    ):
        # The row exists but its store files are gone (deleted index dir on
        # a rebuilt machine): a refresh fixes it, and the caller should hear
        # that instead of a provider-level traceback.
        raise HTTPException(
            status_code=503,
            detail=f"store missing for scope {lookup_scope}; refresh it",
        )

    try:
        provider.preflight()
    except ProviderNotReady as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    start = time.perf_counter()
    try:
        results = provider.search(
            q,
            scope=effective_scope,
            filters=filters,
            top_k=top_k,
            token_budget=token_budget,
        )
    except OSError as exc:
        # Missing store files surface as FileNotFoundError out of the
        # provider; that is a refresh-recoverable 503, never a 500.
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    duration_ms = int((time.perf_counter() - start) * 1000)

    # Defensive bound: never return more than the configured ``top_k``,
    # even if the provider ignores its own contract. Result items pass
    # through unchanged (no fields stripped, no scores invented).
    results = results[:top_k]

    retrieval_log.record_retrieval(
        provider=provider_key,
        scope=effective_scope,
        query=q,
        results=results,
        duration_ms=duration_ms,
        agent_role=agent_role,
        run_id=run_id,
        handoff_id=handoff_id,
        flow_key=flow_key,
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
            "SELECT scope, provider, status, document_count, updated_at,"
            " repository_path"
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
            "repository_path": row["repository_path"],
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
        provider = provider_cls()
        readiness = getattr(provider, "readiness_detail", None)
        provider.preflight()
        # A provider that describes its own readiness (the portable one reports
        # whether its model files are present) fills the detail on success;
        # others keep the empty detail.
        if callable(readiness):
            detail = str(readiness())
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
    """Build the FastAPI application exposing the six ``/v1`` routes."""
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
        evidence_level: str | None = None,
        include_history: bool = False,
        _token: None = Depends(require_token),
    ) -> dict:
        """Search the configured knowledge provider and record the retrieval."""
        return search_knowledge(
            q,
            scope,
            top_k,
            token_budget,
            agent_role,
            run_id,
            handoff_id,
            flow_key,
            evidence_level,
            include_history,
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
        """List the registered scopes with document counts and last refresh."""
        return list_scopes()

    @application.get("/v1/learning")
    async def learning_route(
        history: bool = False,
        repository: str | None = None,
        _token: None = Depends(require_token),
    ) -> dict:
        """List admitted learning artifacts; ``history=true`` lists old ones.

        Read-only, no scope guard: the learning scopes are public, like the
        CLI listing. Rows carry ``repository``; ``repository=<slug>`` filters
        them to that repository alone.
        """
        records = learning.list_artifact_records(history)
        if repository:
            slug = scope_for_path(repository)
            records = [item for item in records if item.get("repository") == slug]
        return {"artifacts": records}

    @application.get("/v1/learning/drafts")
    async def learning_drafts_route(
        pending: bool = False,
        _token: None = Depends(require_token),
    ) -> dict:
        """List the run-directory drafts; ``pending=true`` keeps unadmitted ones.

        Read-only, no scope guard: the listing mirrors ``learning drafts``, so
        a supervisor can see what waits for admission before calling
        ``learning admit-run``.
        """
        drafts = learning.list_pending_drafts()
        if pending:
            drafts = [item for item in drafts if not item["admitted"]]
        return {"drafts": drafts}

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
