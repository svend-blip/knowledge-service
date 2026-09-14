"""Hermetic tests for the standalone knowledge service.

Every test uses a temp database and temp index directory plus a stub
provider; ``torch`` is stubbed where preflight is touched. Nothing here
requires a running service or a GPU.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import types
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from knowledge_service import cli, config, db, maintenance
from knowledge_service.app import create_app
from knowledge_service.portable_provider import OnnxEmbedder, PortableProvider
from knowledge_service.provider import ProviderNotReady
from knowledge_service.scopes import scope_for_path
from knowledge_service import search as search_module
from tests.conftest import OPERATOR_DIRS, changed_entries, snapshot_tree

# ── helpers ─────────────────────────────────────────────────────────────


def write_ini(tmp_path, monkeypatch, body: str) -> None:
    """Point the config loader at a per-test INI file."""
    ini = tmp_path / "knowledge.ini"
    ini.write_text(body, encoding="utf-8")
    monkeypatch.setenv("KNOWLEDGE_SERVICE_INI", str(ini))
    config.reload()


def service_ini(tmp_path, monkeypatch, **overrides) -> str:
    """Write a full service INI with temp paths and return its text."""
    knowledge = {
        "enabled": "true",
        "provider": "stub",
        "scope": "dpmtf-webui",
        "top_k": "8",
        "max_context_tokens": "1200",
        "max_document_chars": "20000",
        "index_dir": str(tmp_path / "index_dir"),
        "min_free_vram_mib": "1",
        "leann_use_daemon": "false",
    }
    service = {"db_path": str(tmp_path / "knowledge.db")}
    knowledge.update({k: v for k, v in overrides.items() if k in knowledge})
    service.update({k: v for k, v in overrides.items() if k not in knowledge})
    body = "[knowledge]\n" + "".join(
        f"{key} = {value}\n" for key, value in knowledge.items()
    )
    body += "\n[service]\n" + "".join(
        f"{key} = {value}\n" for key, value in service.items()
    )
    return body


def make_client() -> TestClient:
    return TestClient(create_app())


class StubProvider:
    """Minimal provider for route tests; records how it was used."""

    calls: list[str] = []
    results: list[dict] = [{"path": "README.md", "content": "alpha beta", "score": 1.0}]
    preflight_error: Exception | None = None
    search_error: Exception | None = None

    def preflight(self) -> None:
        import torch  # noqa: F401 - proves the preflight path touches the stub

        StubProvider.calls.append("preflight")
        if StubProvider.preflight_error is not None:
            raise StubProvider.preflight_error

    def index(self, source: str) -> None:
        StubProvider.calls.append(f"index:{source}")

    def update(self, source: str) -> None:
        StubProvider.calls.append(f"update:{source}")

    def remove(self, source: str) -> None:
        StubProvider.calls.append(f"remove:{source}")

    def search(self, query, *, scope=None, filters=None, top_k=None, token_budget=None):
        StubProvider.calls.append(f"search:{query}")
        if StubProvider.search_error is not None:
            raise StubProvider.search_error
        return [dict(item) for item in StubProvider.results]


def install_stub(monkeypatch) -> None:
    StubProvider.calls = []
    StubProvider.results = [
        {"path": "README.md", "content": "alpha beta", "score": 1.0}
    ]
    StubProvider.preflight_error = None
    StubProvider.search_error = None
    monkeypatch.setitem(search_module.PROVIDER_LOADERS, "stub", lambda: StubProvider)


def stub_torch(monkeypatch, free_mib: int = 8192) -> None:
    cuda = types.SimpleNamespace(
        is_available=lambda: True,
        mem_get_info=lambda: (free_mib * 1024 * 1024, 32 * 1024 * 1024 * 1024),
    )
    monkeypatch.setitem(sys.modules, "torch", types.SimpleNamespace(cuda=cuda))


def log_rows() -> list[sqlite3.Row]:
    conn = db.connect()
    try:
        return conn.execute(
            "SELECT * FROM knowledge_retrieval_log ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


# ── scope rule ──────────────────────────────────────────────────────────


def test_scope_for_path_normalises_windows_and_posix(tmp_path, monkeypatch):
    write_ini(tmp_path, monkeypatch, "[knowledge]\nscope = custom-default\n")

    assert scope_for_path("C:\\Projects\\FlowRunner\\") == "flowrunner"
    assert scope_for_path("C:/Projects/FlowRunner/") == "flowrunner"
    assert scope_for_path("c:\\projects\\FlowRunner") == "flowrunner"
    assert scope_for_path("/home/x/FlowRunner/") == "flowrunner"
    assert scope_for_path("/home/x/AI_AdvisoryBoard") == "ai_advisoryboard"
    # Both styles must agree on one repository.
    assert scope_for_path("C:\\Projects\\FlowRunner\\") == scope_for_path(
        "/srv/projects/FlowRunner/"
    )

    # An empty path, a bare root, and None all keep the configured default.
    assert scope_for_path("") == "custom-default"
    assert scope_for_path(None) == "custom-default"
    assert scope_for_path("/") == "custom-default"


def test_scope_for_path_maps_father_root_to_default_scope(tmp_path, monkeypatch):
    father = tmp_path / "father"
    father.mkdir()
    ini = (
        "[knowledge]\nscope = custom-default\n\n"
        "[service]\nfather_root = " + str(father) + "\n"
    )
    write_ini(tmp_path, monkeypatch, ini)

    assert scope_for_path(str(father)) == "custom-default"
    assert scope_for_path(str(father) + "/") == "custom-default"
    # A child of father keeps its own slug.
    assert scope_for_path(str(father / "subproject")) == "subproject"


# ── routes ──────────────────────────────────────────────────────────────


def test_search_route_disabled_envelope(tmp_path, monkeypatch):
    write_ini(tmp_path, monkeypatch, service_ini(tmp_path, monkeypatch, enabled="false"))
    client = make_client()

    response = client.get("/v1/search", params={"q": "anything"})
    assert response.status_code == 200
    assert response.json() == {
        "enabled": False,
        "provider": "stub",
        "results": [],
        "bounded": True,
    }
    # A disabled installation writes no retrieval-log row.
    assert log_rows() == []

    # An enabled section with the no-op provider still reports disabled shape.
    write_ini(tmp_path, monkeypatch, service_ini(tmp_path, monkeypatch, provider="none"))
    response = client.get("/v1/search", params={"q": "anything"})
    assert response.json()["enabled"] is False
    assert response.json()["results"] == []
    assert log_rows() == []


def test_search_route_denied_is_403_with_detail(tmp_path, monkeypatch):
    install_stub(monkeypatch)
    stub_torch(monkeypatch)
    write_ini(tmp_path, monkeypatch, service_ini(tmp_path, monkeypatch))
    client = make_client()

    response = client.get(
        "/v1/search", params={"q": "x", "scope": "dpmtf-webui", "agent_role": "nobody"}
    )
    assert response.status_code == 403
    detail = response.json()["detail"]
    assert detail
    assert "dpmtf-webui" in detail

    # A public scope needs no grant at all.
    public = client.get("/v1/search", params={"q": "x", "scope": "flowrunner"})
    assert public.status_code == 200
    assert public.json()["enabled"] is True

    # The CLI grant opens exactly the denied triple.
    assert cli.main(["grant", "dpmtf-webui", "nobody"]) == 0
    granted = client.get(
        "/v1/search", params={"q": "x", "scope": "dpmtf-webui", "agent_role": "nobody"}
    )
    assert granted.status_code == 200
    assert len(granted.json()["results"]) == 1


def test_search_route_not_ready_is_503(tmp_path, monkeypatch):
    install_stub(monkeypatch)
    stub_torch(monkeypatch)
    StubProvider.preflight_error = ProviderNotReady("no CUDA device is available")
    write_ini(tmp_path, monkeypatch, service_ini(tmp_path, monkeypatch))
    client = make_client()

    response = client.get("/v1/search", params={"q": "x", "scope": "flowrunner"})
    assert response.status_code == 503
    assert response.json()["detail"]

    # A refused search writes no retrieval-log row.
    assert log_rows() == []


def test_search_route_writes_one_log_row(tmp_path, monkeypatch):
    install_stub(monkeypatch)
    stub_torch(monkeypatch)
    StubProvider.results = [
        {"path": "README.md", "content": "three words here", "score": 0.9},
        {"path": "docs/a.md", "content": "two words", "score": 0.8},
    ]
    write_ini(tmp_path, monkeypatch, service_ini(tmp_path, monkeypatch))
    client = make_client()

    response = client.get(
        "/v1/search",
        params={
            "q": "budget",
            "scope": "flowrunner",
            "top_k": "2",
            "token_budget": "50",
            "agent_role": "dsh",
            "run_id": "012",
            "handoff_id": "H-012-01",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["bounded"] is True
    assert payload["provider"] == "stub"
    assert len(payload["results"]) == 2

    rows = log_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["provider"] == "stub"
    assert row["scope"] == "flowrunner"
    assert row["query"] == "budget"
    assert row["result_count"] == 2
    assert json.loads(row["sources"]) == ["README.md", "docs/a.md"]
    assert row["retrieved_token_count"] == 5
    assert row["agent_role"] == "dsh"
    assert row["run_id"] == "012"
    assert row["handoff_id"] == "H-012-01"


def test_refresh_route_noop_touches_nothing(tmp_path, monkeypatch):
    install_stub(monkeypatch)
    stub_torch(monkeypatch)
    write_ini(tmp_path, monkeypatch, service_ini(tmp_path, monkeypatch))
    client = make_client()

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "note.md").write_text("alpha beta", encoding="utf-8")

    # Manifest that matches the repository exactly, so detection says noop.
    index_dir = tmp_path / "index_dir"
    index_dir.mkdir(parents=True, exist_ok=True)
    manifest = index_dir / "notes.jsonl"
    manifest.write_text(
        json.dumps({"scope": "notes", "path": "note.md", "content": "alpha beta"})
        + "\n",
        encoding="utf-8",
    )

    StubProvider.calls = []
    response = client.post(
        "/v1/refresh", json={"scope": "notes", "repo_path": str(repo)}
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "noop"
    assert payload["manifest"] == str(manifest.resolve())

    assert StubProvider.calls == ["preflight"]
    assert manifest.read_text(encoding="utf-8").startswith('{"scope"')

    conn = db.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM knowledge_indexes").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM knowledge_retrieval_log"
        ).fetchone()[0] == 0
    finally:
        conn.close()

    # A changed file re-indexes through the provider and records one row whose
    # location is the repository path.
    (repo / "extra.md").write_text("gamma", encoding="utf-8")
    changed = client.post(
        "/v1/refresh", json={"scope": "notes", "repo_path": str(repo)}
    )
    assert changed.status_code == 200
    assert changed.json()["status"] == "reindexed"
    assert changed.json()["documents"] == 2
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT scope, location, status, document_count FROM knowledge_indexes"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0]["scope"] == "notes"
    assert rows[0]["location"] == str(repo.resolve())
    assert rows[0]["status"] == "changed"
    assert rows[0]["document_count"] == 2


def test_scopes_route_lists_registry_rows(tmp_path, monkeypatch):
    write_ini(tmp_path, monkeypatch, service_ini(tmp_path, monkeypatch))
    client = make_client()

    maintenance.record_index("alpha", "leann", str(tmp_path / "alpha"), 3, "changed")
    maintenance.record_index("beta", "leann", str(tmp_path / "beta"), 0, "missing")

    response = client.get("/v1/scopes")
    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload, list)
    assert len(payload) == 2
    assert payload[0] == {
        "scope": "alpha",
        "provider": "leann",
        "status": "changed",
        "document_count": 3,
        "indexed_at": payload[0]["indexed_at"],
    }
    assert payload[0]["indexed_at"]
    assert payload[1]["scope"] == "beta"
    assert payload[1]["document_count"] == 0


def test_health_route_reports_provider_and_preflight(tmp_path, monkeypatch):
    install_stub(monkeypatch)
    stub_torch(monkeypatch)
    write_ini(tmp_path, monkeypatch, service_ini(tmp_path, monkeypatch))
    client = make_client()

    response = client.get("/v1/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["provider"] == "stub"
    assert payload["enabled"] is True
    assert payload["preflight"] == {"ok": True, "detail": ""}
    assert StubProvider.calls == ["preflight"]

    # A busy GPU is reported, not swallowed.
    StubProvider.calls = []
    StubProvider.preflight_error = ProviderNotReady(
        "knowledge provider not ready: free GPU memory 12 MiB is below the "
        "configured minimum 4096 MiB"
    )
    busy = client.get("/v1/health")
    assert busy.status_code == 200
    assert busy.json()["preflight"]["ok"] is False
    assert "below the configured minimum" in busy.json()["preflight"]["detail"]

    # The no-op provider is always ready, but reports enabled=false.
    write_ini(tmp_path, monkeypatch, service_ini(tmp_path, monkeypatch, provider="none"))
    none_health = client.get("/v1/health")
    assert none_health.json()["provider"] == "none"
    assert none_health.json()["enabled"] is False
    assert none_health.json()["preflight"] == {"ok": True, "detail": ""}


def test_token_header_guards_every_route_except_health(tmp_path, monkeypatch):
    install_stub(monkeypatch)
    stub_torch(monkeypatch)
    body = service_ini(
        tmp_path, monkeypatch, token="sekret", father_root=str(tmp_path / "father")
    )
    write_ini(tmp_path, monkeypatch, body)
    client = make_client()

    repo = tmp_path / "repo"
    repo.mkdir()

    assert client.get("/v1/search", params={"q": "x"}).status_code == 403
    assert client.get("/v1/scopes").status_code == 403
    assert client.get("/v1/scope-for-path", params={"path": "/x/y"}).status_code == 403
    assert client.post(
        "/v1/refresh", json={"scope": "notes", "repo_path": str(repo)}
    ).status_code == 403

    allowed = {"X-Knowledge-Token": "sekret"}
    search_response = client.get("/v1/search", headers=allowed, params={"q": "x"})
    assert search_response.status_code == 200
    assert search_response.json()["enabled"] is True

    scopes_response = client.get("/v1/scopes", headers=allowed)
    assert scopes_response.status_code == 200
    assert scopes_response.json() == []

    slug = client.get(
        "/v1/scope-for-path", headers=allowed, params={"path": "/srv/FlowRunner/"}
    )
    assert slug.status_code == 200
    assert slug.json() == {"scope": "flowrunner"}

    refresh = client.post(
        "/v1/refresh",
        headers=allowed,
        json={"scope": "notes", "repo_path": str(repo)},
    )
    assert refresh.status_code == 200

    # Health answers without a token so probes keep working.
    health_response = client.get("/v1/health")
    assert health_response.status_code == 200
    assert health_response.json()["status"] == "ok"


# ── CLI ─────────────────────────────────────────────────────────────────


def _source_database(tmp_path) -> str:
    """Build a DPMtF-shaped source database with one row per table."""
    source = tmp_path / "dpmtf.db"
    conn = sqlite3.connect(source)
    conn.executescript(db.SCHEMA_SQL)
    conn.execute(
        "INSERT INTO knowledge_indexes (scope, provider, location, document_count, status)"
        " VALUES ('flowrunner', 'leann', '/srv/FlowRunner', 656, 'changed')"
    )
    conn.execute(
        "INSERT INTO knowledge_indexes (scope, provider, location, document_count, status)"
        " VALUES ('dpmtf-webui', 'leann', '/srv/DPMtF-WebUI', 660, 'changed')"
    )
    conn.execute(
        "INSERT INTO knowledge_scope_grants (scope, agent_role, flow_key)"
        " VALUES ('dpmtf-webui', 'dsh', NULL)"
    )
    conn.execute(
        "INSERT INTO knowledge_scope_grants (scope, agent_role, flow_key)"
        " VALUES ('dpmtf-webui', 'human', '1000-01-PLOOP')"
    )
    conn.execute(
        "INSERT INTO knowledge_exclusions (scope, pattern, kind)"
        " VALUES ('flowrunner', 'exports/', 'path')"
    )
    conn.execute(
        "INSERT INTO knowledge_retrieval_log (provider, scope, query, result_count)"
        " VALUES ('leann', 'flowrunner', 'budget', 3)"
    )
    conn.commit()
    conn.close()
    return str(source)


def test_import_registry_is_idempotent(tmp_path, monkeypatch):
    source = _source_database(tmp_path)
    write_ini(tmp_path, monkeypatch, service_ini(tmp_path, monkeypatch))

    assert cli.main(["import-registry", "--from", source]) == 0
    first = _registry_counts()
    assert first == {
        "knowledge_indexes": 2,
        "knowledge_scope_grants": 2,
        "knowledge_exclusions": 1,
        "knowledge_retrieval_log": 1,
    }

    # The second run inserts nothing and changes nothing.
    assert cli.main(["import-registry", "--from", source]) == 0
    assert _registry_counts() == first

    conn = db.connect()
    try:
        grants = conn.execute(
            "SELECT COUNT(*) FROM knowledge_scope_grants WHERE agent_role = 'dsh'"
        ).fetchone()[0]
        row = conn.execute(
            "SELECT scope FROM knowledge_indexes WHERE scope = 'flowrunner'"
        ).fetchone()
    finally:
        conn.close()
    assert grants == 1
    assert row["scope"] == "flowrunner"


def _registry_counts() -> dict:
    conn = db.connect()
    try:
        return {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in db.REGISTRY_TABLES
        }
    finally:
        conn.close()


def test_cli_grant_and_revoke(tmp_path, monkeypatch):
    write_ini(tmp_path, monkeypatch, service_ini(tmp_path, monkeypatch))

    # Role-only grant, recorded twice, stays one row with NULL flow_key.
    assert cli.main(["grant", "dpmtf-webui", "dsh"]) == 0
    assert cli.main(["grant", "dpmtf-webui", "dsh"]) == 0
    assert _grant_rows("dpmtf-webui", "dsh") == [(None,)]

    # A flow-scoped grant is a separate row and is untouched by the role-only
    # revoke.
    assert cli.main(["grant", "dpmtf-webui", "dsh", "--flow", "9000-01-PLOOP"]) == 0
    rows = _grant_rows("dpmtf-webui", "dsh")
    assert sorted(rows, key=lambda row: str(row[0])) == sorted(
        [(None,), ("9000-01-PLOOP",)], key=lambda row: str(row[0])
    )

    assert cli.main(["revoke", "dpmtf-webui", "dsh"]) == 0
    assert _grant_rows("dpmtf-webui", "dsh") == [("9000-01-PLOOP",)]
    # Revoking again is not an error.
    assert cli.main(["revoke", "dpmtf-webui", "dsh"]) == 0
    assert _grant_rows("dpmtf-webui", "dsh") == [("9000-01-PLOOP",)]

    assert cli.main(["revoke", "dpmtf-webui", "dsh", "--flow", "9000-01-PLOOP"]) == 0
    # Every explicit row is gone, so the next connection re-records only the
    # baseline grant for the configured default scope.
    assert _grant_rows("dpmtf-webui", "dsh") == [(None,)]

    # scopes prints the registry without crashing on an empty table.
    assert cli.main(["scopes"]) == 0


def _grant_rows(scope: str, agent_role: str) -> list:
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT flow_key FROM knowledge_scope_grants"
            " WHERE scope = ? AND agent_role = ?",
            (scope, agent_role),
        ).fetchall()
    finally:
        conn.close()
    return [(row["flow_key"],) for row in rows]


_ALLOWED_ROOTS = frozenset(
    {
        "__future__",
        "abc",
        "argparse",
        "collections",
        "configparser",
        "contextlib",
        "dataclasses",
        "datetime",
        "fastapi",
        "fnmatch",
        "functools",
        "huggingface_hub",
        "io",
        "json",
        "leann",
        "logging",
        "numpy",
        "onnxruntime",
        "os",
        "pathlib",
        "pydantic",
        "re",
        "shutil",
        "sqlite3",
        "struct",
        "sys",
        "tempfile",
        "time",
        "tokenizers",
        "torch",
        "typing",
        "uvicorn",
        "knowledge_service",
    }
)


def test_package_never_imports_dpmtf():
    """Package files import stdlib, declared deps, and knowledge_service only.

    A DPMtF module name (``config``, ``bridge_lib``, ``knowledge``) must never
    appear as an import root: this package reads its own config module and has
    no other checkout on the path.
    """
    import ast
    import importlib
    from pathlib import Path

    source_root = Path(__file__).resolve().parent.parent / "knowledge_service"
    files = sorted(source_root.glob("*.py"))
    assert len(files) >= 11

    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            else:
                continue
            for module in modules:
                assert module.split(".")[0] in _ALLOWED_ROOTS, (
                    path.name,
                    module,
                )
        text = path.read_text(encoding="utf-8")
        assert "bridge_lib" not in text
        assert "DPMtF-WebUI" not in text

    # Every config access goes through this package's own module.
    assert any(
        "from knowledge_service import config"
        in path.read_text(encoding="utf-8")
        for path in files
    )

    # TG2's import path: the package resolves without any other checkout dir.
    saved = list(sys.path)
    try:
        sys.path[:] = [p for p in sys.path if "DPMtF-WebUI" not in p]
        sys.path.insert(0, str(Path.cwd()))
        for name in (
            "knowledge_service.config",
            "knowledge_service.provider",
            "knowledge_service.leann_provider",
            "knowledge_service.portable_provider",
            "knowledge_service.indexer",
            "knowledge_service.maintenance",
            "knowledge_service.scopes",
            "knowledge_service.scope_guard",
            "knowledge_service.retrieval_log",
            "knowledge_service.search",
            "knowledge_service.db",
            "knowledge_service.cli",
            "knowledge_service.app",
        ):
            module = importlib.import_module(name)
            importlib.reload(module)
            assert module.__name__ == name
    finally:
        sys.path[:] = saved


# ── retrieval-log flow key (run 034 parity) ─────────────────────────────


_OLD_LOG_TABLE = """
CREATE TABLE knowledge_retrieval_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    scope TEXT NOT NULL,
    query TEXT NOT NULL,
    result_count INTEGER NOT NULL DEFAULT 0,
    sources TEXT NOT NULL DEFAULT '[]',
    retrieved_token_count INTEGER NOT NULL DEFAULT 0,
    retrieval_duration_ms INTEGER NOT NULL DEFAULT 0,
    agent_role TEXT,
    run_id TEXT,
    handoff_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _log_columns(conn: sqlite3.Connection) -> list:
    return [row[1] for row in conn.execute(
        "pragma table_info(knowledge_retrieval_log)"
    ).fetchall()]


