"""Hermetic tests for the validated learning artifacts.

Every test runs in pytest's ``tmp_path``: the INI (written per test) points
``index_dir``, the database, ``[learning] dir`` and ``[learning] runs_root``
inside it, and a recording stub provider stands in for a real one. Nothing
here needs a running service, a model file, or a network.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from knowledge_service import cli, config, db, learning, maintenance
from knowledge_service.app import create_app
from knowledge_service import search as search_module

# ── helpers ─────────────────────────────────────────────────────────────


class RecordingProvider:
    """Stub provider recording how the learning paths use it.

    Mirrors the real-provider facts the tests need: an empty manifest is
    refused exactly as LEANN refuses it (``No chunks added.``), indexing
    leaves store-marker files for the scope under the index dir, ``search``
    returns the indexed records with a ``metadata`` mapping whose identity
    keys are removed, and ``store_exists`` answers from the marker file.
    """

    index_calls: list[str] = []
    search_calls: list[dict] = []
    records: dict[str, list[dict]] = {}
    search_error: Exception | None = None

    def __init__(self, index_path: str | None = None) -> None:
        self.index_path = index_path

    def preflight(self) -> None:
        return None

    @classmethod
    def store_exists(cls, scope: str | None = None) -> bool:
        name = scope or config.get_scope()
        return (Path(config.get_index_dir()) / f"{name}.stub").is_file()

    def index(self, source: str) -> None:
        RecordingProvider.index_calls.append(source)
        path = Path(source)
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
        lines = [line for line in text.splitlines() if line.strip()]
        if not lines:
            raise RuntimeError("No chunks added.")
        scope = path.stem
        RecordingProvider.records[scope] = [json.loads(line) for line in lines]
        marker_dir = Path(config.get_index_dir())
        marker_dir.mkdir(parents=True, exist_ok=True)
        (marker_dir / f"{scope}.stub").write_text(
            "\n".join(lines), encoding="utf-8"
        )

    def update(self, source: str) -> None:
        RecordingProvider.index_calls.append(source)

    def remove(self, source: str) -> None:
        return None

    def search(
        self, query, *, scope=None, filters=None, top_k=None, token_budget=None
    ):
        RecordingProvider.search_calls.append(
            {"query": query, "scope": scope, "filters": filters}
        )
        if RecordingProvider.search_error is not None:
            raise RecordingProvider.search_error
        name = scope or config.get_scope()
        hits = []
        for record in RecordingProvider.records.get(name, []):
            metadata = record.get("metadata") or {}
            hits.append(
                {
                    "path": record["path"],
                    "content": record["content"],
                    "score": 0.9,
                    "scope": name,
                    "metadata": {
                        key: value
                        for key, value in metadata.items()
                        if key not in {"id", "path", "scope"}
                    },
                }
            )
        if hits:
            return hits
        return [
            {
                "path": "README.md",
                "content": "alpha beta",
                "score": 0.9,
                "scope": name,
                "metadata": {},
            }
        ]


def setup_service(tmp_path, monkeypatch, enabled: str = "true") -> None:
    """Point config at temp paths, register the stub provider, reset records."""
    index_dir = tmp_path / "index_dir"
    body = (
        "[knowledge]\n"
        f"enabled = {enabled}\n"
        "provider = stub\n"
        "scope = dpmtf-webui\n"
        "top_k = 8\n"
        "max_context_tokens = 1200\n"
        "index_dir = %s\n"
        "\n"
        "[service]\n"
        f"db_path = {tmp_path / 'knowledge.db'}\n"
        "token =\n"
        "\n"
        "[learning]\n"
        f"dir = {index_dir / 'learning'}\n"
        f"runs_root = {tmp_path / 'flows'}\n"
    ) % index_dir
    ini = tmp_path / "knowledge.ini"
    ini.write_text(body, encoding="utf-8")
    monkeypatch.setenv("KNOWLEDGE_SERVICE_INI", str(ini))
    config.reload()
    RecordingProvider.index_calls = []
    RecordingProvider.search_calls = []
    RecordingProvider.records = {}
    RecordingProvider.search_error = None
    monkeypatch.setitem(
        search_module.PROVIDER_LOADERS, "stub", lambda: RecordingProvider
    )


def seed_repository_scope(tmp_path) -> None:
    """Give the ``flowrunner`` scope a registry row and a stub store marker."""
    maintenance.record_index(
        "flowrunner", "stub", str(tmp_path / "repo"), 1, "changed",
        repository_path=str(tmp_path / "repo"),
    )
    index_dir = Path(config.get_index_dir())
    index_dir.mkdir(parents=True, exist_ok=True)
    (index_dir / "flowrunner.stub").write_text("seed", encoding="utf-8")


def make_doc(**overrides) -> dict:
    """A schema-valid learning artifact; ``overrides`` replace top-level keys."""
    doc = {
        "topic": "Share one index directory across scopes",
        "scope": "experience",
        "repository": "dpmtf-webui",
        "family": "2000",
        "run": "029",
        "problem": "Each scope rebuilt its own tree scan.",
        "approach": "Build scopes from manifests instead.",
        "result": "Rebuild takes seconds; 7/7 goals green.",
        "failed_approaches": ["Watching mtime only: missed renames."],
        "important_files": ["knowledge_service/maintenance.py"],
        "architecture_implications": [
            "One LEANN store per scope, rebuilt from the manifest."
        ],
        "validation": {
            "evidence_level": "tests",
            "verdicts": ["002 APPROVED"],
            "testgoals": "7/7",
        },
        "confidence": "high",
        "supersedes": [],
        "admitted_by": "supervisor",
    }
    doc.update(overrides)
    return doc


def write_draft(tmp_path: Path, doc: dict, name: str = "draft.yaml") -> Path:
    draft = tmp_path / name
    draft.write_text(
        yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    return draft


def close_run(tmp_path: Path, family: str, run: str, status: str = "SUCCESS") -> None:
    """Write the END-REPORT exactly as real chains do: bold status line."""
    run_dir = tmp_path / "flows" / family / "runs" / run
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "END-REPORT.md").write_text(
        f"# END-REPORT {family}/{run}\n**Status:** {status}\ntext\n", encoding="utf-8"
    )


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def registry_rows() -> dict[str, tuple[str, str, int]]:
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT scope, location, document_count, status FROM knowledge_indexes"
        ).fetchall()
    finally:
        conn.close()
    return {
        row["scope"]: (row["location"], row["status"], row["document_count"])
        for row in rows
    }


# ── schema ──────────────────────────────────────────────────────────────


def test_learning_schema_rejects_missing_keys_and_hypothesis():
    doc = make_doc()
    assert learning.validate(doc) == []

    missing = dict(doc)
    del missing["problem"]
    violations = learning.validate(missing)
    assert violations and any("problem" in line for line in violations)

    hypothesis = dict(doc)
    hypothesis["validation"] = dict(doc["validation"], evidence_level="hypothesis")
    assert learning.validate(hypothesis)

    wrong_scope = dict(doc, scope="ecosystem")
    assert learning.validate(wrong_scope)

    weak_confidence = dict(doc, confidence="very-high")
    assert learning.validate(weak_confidence)

    broken_lists = dict(doc, failed_approaches="watched mtime")
    assert learning.validate(broken_lists)


# ── closure proof ───────────────────────────────────────────────────────


def test_check_run_closed_accepts_bold_status_lines(tmp_path, monkeypatch):
    setup_service(tmp_path, monkeypatch)

    def write_report(body: str) -> None:
        run_dir = tmp_path / "flows" / "2000" / "runs" / "041"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "END-REPORT.md").write_text(
            f"# END-REPORT 2000/041\n{body}\ntext\n", encoding="utf-8"
        )

    write_report("**Status:** SUCCESS")
    assert learning._check_run_closed("2000", "041") == ""

    write_report("**Status: SUCCESS** — run closed.")
    assert learning._check_run_closed("2000", "041") == ""

    write_report("**Status:** BLOCKED")
    assert learning._check_run_closed("2000", "041")


def test_admit_refuses_a_run_without_success_end_report(tmp_path, monkeypatch):
    setup_service(tmp_path, monkeypatch)
    draft = write_draft(tmp_path, make_doc())

    close_run(tmp_path, "2000", "029", status="BLOCKED")
    assert learning.admit(str(draft)) == 1
    assert not learning.learning_dir().joinpath("dpmtf-webui", "2000", "029.yaml").is_file()
    assert not (learning.learning_dir() / "LEDGER.md").is_file()

    close_run(tmp_path, "2000", "029", status="SUCCESS")
    assert learning.admit(str(draft)) == 0
    assert learning.learning_dir().joinpath("dpmtf-webui", "2000", "029.yaml").is_file()

    # A missing runs directory skips the closure check with a warning: run
    # 030 has no directory under runs_root but is admitted anyway.
    doc_030 = make_doc(run="030")
    draft_030 = write_draft(tmp_path, doc_030, "draft030.yaml")
    assert learning.admit(str(draft_030)) == 0
    assert learning.learning_dir().joinpath("dpmtf-webui", "2000", "030.yaml").is_file()


# ── admission ───────────────────────────────────────────────────────────


def test_admit_writes_manifests_and_rebuilds_both_scopes(tmp_path, monkeypatch):
    setup_service(tmp_path, monkeypatch)
    close_run(tmp_path, "2000", "029")
    draft = write_draft(tmp_path, make_doc())

    assert learning.admit(str(draft)) == 0

    directory = learning.learning_dir()
    experience = read_jsonl(directory / "experience.jsonl")
    assert len(experience) == 1
    record = experience[0]
    assert record["scope"] == "experience"
    assert record["path"] == "dpmtf-webui/2000/029.yaml"
    metadata = record["metadata"]
    assert metadata["scope"] == "experience"
    assert metadata["path"] == "dpmtf-webui/2000/029.yaml"
    assert metadata["evidence_level"] == "tests"
    assert metadata["repository"] == "dpmtf-webui"
    assert metadata["family"] == "2000"
    assert metadata["run"] == "029"
    assert metadata["confidence"] == "high"
    content = record["content"]
    assert "Share one index directory across scopes" in content
    assert "Watching mtime only" in content
    assert "tests" in content and "7/7" in content

    ecosystem = read_jsonl(directory / "ecosystem.jsonl")
    assert len(ecosystem) == 1
    assert "One LEANN store per scope" in ecosystem[0]["content"]
    assert ecosystem[0]["metadata"]["origin"] == (
        "dpmtf-webui/2000/029/Share one index directory across scopes"
    )

    sources = RecordingProvider.index_calls
    assert any(source.endswith("experience.jsonl") for source in sources)
    assert any(source.endswith("ecosystem.jsonl") for source in sources)

    rows = registry_rows()
    location = str(Path(config.get_learning_dir()))
    assert rows["experience"] == (location, "changed", 1)
    assert rows["ecosystem"] == (location, "changed", 1)


def test_admit_first_artifact_exits_zero_with_empty_history(tmp_path, monkeypatch):
    setup_service(tmp_path, monkeypatch)
    close_run(tmp_path, "2000", "029")
    draft = write_draft(tmp_path, make_doc())

    # History holds nothing yet: the empty history manifest must not reach
    # the provider (the stub refuses empty manifests like LEANN does), and
    # the whole admission still succeeds.
    assert learning.admit(str(draft)) == 0

    directory = learning.learning_dir()
    assert directory.joinpath("dpmtf-webui", "2000", "029.yaml").is_file()
    ledger_lines = [
        line
        for line in (directory / "LEDGER.md").read_text(encoding="utf-8").splitlines()
        if line.startswith("- ")
    ]
    assert len(ledger_lines) == 1

    rows = registry_rows()
    location = str(Path(config.get_learning_dir()))
    assert rows["experience-history"] == (location, "empty", 0)
    assert rows["experience"][1] == "changed"
    assert rows["experience"][2] == 1
    # The live stores exist as store files; history keeps none.
    index_dir = Path(config.get_index_dir())
    assert (index_dir / "experience.stub").is_file()
    assert not (index_dir / "experience-history.stub").exists()


def test_supersede_moves_the_older_artifact_to_history(tmp_path, monkeypatch):
    setup_service(tmp_path, monkeypatch)
    close_run(tmp_path, "2000", "021")
    close_run(tmp_path, "2000", "029")
    old = make_doc(run="021", topic="Old conclusion")
    newer = make_doc(run="029", topic="Measured the other way", supersedes=["2000/021"])

    assert learning.admit(str(write_draft(tmp_path, old, "old.yaml"))) == 0
    directory = learning.learning_dir()
    assert directory.joinpath("dpmtf-webui", "2000", "021.yaml").is_file()

    assert learning.admit(str(write_draft(tmp_path, newer, "new.yaml"))) == 0

    assert not directory.joinpath("dpmtf-webui", "2000", "021.yaml").is_file()
    history_file = directory / "history" / "dpmtf-webui" / "2000" / "021.yaml"
    assert history_file.is_file()

    moved = yaml.safe_load(history_file.read_text(encoding="utf-8"))
    assert moved["superseded_by"] == "dpmtf-webui/2000/029"

    live_paths = [
        record["path"] for record in read_jsonl(directory / "experience.jsonl")
    ]
    assert live_paths == ["dpmtf-webui/2000/029.yaml"]
    history_records = read_jsonl(directory / "experience-history.jsonl")
    assert len(history_records) == 1
    assert history_records[0]["path"] == "dpmtf-webui/2000/021.yaml"
    assert history_records[0]["metadata"]["superseded_by"] == "dpmtf-webui/2000/029"


def test_retract_moves_to_history_and_rebuilds(tmp_path, monkeypatch):
    setup_service(tmp_path, monkeypatch)
    close_run(tmp_path, "2000", "029")
    assert learning.admit(str(write_draft(tmp_path, make_doc()))) == 0
    RecordingProvider.index_calls = []

    assert learning.retract("2000/029") == 0

    directory = learning.learning_dir()
    assert not directory.joinpath("dpmtf-webui", "2000", "029.yaml").is_file()
    history_file = directory / "history" / "dpmtf-webui" / "2000" / "029.yaml"
    assert history_file.is_file()

    moved = yaml.safe_load(history_file.read_text(encoding="utf-8"))
    assert moved["retracted_at"]

    assert read_jsonl(directory / "experience.jsonl") == []
    history_records = read_jsonl(directory / "experience-history.jsonl")
    assert len(history_records) == 1
    assert history_records[0]["metadata"]["retracted_at"]

    # Every non-empty rebuild refreshes its store; history is refreshed last.
    assert RecordingProvider.index_calls[-1].endswith("experience-history.jsonl")

    # The emptied live scope records the empty state instead of a stale store.
    rows = registry_rows()
    assert rows["experience"][1] == "empty"
    assert rows["experience"][2] == 0

    assert learning.retract("2000/missing") == 1


# ── empty manifests and empty stores ────────────────────────────────────


def test_refresh_manifest_scope_empty_manifest_builds_no_store(
    tmp_path, monkeypatch
):
    setup_service(tmp_path, monkeypatch)
    index_dir = Path(config.get_index_dir())
    index_dir.mkdir(parents=True, exist_ok=True)

    # Store files an earlier build left behind, in both providers' naming.
    (index_dir / "experience.leann").mkdir()
    (index_dir / "experience.leann.meta.json").write_text("{}", encoding="utf-8")
    (index_dir / "experience.portable.db").write_text("stale", encoding="utf-8")

    manifest = tmp_path / "experience.jsonl"
    manifest.write_text("\n   \n", encoding="utf-8")

    result = maintenance.refresh_manifest_scope("experience", str(manifest))

    assert result["status"] == "empty"
    assert result["documents"] == 0
    assert RecordingProvider.index_calls == []
    assert not (index_dir / "experience.leann").exists()
    assert not (index_dir / "experience.leann.meta.json").exists()
    assert not (index_dir / "experience.portable.db").exists()

    rows = registry_rows()
    location = str(Path(config.get_learning_dir()))
    assert rows["experience"] == (location, "empty", 0)


# ── search ──────────────────────────────────────────────────────────────


def test_search_filters_experience_by_evidence_level(tmp_path, monkeypatch):
    setup_service(tmp_path, monkeypatch)
    close_run(tmp_path, "2000", "029")
    assert learning.admit(str(write_draft(tmp_path, make_doc()))) == 0

    client = TestClient(create_app())
    seed_repository_scope(tmp_path)

    response = client.get("/v1/search", params={"q": "index", "scope": "experience"})
    assert response.status_code == 200
    call = RecordingProvider.search_calls[-1]
    assert call["scope"] == "experience"
    assert call["filters"] == {
        "evidence_level": {
            "in": ["tests", "measured_runtime", "approved_architecture"]
        }
    }
    assert len(response.json()["results"]) == 1

    response = client.get(
        "/v1/search",
        params={
            "q": "index",
            "scope": "experience",
            "evidence_level": "reviewer_conclusion",
        },
    )
    assert response.status_code == 200
    allowed = RecordingProvider.search_calls[-1]["filters"]["evidence_level"]["in"]
    assert allowed == [
        "tests",
        "measured_runtime",
        "approved_architecture",
        "reviewer_conclusion",
    ]

    response = client.get("/v1/search", params={"q": "index", "scope": "flowrunner"})
    assert response.status_code == 200
    call = RecordingProvider.search_calls[-1]
    assert call["scope"] == "flowrunner"
    assert call["filters"] is None


def test_search_include_history_targets_the_history_scope(tmp_path, monkeypatch):
    setup_service(tmp_path, monkeypatch)
    close_run(tmp_path, "2000", "021")
    close_run(tmp_path, "2000", "029")
    assert learning.admit(
        str(write_draft(tmp_path, make_doc(run="021"), "old.yaml"))
    ) == 0
    assert learning.admit(
        str(
            write_draft(
                tmp_path, make_doc(run="029", supersedes=["2000/021"]), "new.yaml"
            )
        )
    ) == 0
    RecordingProvider.search_calls = []

    client = TestClient(create_app())
    seed_repository_scope(tmp_path)

    response = client.get(
        "/v1/search",
        params={"q": "index", "scope": "experience", "include_history": "true"},
    )
    assert response.status_code == 200
    call = RecordingProvider.search_calls[-1]
    assert call["scope"] == "experience-history"
    assert call["filters"] is not None

    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT scope FROM knowledge_retrieval_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row["scope"] == "experience-history"

    response = client.get(
        "/v1/search",
        params={"q": "index", "scope": "flowrunner", "include_history": "true"},
    )
    assert response.status_code == 200
    assert RecordingProvider.search_calls[-1]["scope"] == "flowrunner"


def test_search_empty_learning_scope_returns_no_results(tmp_path, monkeypatch):
    setup_service(tmp_path, monkeypatch)
    close_run(tmp_path, "2000", "029")
    assert learning.admit(str(write_draft(tmp_path, make_doc()))) == 0
    RecordingProvider.search_calls = []

    client = TestClient(create_app())

    # History holds nothing yet: both entry points answer 200 with an empty
    # list and never reach the provider.
    response = client.get("/v1/search", params={"q": "index", "scope": "experience-history"})
    assert response.status_code == 200
    assert response.json()["results"] == []

    response = client.get(
        "/v1/search",
        params={"q": "index", "scope": "experience", "include_history": "true"},
    )
    assert response.status_code == 200
    assert response.json()["results"] == []
    assert RecordingProvider.search_calls == []

    # The retrieval is logged like any other.
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT scope FROM knowledge_retrieval_log WHERE scope = 'experience-history'"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 2

    # The populated scope keeps its normal path.
    response = client.get("/v1/search", params={"q": "index", "scope": "experience"})
    assert response.status_code == 200
    assert len(response.json()["results"]) == 1


# ── maintenance ─────────────────────────────────────────────────────────


def test_refresh_all_skips_learning_scopes(tmp_path, monkeypatch, capsys):
    setup_service(tmp_path, monkeypatch, enabled="false")
    directory = Path(config.get_learning_dir())
    directory.mkdir(parents=True, exist_ok=True)
    repo = tmp_path / "repo"
    repo.mkdir()

    maintenance.record_index("experience", "stub", str(directory), 2, "changed")
    maintenance.record_index("ecosystem", "stub", str(directory), 1, "changed")
    maintenance.record_index(
        "experience-history", "stub", str(directory), 0, "empty"
    )
    maintenance.record_index("flowrunner", "stub", str(repo), 9, "changed")

    calls: list[str] = []
    monkeypatch.setattr(
        maintenance,
        "refresh_scope",
        lambda scope, repo_path: calls.append(scope)
        or {"status": "noop", "manifest": ""},
    )

    assert cli.main(["refresh-all"]) == 0

    assert calls == ["flowrunner"]
    captured = capsys.readouterr()
    assert "skip experience" in captured.err
    assert "skip ecosystem" in captured.err
    assert "skip experience-history" in captured.err

    # /v1/scopes lists the learning scopes like any other registry row.
    scopes = {
        row["scope"]
        for row in db.connect().execute("SELECT scope FROM knowledge_indexes")
    }
    assert {"experience", "ecosystem", "experience-history"} <= scopes


# ── ledger ──────────────────────────────────────────────────────────────


def test_learning_ledger_records_every_admission(tmp_path, monkeypatch):
    setup_service(tmp_path, monkeypatch)
    close_run(tmp_path, "2000", "029")
    assert learning.admit(str(write_draft(tmp_path, make_doc()))) == 0

    ledger = learning.learning_dir() / "LEDGER.md"
    assert ledger.is_file()
    text = ledger.read_text(encoding="utf-8")
    assert text.startswith("# Learning Ledger")
    lines = [line for line in text.splitlines() if line.startswith("- ")]
    assert len(lines) == 1
    assert "admitted" in lines[0]
    assert "2000/029" in lines[0]
    assert "| tests |" in lines[0]
    assert "supervisor" in lines[0]

    assert learning.retract("2000/029") == 0
    lines = [
        line
        for line in ledger.read_text(encoding="utf-8").splitlines()
        if line.startswith("- ")
    ]
    assert len(lines) == 2
    assert "retracted" in lines[1]


# ── A2-2: error contract, metadata, listing ─────────────────────────────


def _log_row_count() -> int:
    conn = db.connect()
    try:
        return len(
            conn.execute("SELECT 1 FROM knowledge_retrieval_log").fetchall()
        )
    finally:
        conn.close()


def test_search_unknown_scope_is_404_without_a_log_row(tmp_path, monkeypatch):
    setup_service(tmp_path, monkeypatch)
    client = TestClient(create_app())

    response = client.get(
        "/v1/search", params={"q": "x", "scope": "no-such-scope-xyz"}
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "unknown scope: no-such-scope-xyz"
    assert _log_row_count() == 0

    # Learning scopes keep the 200-empty behaviour even before any admit.
    response = client.get("/v1/search", params={"q": "x", "scope": "experience"})
    assert response.status_code == 200
    assert response.json()["results"] == []
    assert _log_row_count() == 1


def test_search_known_scope_with_missing_store_is_503(tmp_path, monkeypatch):
    setup_service(tmp_path, monkeypatch)
    maintenance.record_index("notes", "stub", str(tmp_path / "repo"), 3, "changed")
    client = TestClient(create_app())

    response = client.get("/v1/search", params={"q": "x", "scope": "notes"})
    assert response.status_code == 503
    assert response.json()["detail"] == (
        "store missing for scope notes; refresh it"
    )
    assert RecordingProvider.search_calls == []
    assert _log_row_count() == 0

    # A present store marker opens the normal path again.
    index_dir = Path(config.get_index_dir())
    index_dir.mkdir(parents=True, exist_ok=True)
    (index_dir / "notes.stub").write_text("{}", encoding="utf-8")
    response = client.get("/v1/search", params={"q": "x", "scope": "notes"})
    assert response.status_code == 200
    assert len(response.json()["results"]) == 1


def test_provider_store_errors_never_leak_as_500(tmp_path, monkeypatch):
    setup_service(tmp_path, monkeypatch)
    maintenance.record_index("notes", "stub", str(tmp_path / "repo"), 1, "changed")
    index_dir = Path(config.get_index_dir())
    index_dir.mkdir(parents=True, exist_ok=True)
    (index_dir / "notes.stub").write_text("{}", encoding="utf-8")
    RecordingProvider.search_error = FileNotFoundError(
        "/tmp/gone/notes.leann.meta.json"
    )
    client = TestClient(create_app())

    response = client.get("/v1/search", params={"q": "x", "scope": "notes"})
    assert response.status_code == 503
    assert "notes.leann.meta.json" in response.json()["detail"]
    assert RecordingProvider.search_calls[-1]["scope"] == "notes"
    assert _log_row_count() == 0


def test_search_results_carry_passage_metadata(tmp_path, monkeypatch):
    setup_service(tmp_path, monkeypatch)
    close_run(tmp_path, "2000", "029")
    assert learning.admit(str(write_draft(tmp_path, make_doc()))) == 0
    seed_repository_scope(tmp_path)

    client = TestClient(create_app())

    response = client.get("/v1/search", params={"q": "index", "scope": "experience"})
    assert response.status_code == 200
    hit = response.json()["results"][0]
    assert hit["path"] == "dpmtf-webui/2000/029.yaml"
    metadata = hit["metadata"]
    assert metadata["evidence_level"] == "tests"
    assert metadata["family"] == "2000"
    assert metadata["run"] == "029"
    assert metadata["repository"] == "dpmtf-webui"
    assert metadata["confidence"] == "high"
    # Identity keys sit on the result itself, not twice inside metadata.
    assert "path" not in metadata
    assert "scope" not in metadata
    assert "id" not in metadata

    response = client.get("/v1/search", params={"q": "index", "scope": "flowrunner"})
    assert response.status_code == 200
    assert response.json()["results"][0]["metadata"] == {}


def test_learning_route_lists_admitted_and_history(tmp_path, monkeypatch, capsys):
    setup_service(tmp_path, monkeypatch)
    close_run(tmp_path, "2000", "029")
    close_run(tmp_path, "1000", "007")
    close_run(tmp_path, "1000", "012")
    assert learning.admit(
        str(write_draft(tmp_path, make_doc(), "a.yaml"))
    ) == 0
    assert learning.admit(
        str(
            write_draft(
                tmp_path,
                make_doc(family="1000", run="007", topic="Older one"),
                "b.yaml",
            )
        )
    ) == 0
    assert learning.admit(
        str(
            write_draft(
                tmp_path,
                make_doc(
                    family="1000",
                    run="012",
                    topic="Newest one",
                    supersedes=["1000/007"],
                ),
                "c.yaml",
            )
        )
    ) == 0

    client = TestClient(create_app())

    live = client.get("/v1/learning")
    assert live.status_code == 200
    artifacts = live.json()["artifacts"]
    assert [(item["family"], item["run"]) for item in artifacts] == [
        ("1000", "012"),
        ("2000", "029"),
    ]
    assert artifacts[0]["topic"] == "Newest one"
    assert artifacts[0]["supersedes"] == ["1000/007"]
    assert artifacts[0]["evidence_level"] == "tests"
    assert artifacts[0]["confidence"] == "high"
    assert artifacts[0]["admitted_by"] == "supervisor"
    # The route answer is exactly the pure function's answer.
    assert artifacts == learning.list_artifact_records()

    history = client.get("/v1/learning", params={"history": "true"})
    assert history.status_code == 200
    old = history.json()["artifacts"]
    assert [(item["family"], item["run"]) for item in old] == [("1000", "007")]
    assert old[0]["superseded_by"] == "dpmtf-webui/1000/012"
    assert old[0]["retracted_at"] is None

    assert RecordingProvider.search_calls == []

    # The CLI listing shares the records, printing byte-identical lines.
    assert cli.main(["learning", "list"]) == 0
    printed = capsys.readouterr().out.splitlines()
    assert printed[0] == "dpmtf-webui/1000/012\tNewest one\ttests\thigh\tsupervisor"
    assert printed[1] == (
        "dpmtf-webui/2000/029\tShare one index directory across scopes\ttests\thigh\tsupervisor"
    )


# ── A2-3: admission from the run directory, pending drafts ──────────────


def write_run_draft(tmp_path: Path, family: str, run: str, doc: dict) -> Path:
    """Write ``doc`` to ``<runs_root>/<family>/runs/<run>/LEARNING-DRAFT.yaml``."""
    path = learning.draft_path(None, family, run)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    return path


def ledger_lines() -> list:
    ledger = learning.learning_dir() / "LEDGER.md"
    if not ledger.is_file():
        return []
    return [
        line
        for line in ledger.read_text(encoding="utf-8").splitlines()
        if line.startswith("- ")
    ]


def test_admit_run_reads_the_draft_from_the_runs_root_and_sets_admitted_by(
    tmp_path, monkeypatch
):
    setup_service(tmp_path, monkeypatch)
    close_run(tmp_path, "2000", "041")
    draft = write_run_draft(
        tmp_path, "2000", "041", make_doc(run="041", admitted_by="pending")
    )
    before = draft.read_bytes()

    assert learning.draft_path(None, "2000", "041") == draft
    assert learning.admit_run("2000/041", "svend") == 0

    artifact = yaml.safe_load(
        (learning.learning_dir() / "dpmtf-webui" / "2000" / "041.yaml").read_text(encoding="utf-8")
    )
    assert artifact["admitted_by"] == "svend"
    assert artifact["topic"] == "Share one index directory across scopes"

    # The draft keeps its bytes and its place; only the artifact carries the name.
    assert draft.read_bytes() == before
    assert draft.is_file()

    lines = ledger_lines()
    assert len(lines) == 1
    assert "| admitted | dpmtf-webui/2000/041 | tests | svend |" in lines[0]
    assert lines[0].endswith(f"| source={draft}")


def test_admit_run_refuses_pending_missing_and_unclosed(tmp_path, monkeypatch):
    setup_service(tmp_path, monkeypatch)
    close_run(tmp_path, "2000", "041")
    write_run_draft(
        tmp_path, "2000", "041", make_doc(run="041", admitted_by="pending")
    )
    directory = learning.learning_dir()

    assert learning.admit_run("2000/041", "pending") == 1
    assert learning.admit_run("2000/041", "Pending") == 1
    assert learning.admit_run("2000/041", "   ") == 1
    assert learning.admit_run("no-slash", "svend") == 1
    assert learning.admit_run("1000/007", "svend") == 1  # no draft there
    assert not directory.joinpath("dpmtf-webui", "2000", "041.yaml").is_file()
    assert ledger_lines() == []

    close_run(tmp_path, "2000", "041", status="BLOCKED")
    assert learning.admit_run("2000/041", "svend") == 1
    assert not directory.joinpath("dpmtf-webui", "2000", "041.yaml").is_file()
    assert ledger_lines() == []


def test_validate_run_reports_violations_and_run_status_without_admitting(
    tmp_path, monkeypatch, capsys
):
    setup_service(tmp_path, monkeypatch)
    close_run(tmp_path, "2000", "041")
    write_run_draft(
        tmp_path,
        "2000",
        "041",
        make_doc(run="041", admitted_by="pending", confidence="very-high"),
    )

    assert learning.validate_run("2000/041") == 1
    printed = capsys.readouterr().out.splitlines()
    assert any("confidence" in line for line in printed)
    assert "run status: SUCCESS" in printed

    directory = learning.learning_dir()
    assert not directory.joinpath("dpmtf-webui", "2000", "041.yaml").is_file()
    assert ledger_lines() == []

    write_run_draft(tmp_path, "2000", "041", make_doc(run="041"))
    assert learning.validate_run("2000/041") == 0
    assert capsys.readouterr().out.splitlines() == ["run status: SUCCESS"]

    # Markdown decoration and a trailing note still read as the bare word.
    report = tmp_path / "flows" / "2000" / "runs" / "041" / "END-REPORT.md"
    report.write_text(
        "# END-REPORT\n**Status: SUCCESS** — run closed.\n", encoding="utf-8"
    )
    assert learning.validate_run("2000/041") == 0
    assert capsys.readouterr().out.splitlines() == ["run status: SUCCESS"]

    close_run(tmp_path, "1000", "007", status="BLOCKED")
    write_run_draft(tmp_path, "1000", "007", make_doc(family="1000", run="007"))
    assert learning.validate_run("1000/007") == 0
    assert capsys.readouterr().out.splitlines() == ["run status: BLOCKED"]

    # No END-REPORT reads as missing; a missing draft is a clean refusal.
    write_run_draft(tmp_path, "3000", "001", make_doc(family="3000", run="001"))
    assert learning.validate_run("3000/001") == 0
    assert capsys.readouterr().out.splitlines() == ["run status: missing"]
    assert learning.validate_run("4000/002") == 1


def test_drafts_route_lists_pending_drafts_with_run_status(tmp_path, monkeypatch):
    setup_service(tmp_path, monkeypatch)
    assert learning.list_pending_drafts() == []

    close_run(tmp_path, "1000", "007")
    close_run(tmp_path, "1000", "012")
    close_run(tmp_path, "2000", "041")
    write_run_draft(
        tmp_path,
        "1000",
        "007",
        make_doc(family="1000", run="007", topic="Older one", admitted_by="pending"),
    )
    write_run_draft(
        tmp_path,
        "1000",
        "012",
        make_doc(family="1000", run="012", topic="Newest one", admitted_by="pending"),
    )
    broken = learning.draft_path(None, "2000", "041")
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text("topic: [unclosed\n", encoding="utf-8")

    assert learning.admit_run("1000/012", "svend") == 0

    client = TestClient(create_app())

    response = client.get("/v1/learning/drafts")
    assert response.status_code == 200
    items = response.json()["drafts"]
    assert items == learning.list_pending_drafts()
    assert [(item["family"], item["run"]) for item in items] == [
        ("1000", "007"),
        ("1000", "012"),
        ("2000", "041"),
    ]
    assert items[0]["topic"] == "Older one"
    assert items[0]["evidence_level"] == "tests"
    assert items[0]["run_status"] == "SUCCESS"
    assert items[0]["valid"] is True
    assert items[0]["violations"] == 0
    assert items[0]["admitted"] is False
    assert items[1]["admitted"] is True

    pending = client.get("/v1/learning/drafts", params={"pending": "true"})
    assert pending.status_code == 200
    kept = pending.json()["drafts"]
    assert [(item["family"], item["run"]) for item in kept] == [
        ("1000", "007"),
        ("2000", "041"),
    ]

    broken_entry = items[2]
    assert broken_entry["valid"] is False
    assert broken_entry["violations"] == 1
    assert broken_entry["topic"] == ""
    assert broken_entry["run_status"] == "SUCCESS"

    assert RecordingProvider.search_calls == []


def test_cli_learning_admit_run_and_drafts(tmp_path, monkeypatch, capsys):
    setup_service(tmp_path, monkeypatch)
    close_run(tmp_path, "1000", "007")
    close_run(tmp_path, "2000", "041")
    write_run_draft(
        tmp_path,
        "1000",
        "007",
        make_doc(family="1000", run="007", topic="Older one", admitted_by="pending"),
    )
    write_run_draft(
        tmp_path, "2000", "041", make_doc(run="041", admitted_by="pending")
    )

    assert cli.main(["learning", "drafts"]) == 0
    printed = capsys.readouterr().out.splitlines()
    assert len(printed) == 2
    assert printed[0] == "dpmtf-webui/1000/007\tOlder one\ttests\tSUCCESS\tpending\tvalid\t0"
    assert printed[1].startswith("dpmtf-webui/2000/041\tShare one index directory")
    assert printed[1].endswith("\tSUCCESS\tpending\tvalid\t0")

    assert cli.main(["learning", "validate-run", "2000/041"]) == 0
    assert capsys.readouterr().out.splitlines() == ["run status: SUCCESS"]

    assert (
        cli.main(
            ["learning", "admit-run", "2000/041", "--admitted-by", "svend"]
        )
        == 0
    )
    artifact = learning.learning_dir() / "dpmtf-webui" / "2000" / "041.yaml"
    assert artifact.is_file()
    doc = yaml.safe_load(artifact.read_text(encoding="utf-8"))
    assert doc["admitted_by"] == "svend"
    assert ledger_lines()[-1].endswith(
        f"| source={learning.draft_path(None, '2000', '041')}"
    )

    # The CLI writes exactly where the function writes, through the same path.
    assert learning.learning_dir().joinpath(
        "dpmtf-webui", "1000", "007.yaml"
    ) == learning._artifact_path(None, "1000", "007")
    assert learning.admit_run("1000/007", "reviewer") == 0
    assert learning.learning_dir().joinpath("dpmtf-webui", "1000", "007.yaml").is_file()

    assert cli.main(["learning", "drafts"]) == 0
    printed = capsys.readouterr().out.splitlines()
    assert [line.split("\t")[0] for line in printed] == ["dpmtf-webui/1000/007", "dpmtf-webui/2000/041"]
    assert all("\tadmitted\tvalid\t0" in line for line in printed)

    assert cli.main(["learning", "admit-run", "no-slash", "--admitted-by", "x"]) == 1


# ── A2-4: repository-scoped artifacts ───────────────────────────────────


def test_artifact_key_carries_the_repository_slug(tmp_path, monkeypatch):
    setup_service(tmp_path, monkeypatch)
    close_run(tmp_path, "2000", "029")
    assert learning.admit(str(write_draft(tmp_path, make_doc(), "father.yaml"))) == 0
    assert learning.admit(
        str(write_draft(tmp_path, make_doc(repository="FlowRunner"), "child.yaml"))
    ) == 0

    directory = learning.learning_dir()
    assert directory.joinpath("dpmtf-webui", "2000", "029.yaml").is_file()
    assert directory.joinpath("flowrunner", "2000", "029.yaml").is_file()
    assert not directory.joinpath("2000", "029.yaml").exists()

    records = learning.list_artifact_records()
    assert [
        (item["repository"], item["family"], item["run"]) for item in records
    ] == [
        ("dpmtf-webui", "2000", "029"),
        ("flowrunner", "2000", "029"),
    ]

    lines = ledger_lines()
    assert len(lines) == 2
    assert any("dpmtf-webui/2000/029" in line for line in lines)
    assert any("flowrunner/2000/029" in line for line in lines)


def test_legacy_layout_is_migrated_once_with_a_ledger_line(tmp_path, monkeypatch):
    setup_service(tmp_path, monkeypatch)
    directory = learning.learning_dir()
    legacy = directory / "2000" / "029.yaml"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(
        yaml.safe_dump(make_doc(), sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    legacy_history = directory / "history" / "1000" / "007.yaml"
    legacy_history.parent.mkdir(parents=True)
    legacy_history.write_text(
        yaml.safe_dump(make_doc(family="1000", run="007"), sort_keys=False), encoding="utf-8"
    )

    assert learning.migrate_legacy_layout() == 2

    assert directory.joinpath("dpmtf-webui", "2000", "029.yaml").is_file()
    assert directory.joinpath("history", "dpmtf-webui", "1000", "007.yaml").is_file()
    assert not legacy.exists()
    assert not legacy_history.exists()

    lines = ledger_lines()
    assert len(lines) == 2
    assert all("migrated" in line for line in lines)
    assert any("dpmtf-webui/2000/029" in line for line in lines)
    assert any("dpmtf-webui/1000/007" in line for line in lines)

    # Idempotent: a second call moves nothing and writes no new lines.
    assert learning.migrate_legacy_layout() == 0
    assert len(ledger_lines()) == 2

    # rebuild keeps the migrated tree; manifests carry the new layout paths.
    assert learning.rebuild() == 0
    paths = [record["path"] for record in read_jsonl(directory / "experience.jsonl")]
    assert paths == ["dpmtf-webui/2000/029.yaml"]


def test_supersedes_accepts_two_and_three_part_references(tmp_path, monkeypatch):
    setup_service(tmp_path, monkeypatch)
    close_run(tmp_path, "2000", "021")
    close_run(tmp_path, "2000", "029")
    assert learning.admit(
        str(write_draft(tmp_path, make_doc(run="021"), "old.yaml"))
    ) == 0
    assert learning.admit(
        str(
            write_draft(
                tmp_path,
                make_doc(supersedes=["dpmtf-webui/2000/021"]),
                "new.yaml",
            )
        )
    ) == 0
    directory = learning.learning_dir()
    moved = yaml.safe_load(
        directory.joinpath(
            "history", "dpmtf-webui", "2000", "021.yaml"
        ).read_text(encoding="utf-8")
    )
    assert moved["superseded_by"] == "dpmtf-webui/2000/029"

    close_run(tmp_path, "1000", "007")
    close_run(tmp_path, "1000", "012")
    assert learning.admit(
        str(write_draft(tmp_path, make_doc(family="1000", run="007"), "seven.yaml"))
    ) == 0
    assert learning.admit(
        str(
            write_draft(
                tmp_path,
                make_doc(family="1000", run="012", supersedes=["1000/007"]),
                "twelve.yaml",
            )
        )
    ) == 0
    moved = yaml.safe_load(
        directory.joinpath(
            "history", "dpmtf-webui", "1000", "007.yaml"
        ).read_text(encoding="utf-8")
    )
    assert moved["superseded_by"] == "dpmtf-webui/1000/012"

    doc = make_doc()
    assert learning.validate(dict(doc, supersedes=["dpmtf-webui/2000/021"])) == []
    assert learning.validate(dict(doc, supersedes=["2000/021"])) == []
    assert learning.validate(dict(doc, supersedes=["one-part"]))
    assert learning.validate(dict(doc, supersedes=["a/b/c/d"]))


def test_drafts_are_found_under_every_registered_repository(tmp_path, monkeypatch):
    setup_service(tmp_path, monkeypatch)
    seed_repository_scope(tmp_path)
    maintenance.record_index(
        "alpha", "stub", str(tmp_path / "repo-alpha"), 1, "changed",
        repository_path=str(tmp_path / "repo-alpha"),
    )

    flow_run = tmp_path / "repo" / ".flowrunner" / "1000" / "runs" / "007"
    flow_run.mkdir(parents=True)
    flow_run.joinpath("LEARNING-DRAFT.yaml").write_text(
        yaml.safe_dump(
            make_doc(family="1000", run="007", repository="flowrunner"),
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    flow_run.joinpath("END-REPORT.md").write_text(
        "# END-REPORT\n**Status:** SUCCESS\n", encoding="utf-8"
    )

    alpha_run = tmp_path / "repo-alpha" / ".flowrunner" / "3000" / "runs" / "001"
    alpha_run.mkdir(parents=True)
    alpha_run.joinpath("LEARNING-DRAFT.yaml").write_text(
        yaml.safe_dump(
            make_doc(family="3000", run="001", repository="alpha"), sort_keys=False
        ),
        encoding="utf-8",
    )

    close_run(tmp_path, "2000", "041")
    write_run_draft(tmp_path, "2000", "041", make_doc(run="041"))

    rows = learning.list_pending_drafts()
    assert [
        (item["repository"], item["family"], item["run"]) for item in rows
    ] == [
        ("flowrunner", "1000", "007"),
        ("dpmtf-webui", "2000", "041"),
        ("alpha", "3000", "001"),
    ]
    assert rows[0]["run_status"] == "SUCCESS"
    assert rows[2]["run_status"] == "missing"
    assert all(item["admitted"] is False for item in rows)

    client = TestClient(create_app())
    response = client.get("/v1/learning/drafts")
    assert response.status_code == 200
    assert response.json()["drafts"] == rows

    assert learning.admit_run("flowrunner/1000/007", "svend") == 0
    assert learning.learning_dir().joinpath(
        "flowrunner", "1000", "007.yaml"
    ).is_file()

    artifacts = client.get("/v1/learning").json()["artifacts"]
    assert [
        (item["repository"], item["family"], item["run"]) for item in artifacts
    ] == [("flowrunner", "1000", "007")]
    kept = client.get(
        "/v1/learning", params={"repository": "FlowRunner"}
    ).json()["artifacts"]
    assert [(item["repository"], item["family"]) for item in kept] == [
        ("flowrunner", "1000")
    ]
    assert client.get(
        "/v1/learning", params={"repository": "alpha"}
    ).json()["artifacts"] == []


def test_admit_run_refuses_a_draft_whose_repository_does_not_match(
    tmp_path, monkeypatch
):
    setup_service(tmp_path, monkeypatch)
    close_run(tmp_path, "2000", "041")
    write_run_draft(
        tmp_path, "2000", "041", make_doc(run="041", repository="flowrunner")
    )
    directory = learning.learning_dir()

    assert learning.admit_run("dpmtf-webui/2000/041", "svend") == 1
    assert learning.admit_run("2000/041", "svend") == 1
    assert not directory.joinpath("dpmtf-webui", "2000", "041.yaml").is_file()
    assert ledger_lines() == []

    write_run_draft(tmp_path, "2000", "041", make_doc(run="041"))
    assert learning.admit_run("dpmtf-webui/2000/041", "svend") == 0
    assert directory.joinpath("dpmtf-webui", "2000", "041.yaml").is_file()
    lines = ledger_lines()
    assert len(lines) == 1
    assert "dpmtf-webui/2000/041" in lines[0]


def test_learning_routes_and_cli_take_three_part_refs(tmp_path, monkeypatch, capsys):
    setup_service(tmp_path, monkeypatch)
    close_run(tmp_path, "1000", "007")
    close_run(tmp_path, "2000", "041")
    write_run_draft(
        tmp_path,
        "1000",
        "007",
        make_doc(
            family="1000", run="007", topic="Older one", admitted_by="pending"
        ),
    )
    write_run_draft(
        tmp_path, "2000", "041", make_doc(run="041", admitted_by="pending")
    )

    client = TestClient(create_app())
    items = client.get(
        "/v1/learning/drafts", params={"pending": "true"}
    ).json()["drafts"]
    assert [
        (item["repository"], item["family"], item["run"]) for item in items
    ] == [
        ("dpmtf-webui", "1000", "007"),
        ("dpmtf-webui", "2000", "041"),
    ]

    assert (
        cli.main(
            ["learning", "admit-run", "dpmtf-webui/2000/041", "--admitted-by", "svend"]
        )
        == 0
    )
    assert learning.learning_dir().joinpath(
        "dpmtf-webui", "2000", "041.yaml"
    ).is_file()
    capsys.readouterr()

    artifacts = client.get("/v1/learning").json()["artifacts"]
    assert [
        (item["repository"], item["family"], item["run"]) for item in artifacts
    ] == [("dpmtf-webui", "2000", "041")]
    kept = client.get(
        "/v1/learning", params={"repository": "DPMtF-WebUI"}
    ).json()["artifacts"]
    assert kept == artifacts
    assert client.get(
        "/v1/learning", params={"repository": "flowrunner"}
    ).json()["artifacts"] == []

    assert cli.main(["learning", "list"]) == 0
    printed = capsys.readouterr().out.splitlines()
    assert printed[0].startswith("dpmtf-webui/2000/041\t")

    assert cli.main(["learning", "validate-run", "dpmtf-webui/9999/001"]) == 1
    assert "does not exist" in capsys.readouterr().err

    assert cli.main(["learning", "validate-run", "one"]) == 1
    assert "reference must be" in capsys.readouterr().err

    assert cli.main(["learning", "validate-run", "nosuchrepo/2000/041"]) == 1
    assert "unknown repository: nosuchrepo" in capsys.readouterr().err

    assert learning.retract("dpmtf-webui/2000/041") == 0
    assert learning.learning_dir().joinpath(
        "history", "dpmtf-webui", "2000", "041.yaml"
    ).is_file()
    history = client.get("/v1/learning", params={"history": "true"}).json()["artifacts"]
    assert history[0]["repository"] == "dpmtf-webui"
    assert history[0]["retracted_at"]


def test_refresh_records_the_repository_path_and_refresh_all_uses_it(
    tmp_path, monkeypatch, capsys
):
    setup_service(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    repo.joinpath("notes.md").write_text("# notes\n", encoding="utf-8")

    result = maintenance.refresh_scope("beta", str(repo))
    assert result["status"] in {"changed", "noop", "reindexed"}

    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT repository_path FROM knowledge_indexes WHERE scope = ?",
            ("beta",),
        ).fetchone()
    finally:
        conn.close()
    assert Path(row["repository_path"]).resolve() == repo.resolve()

    # An imported-style row: manifest location, empty repository_path.
    maintenance.record_index(
        "gamma",
        "stub",
        str(Path(config.get_index_dir()) / "gamma.jsonl"),
        1,
        "changed",
    )

    assert cli.main(["refresh-all", "--from-registry", "--dry-run"]) == 0
    printed = capsys.readouterr()
    assert any(line.startswith("beta\t") for line in printed.out.splitlines())
    assert "gamma" not in printed.out
    assert "repository_path" in printed.err
    assert "set-repository gamma" in printed.err

    assert cli.main(["set-repository", "gamma", str(repo)]) == 0
    capsys.readouterr()
    assert cli.main(["refresh-all", "--from-registry", "--dry-run"]) == 0
    printed = capsys.readouterr()
    assert any(line.startswith("gamma\t") for line in printed.out.splitlines())


def test_set_repository_cli_and_scopes_route_show_the_path(
    tmp_path, monkeypatch, capsys
):
    setup_service(tmp_path, monkeypatch)
    maintenance.record_index(
        "delta", "stub", str(tmp_path / "delta.jsonl"), 1, "changed"
    )

    assert cli.main(["set-repository", "delta", str(tmp_path / "gone")]) == 1
    assert "existing directory" in capsys.readouterr().err

    repo = tmp_path / "repo-delta"
    repo.mkdir()
    assert cli.main(["set-repository", "delta", str(repo)]) == 0
    capsys.readouterr()

    client = TestClient(create_app())
    response = client.get("/v1/scopes")
    assert response.status_code == 200
    rows = {row["scope"]: row for row in response.json()}
    assert Path(rows["delta"]["repository_path"]).resolve() == repo.resolve()
