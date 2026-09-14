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

    Mirrors the two real-provider facts the correction demands: an empty
    manifest is refused exactly as LEANN refuses it (``No chunks added.``),
    and indexing leaves store files for the scope under the index dir.
    """

    index_calls: list[str] = []
    search_calls: list[dict] = []

    def __init__(self, index_path: str | None = None) -> None:
        self.index_path = index_path

    def preflight(self) -> None:
        return None

    def index(self, source: str) -> None:
        RecordingProvider.index_calls.append(source)
        path = Path(source)
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
        records = [line for line in text.splitlines() if line.strip()]
        if not records:
            raise RuntimeError("No chunks added.")
        scope = path.stem
        marker_dir = Path(config.get_index_dir())
        marker_dir.mkdir(parents=True, exist_ok=True)
        (marker_dir / f"{scope}.stub").write_text(
            "\n".join(records), encoding="utf-8"
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
        return [
            {
                "path": "2000/029.yaml",
                "content": "alpha beta",
                "score": 0.9,
                "scope": scope,
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
    monkeypatch.setitem(
        search_module.PROVIDER_LOADERS, "stub", lambda: RecordingProvider
    )


def make_doc(**overrides) -> dict:
    """A schema-valid learning artifact; ``overrides`` replace top-level keys."""
    doc = {
        "topic": "Share one index directory across scopes",
        "scope": "experience",
        "repository": "flowrunner",
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
    assert not learning.learning_dir().joinpath("2000", "029.yaml").is_file()
    assert not (learning.learning_dir() / "LEDGER.md").is_file()

    close_run(tmp_path, "2000", "029", status="SUCCESS")
    assert learning.admit(str(draft)) == 0
    assert learning.learning_dir().joinpath("2000", "029.yaml").is_file()

    # A missing runs directory skips the closure check with a warning: run
    # 030 has no directory under runs_root but is admitted anyway.
    doc_030 = make_doc(run="030")
    draft_030 = write_draft(tmp_path, doc_030, "draft030.yaml")
    assert learning.admit(str(draft_030)) == 0
    assert learning.learning_dir().joinpath("2000", "030.yaml").is_file()


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
    assert record["path"] == "2000/029.yaml"
    metadata = record["metadata"]
    assert metadata["scope"] == "experience"
    assert metadata["path"] == "2000/029.yaml"
    assert metadata["evidence_level"] == "tests"
    assert metadata["repository"] == "flowrunner"
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
        "2000/029/Share one index directory across scopes"
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
    assert directory.joinpath("2000", "029.yaml").is_file()
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
    assert directory.joinpath("2000", "021.yaml").is_file()

    assert learning.admit(str(write_draft(tmp_path, newer, "new.yaml"))) == 0

    assert not directory.joinpath("2000", "021.yaml").is_file()
    history_file = directory / "history" / "2000" / "021.yaml"
    assert history_file.is_file()

    moved = yaml.safe_load(history_file.read_text(encoding="utf-8"))
    assert moved["superseded_by"] == "2000/029"

    live_paths = [
        record["path"] for record in read_jsonl(directory / "experience.jsonl")
    ]
    assert live_paths == ["2000/029.yaml"]
    history_records = read_jsonl(directory / "experience-history.jsonl")
    assert len(history_records) == 1
    assert history_records[0]["path"] == "2000/021.yaml"
    assert history_records[0]["metadata"]["superseded_by"] == "2000/029"


def test_retract_moves_to_history_and_rebuilds(tmp_path, monkeypatch):
    setup_service(tmp_path, monkeypatch)
    close_run(tmp_path, "2000", "029")
    assert learning.admit(str(write_draft(tmp_path, make_doc()))) == 0
    RecordingProvider.index_calls = []

    assert learning.retract("2000/029") == 0

    directory = learning.learning_dir()
    assert not directory.joinpath("2000", "029.yaml").is_file()
    history_file = directory / "history" / "2000" / "029.yaml"
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