def test_schema_has_flow_key_and_upgrades_an_older_database(tmp_path, monkeypatch):
    """Fresh databases get the column; a pre-run-034 file is upgraded."""
    write_ini(tmp_path, monkeypatch, service_ini(tmp_path, monkeypatch))

    fresh = sqlite3.connect(tmp_path / "fresh.db")
    db.ensure_schema(fresh)
    columns = _log_columns(fresh)
    # Column order matters for schema parity with the shared databases: the
    # column is appended last, exactly where ALTER TABLE puts it.
    assert columns[-1] == "flow_key"
    assert columns.index("handoff_id") < columns.index("flow_key")
    fresh.close()

    # An older file: table without the column, holding one row.
    old = sqlite3.connect(tmp_path / "old.db")
    old.executescript(_OLD_LOG_TABLE)
    old.execute(
        "INSERT INTO knowledge_retrieval_log (provider, scope, query, result_count)"
        " VALUES ('leann', 'flowrunner', 'budget', 3)"
    )
    old.commit()

    db.ensure_schema(old)

    assert _log_columns(old)[-1] == "flow_key"
    rows = old.execute(
        "SELECT query, result_count, flow_key FROM knowledge_retrieval_log"
    ).fetchall()
    # Existing rows survive and read back with a NULL flow key.
    assert [(row[0], row[1], row[2]) for row in rows] == [("budget", 3, None)]

    # The upgrade is a no-op the second time around.
    db.ensure_schema(old)
    assert _log_columns(old)[-1] == "flow_key"
    assert old.execute(
        "SELECT COUNT(*) FROM knowledge_retrieval_log"
    ).fetchone()[0] == 1
    old.close()


def test_search_route_records_the_flow_key(tmp_path, monkeypatch):
    install_stub(monkeypatch)
    stub_torch(monkeypatch)
    write_ini(tmp_path, monkeypatch, service_ini(tmp_path, monkeypatch))
    client = make_client()

    response = client.get(
        "/v1/search",
        params={
            "q": "budget",
            "scope": "flowrunner",
            "agent_role": "dsh",
            "run_id": "034",
            "flow_key": "9000-01-PLOOP",
        },
    )
    assert response.status_code == 200

    rows = log_rows()
    assert len(rows) == 1
    assert rows[0]["flow_key"] == "9000-01-PLOOP"
    assert rows[0]["run_id"] == "034"

    # Without the parameter the column stays NULL, not an empty string.
    assert client.get(
        "/v1/search", params={"q": "budget", "scope": "flowrunner"}
    ).status_code == 200
    rows = log_rows()
    assert len(rows) == 2
    assert rows[1]["flow_key"] is None


def _source_with_flow_key(tmp_path) -> str:
    """Source database in the post-run-034 shape (column present)."""
    source = tmp_path / "with-flow-key.db"
    conn = sqlite3.connect(source)
    conn.executescript(db.SCHEMA_SQL)
    conn.execute(
        "INSERT INTO knowledge_retrieval_log"
        " (provider, scope, query, result_count, flow_key)"
        " VALUES ('leann', 'flowrunner', 'budget', 3, '9000-01-PLOOP')"
    )
    conn.execute(
        "INSERT INTO knowledge_retrieval_log"
        " (provider, scope, query, result_count, flow_key)"
        " VALUES ('leann', 'flowrunner', 'exclusions', 1, NULL)"
    )
    conn.commit()
    conn.close()
    return str(source)


def _source_without_flow_key(tmp_path) -> str:
    """Source database from before run **034** (column absent)."""
    source = tmp_path / "before-034.db"
    conn = sqlite3.connect(source)
    conn.executescript(_OLD_LOG_TABLE)
    conn.execute(
        "INSERT INTO knowledge_retrieval_log (provider, scope, query, result_count)"
        " VALUES ('leann', 'flowrunner', 'legacy', 2)"
    )
    conn.commit()
    conn.close()
    return str(source)


def _imported_log_rows() -> list:
    conn = db.connect()
    try:
        return conn.execute(
            "SELECT query, flow_key FROM knowledge_retrieval_log ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def test_import_registry_copies_flow_key_when_present(tmp_path, monkeypatch):
    # One fresh target per source: the import keeps original ids, so rows from
    # two sources would otherwise compete on id.
    write_ini(
        tmp_path, monkeypatch, service_ini(tmp_path, monkeypatch, db_path=str(tmp_path / "after.db"))
    )
    assert cli.main(["import-registry", "--from", _source_with_flow_key(tmp_path)]) == 0
    rows = {row[0]: row[1] for row in _imported_log_rows()}
    assert rows == {"budget": "9000-01-PLOOP", "exclusions": None}

    # A source from before run 034 has no column to copy: its row arrives with
    # a NULL flow key instead of an invented one.
    write_ini(
        tmp_path, monkeypatch, service_ini(tmp_path, monkeypatch, db_path=str(tmp_path / "before.db"))
    )
    assert cli.main(["import-registry", "--from", _source_without_flow_key(tmp_path)]) == 0
    assert {row[0]: row[1] for row in _imported_log_rows()} == {"legacy": None}


# ── isolation of operator storage ───────────────────────────────────────


def test_registry_rows_land_in_the_test_database_only(tmp_path, monkeypatch):
    """Registry writes go to the per-test database, not the operator's file."""
    write_ini(tmp_path, monkeypatch, service_ini(tmp_path, monkeypatch))

    maintenance.record_index("alpha", "leann", str(tmp_path / "alpha"), 3, "changed")
    maintenance.record_index("beta", "leann", str(tmp_path / "beta"), 0, "missing")

    test_db = Path(config.get_db_path())
    assert str(test_db).startswith(str(tmp_path))

    conn = sqlite3.connect(test_db)
    try:
        rows = dict(conn.execute(
            "select scope, document_count from knowledge_indexes order by scope"
        ))
    finally:
        conn.close()
    assert rows == {"alpha": 3, "beta": 0}

    # The operator's own file is a different location and is never consulted:
    # the resolved path came from the INI this test wrote.
    operator_db = Path.home() / ".local/share/knowledge-service/knowledge.db"
    assert test_db != operator_db


def test_config_rereads_the_ini_env_on_every_call(tmp_path, monkeypatch):
    """A changed KNOWLEDGE_SERVICE_INI is honoured without an explicit reload."""
    first = tmp_path / "first.ini"
    first.write_text(
        "[knowledge]\nindex_dir = %s\n\n[service]\ndb_path = %s\n"
        % (tmp_path / "one_index", tmp_path / "one.db"),
        encoding="utf-8",
    )
    second = tmp_path / "second.ini"
    second.write_text(
        "[knowledge]\nindex_dir = %s\n\n[service]\ndb_path = %s\n"
        % (tmp_path / "two_index", tmp_path / "two.db"),
        encoding="utf-8",
    )

    monkeypatch.setenv("KNOWLEDGE_SERVICE_INI", str(first))
    assert config.get_db_path().endswith("one.db")
    assert config.get_index_dir().endswith("one_index")
    assert config.ini_path() == str(first)

    # Same process, no reload() call: the new value must win on the next call.
    monkeypatch.setenv("KNOWLEDGE_SERVICE_INI", str(second))
    assert config.get_db_path().endswith("two.db")
    assert config.get_index_dir().endswith("two_index")
    assert config.ini_path() == str(second)


def test_guard_fixture_detects_a_write_outside_tmp(tmp_path):
    """The guard fixture notices a write into a copy of the operator layout."""
    # The autouse fixture is in effect for this test already.
    assert str(os.environ["KNOWLEDGE_SERVICE_INI"]).startswith(str(tmp_path))

    layout = tmp_path / "operator-layout"
    (layout / "knowledge_index").mkdir(parents=True)
    database = layout / "knowledge.db"
    database.write_text("one\n", encoding="utf-8")
    store = layout / "knowledge_index" / "flowrunner.jsonl"
    store.write_text("alpha\n", encoding="utf-8")

    before = snapshot_tree(layout)
    assert changed_entries(before, snapshot_tree(layout)) == []

    database.write_text("two and longer\n", encoding="utf-8")
    assert changed_entries(before, snapshot_tree(layout)) == ["knowledge.db"]

    store.unlink()
    assert changed_entries(before, snapshot_tree(layout)) == [
        "knowledge.db",
        "knowledge_index/flowrunner.jsonl",
    ]

    # A missing directory is not a change: nothing to compare against.
    assert snapshot_tree(layout / "absent") == {}
    assert changed_entries({}, snapshot_tree(layout / "absent")) == []

    assert OPERATOR_DIRS == (
        Path.home() / ".local/share/knowledge-service",
        Path.home() / ".local/share/dpmtf/knowledge_index",
    )


# ── portable provider ───────────────────────────────────────────────────


class FakeEmbedder:
    """Deterministic embedder for tests: one axis per keyword, no dependency."""

    dim = 3

    def __init__(self) -> None:
        self.calls: list = []

    def embed(self, texts):
        self.calls.append(list(texts))
        return [
            [1.0, 0.0, 0.0] if "apple" in text else [0.0, 1.0, 0.0]
            for text in texts
        ]


def write_manifest(tmp_path, records) -> str:
    """Write ``records`` as a JSONL manifest and return its path."""
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )
    return str(manifest)


def model_home(tmp_path) -> Path:
    """Create a model directory holding the two files the provider reads."""
    directory = tmp_path / "model"
    (directory / "onnx").mkdir(parents=True, exist_ok=True)
    (directory / "onnx" / "model.onnx").write_bytes(b"fake-onnx")
    (directory / "tokenizer.json").write_text("{}", encoding="utf-8")
    return directory


def portable_provider(tmp_path, embedder=None) -> PortableProvider:
    return PortableProvider(
        index_path=tmp_path / "store" / "s.portable.db",
        model_dir=model_home(tmp_path),
        embedder=embedder or FakeEmbedder(),
    )


def portable_ini(tmp_path, monkeypatch, extra: str = "") -> None:
    write_ini(
        tmp_path,
        monkeypatch,
        service_ini(tmp_path, monkeypatch)
        + "\n[portable]\nmodel_dir = %s\n%s" % (tmp_path / "model", extra),
    )


def test_onnx_embedder_pools_a_realistic_session_output(tmp_path, monkeypatch):
    """A real session returns (batch, seq, hidden); pooling must collapse it."""
    import numpy as np

    portable_ini(tmp_path, monkeypatch)

    class FakeEncoded:
        def __init__(self, ids):
            self.ids = ids

    class FakeTokenizer:
        def encode(self, text, add_special_tokens=True):
            # Two special tokens around one id per word, as a real tokenizer does.
            return FakeEncoded(list(range(1, len(text.split()) + 3)))

    class FakeSpec:
        def __init__(self, name):
            self.name = name

    class FakeSession:
        """Stands in for onnxruntime.InferenceSession, arrays only."""

        def __init__(self):
            self.feeds = []

        def get_inputs(self):
            return [FakeSpec("input_ids"), FakeSpec("attention_mask")]

        def run(self, _, feeds):
            self.feeds.append(dict(feeds))
            ids = feeds["input_ids"]
            batch, width = int(ids.shape[0]), int(ids.shape[1])
            hidden = 4
            out = np.zeros((batch, width, hidden), dtype=np.float32)
            for position in range(width):
                # Non-zero on padding positions too, so a pooling step that
                # ignores the attention mask cannot look correct.
                out[:, position, :] = np.asarray(
                    [(position + 1) + 0.5 * h for h in range(hidden)],
                    dtype=np.float32,
                )
            return [out]

    texts = ["apple pie", "how is a flowapp exported"]
    session = FakeSession()
    embedder = OnnxEmbedder(
        tmp_path / "model", session=session, tokenizer=FakeTokenizer()
    )

    vectors = embedder.embed(texts)

    assert len(vectors) == len(texts)
    assert [len(vector) for vector in vectors] == [4, 4]

    lengths = [len(text.split()) + 2 for text in texts]
    assert int(session.feeds[0]["attention_mask"].sum()) == sum(lengths)
    assert session.feeds[0]["input_ids"].shape == (2, max(lengths))

    for vector, length in zip(vectors, lengths):
        # Mean over the real positions only: (pos + 1) averages to
        # (length + 1) / 2, plus the hidden-axis offset. Then unit length.
        expected = np.asarray(
            [(length + 1) / 2 + 0.5 * h for h in range(4)], dtype=np.float32
        )
        expected = expected / np.linalg.norm(expected)
        assert vector == pytest.approx([float(v) for v in expected], rel=1e-5, abs=1e-6)

    # Padding to a longer batch partner leaves the shorter text's vector alone.
    assert embedder.embed([texts[0]])[0] == pytest.approx(
        [float(v) for v in vectors[0]], rel=1e-5, abs=1e-6
    )
    assert embedder.dim == 4


def test_portable_index_search_roundtrip_with_fake_embedder(tmp_path, monkeypatch):
    portable_ini(tmp_path, monkeypatch)
    embedder = FakeEmbedder()
    provider = portable_provider(tmp_path, embedder)
    manifest = write_manifest(
        tmp_path,
        [
            {"scope": "s", "path": "docs/a.md", "content": "apple pie"},
            {"scope": "s", "path": "docs/b.md", "content": "banana split"},
        ],
    )

    assert provider.index(manifest) is None

    results = provider.search("apple", scope="s")
    assert [item["path"] for item in results] == ["docs/a.md", "docs/b.md"]
    assert results[0]["score"] == 1.0
    assert results[1]["score"] == 0.0
    assert results[0]["content"] == "apple pie"
    assert results[0]["scope"] == "s"
    assert set(results[0]) == {"path", "content", "score", "scope"}

    # Embeddings go in as float32 blobs with their width and model id.
    conn = sqlite3.connect(tmp_path / "store" / "s.portable.db")
    rows = conn.execute(
        "select id, dim, model, length(embedding) from passages order by id"
    ).fetchall()
    conn.close()
    assert [row[0] for row in rows] == ["s:docs/a.md", "s:docs/b.md"]
    assert {row[1] for row in rows} == {3}
    assert {row[3] for row in rows} == {12}
    assert {row[2] for row in rows} == {config.get_portable_model_id()}

    # One batched call per index, then a single call for the query.
    assert embedder.calls == [["apple pie", "banana split"], ["apple"]]


def test_portable_update_replaces_and_remove_deletes(tmp_path, monkeypatch):
    portable_ini(tmp_path, monkeypatch)
    provider = portable_provider(tmp_path)
    manifest = write_manifest(
        tmp_path,
        [
            {"scope": "s", "path": "docs/a.md", "content": "apple pie"},
            {"scope": "s", "path": "docs/b.md", "content": "banana split"},
        ],
    )
    provider.index(manifest)

    # A rewritten manifest replaces the passage under the same id and drops the
    # one it no longer lists. Neither call is a no-op or NotImplementedError.
    manifest = write_manifest(
        tmp_path, [{"scope": "s", "path": "docs/a.md", "content": "apple crumble"}]
    )
    assert provider.update(manifest) is None
    assert [item["content"] for item in provider.search("apple", scope="s")] == [
        "apple crumble"
    ]

    assert provider.remove(manifest) is None
    assert provider.search("apple", scope="s") == []


def test_portable_scope_filter_and_token_budget(tmp_path, monkeypatch):
    portable_ini(tmp_path, monkeypatch)
    provider = portable_provider(tmp_path)
    write_manifest(
        tmp_path,
        [
            {"scope": "s", "path": "docs/a.md", "content": "apple one two three"},
            {"scope": "other", "path": "docs/b.md", "content": "apple two"},
        ],
    )
    provider.index(str(tmp_path / "manifest.jsonl"))

    assert [item["scope"] for item in provider.search("apple", scope="s")] == ["s"]
    assert [item["path"] for item in provider.search("apple", scope="other")] == [
        "docs/b.md"
    ]
    filtered = provider.search("apple", scope="s", filters={"path": "docs/a.md"})
    assert [item["path"] for item in filtered] == ["docs/a.md"]
    assert provider.search("apple", scope="s", filters={"path": "docs/b.md"}) == []

    # Same truncation rule as the LEANN provider: cut to fit, never pad.
    bounded = provider.search("apple", scope="s", token_budget=2)
    assert bounded[0]["content"] == "apple one"
    assert len(provider.search("apple", scope="s", top_k=1)) == 1


def test_portable_preflight_reports_missing_model(tmp_path, monkeypatch):
    portable_ini(tmp_path, monkeypatch)
    empty = tmp_path / "empty-model"
    empty.mkdir()
    missing = PortableProvider(
        index_path=tmp_path / "s.portable.db",
        model_dir=empty,
        embedder=FakeEmbedder(),
    )

    # ``pytest.raises`` matches by class identity, and the import test reloads
    # the package modules, so the check is on the name the interface promises.
    with pytest.raises(Exception) as info:
        missing.preflight()
    assert type(info.value).__name__ == "ProviderNotReady"
    message = str(info.value)
    assert message.startswith("knowledge provider not ready:")
    assert "model.onnx" in message and str(empty) in message
    assert missing.readiness_detail() == "model present: no"

    ready = portable_provider(tmp_path)
    assert ready.preflight() is None
    assert ready.readiness_detail() == "model present: yes"


def test_resolve_provider_binds_portable_store_path(tmp_path, monkeypatch):
    portable_ini(tmp_path, monkeypatch, extra="model_id = mini/model\n")

    factory = search_module.resolve_provider("portable", scope="flowrunner")
    provider = factory()
    # Class identity moves when the import test reloads the package, so the
    # check is on the promised class name.
    assert type(provider).__name__ == "PortableProvider"
    assert str(provider._index_path).endswith("flowrunner.portable.db")
    assert str(tmp_path / "index_dir") in str(provider._index_path)
    assert str(provider._model_dir) == str(tmp_path / "model")
    assert config.get_portable_model_id() == "mini/model"

    # Without an explicit scope the configured scope binds the store.
    unscoped = search_module.resolve_provider("portable")()
    assert str(unscoped._index_path).endswith("dpmtf-webui.portable.db")

    # Defaults of the [portable] section, from an ini that does not mention it.
    write_ini(tmp_path, monkeypatch, service_ini(tmp_path, monkeypatch))
    assert config.get_portable_model_id() == (
        "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    )
    assert config.get_portable_model_dir().endswith(
        "knowledge-service/models/paraphrase-multilingual-MiniLM-L12-v2"
    )


@pytest.mark.parametrize("provider_key", ["none", "portable"])
def test_provider_contract_is_shared(provider_key, tmp_path, monkeypatch):
    """Both providers satisfy the same interface without raising."""
    portable_ini(tmp_path, monkeypatch)
    if provider_key == "none":
        provider = search_module.resolve_provider("none")()
    else:
        provider = portable_provider(tmp_path)

    manifest = write_manifest(
        tmp_path, [{"scope": "s", "path": "docs/a.md", "content": "apple pie"}]
    )

    assert provider.preflight() is None
    assert provider.index(manifest) is None
    assert provider.update(manifest) is None

    results = provider.search("apple", scope="s", top_k=3, token_budget=10)
    assert isinstance(results, list)
    for item in results:
        assert {"path", "content", "score", "scope"} <= set(item)
    if provider_key == "none":
        assert results == []
    else:
        assert results[0]["path"] == "docs/a.md"

    assert provider.remove(manifest) is None
