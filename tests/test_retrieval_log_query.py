"""Tests for reading ``knowledge_retrieval_log``: query, summary, route, CLI.

Every test works on a temp database seeded directly; nothing here needs the
live service or the operator's own log.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from knowledge_service import cli, config, db, retrieval_log
from knowledge_service.app import create_app

# ── helpers ──────────────────────────────────────────────────────────────


def write_ini(tmp_path, monkeypatch, db_name: str = "knowledge.db") -> None:
    """Point the config loader at a per-test INI with a temp database."""
    ini = tmp_path / "knowledge.ini"
    ini.write_text(
        "[knowledge]\nenabled = true\nprovider = stub\nscope = dpmtf-webui\n"
        f"index_dir = {tmp_path / 'index_dir'}\n\n"
        "[service]\n"
        f"db_path = {tmp_path / db_name}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("KNOWLEDGE_SERVICE_INI", str(ini))
    config.reload()


def seed(tmp_path, monkeypatch, rows: list[dict]) -> None:
    """Create the schema in a temp database and insert the given rows."""
    write_ini(tmp_path, monkeypatch)
    conn = db.connect()
    try:
        for row in rows:
            columns = list(row)
            conn.execute(
                f"INSERT INTO knowledge_retrieval_log ({', '.join(columns)})"
                f" VALUES ({', '.join('?' for _ in columns)})",
                tuple(row[key] for key in columns),
            )
        conn.commit()
    finally:
        conn.close()


def standard_rows() -> list[dict]:
    """Four rows spanning two scopes, roles and runs, minutes apart."""
    return [
        {
            "provider": "stub", "scope": "dpmtf", "query": "q-one",
            "result_count": 2, "sources": '["a.md"]',
            "retrieved_token_count": 20, "retrieval_duration_ms": 5,
            "agent_role": "dsh", "run_id": "046", "handoff_id": "H1",
            "created_at": "2026-09-15 08:00:00", "flow_key": "9000-01-PLOOP",
        },
        {
            "provider": "stub", "scope": "other", "query": "q-two",
            "result_count": 1, "sources": "[]",
            "retrieved_token_count": 10, "retrieval_duration_ms": 7,
            "agent_role": "supervisor", "run_id": "046", "handoff_id": "H2",
            "created_at": "2026-09-15 09:00:00", "flow_key": None,
        },
        {
            "provider": "stub", "scope": "dpmtf", "query": "q-three",
            "result_count": 3, "sources": "[]",
            "retrieved_token_count": 30, "retrieval_duration_ms": 9,
            "agent_role": "dsh", "run_id": "047", "handoff_id": "H3",
            "created_at": "2026-09-15 10:00:00", "flow_key": None,
        },
        {
            "provider": "stub", "scope": "dpmtf", "query": "q-four",
            "result_count": 4, "sources": "[]",
            "retrieved_token_count": 40, "retrieval_duration_ms": 11,
            "agent_role": "dsh", "run_id": "046", "handoff_id": "H4",
            "created_at": "2026-09-15 11:00:00", "flow_key": None,
        },
    ]


# ── query_retrievals ─────────────────────────────────────────────────────


def test_query_filters_combine_and_page_newest_first(tmp_path, monkeypatch):
    seed(tmp_path, monkeypatch, standard_rows())

    answer = retrieval_log.query_retrievals()
    assert [row["query"] for row in answer["rows"]] == [
        "q-four", "q-three", "q-two", "q-one",
    ]
    assert answer["total"] == 4
    assert answer["limit"] == 50 and answer["offset"] == 0

    one_run = retrieval_log.query_retrievals(run_id="046")
    assert one_run["total"] == 3
    combined = retrieval_log.query_retrievals(run_id="046", scope="dpmtf")
    assert [row["query"] for row in combined["rows"]] == ["q-four", "q-one"]
    assert combined["total"] == 2

    window = retrieval_log.query_retrievals(
        since="2026-09-15 09:30:00", until="2026-09-15 10:30:00"
    )
    assert [row["query"] for row in window["rows"]] == ["q-three"]
    by_role = retrieval_log.query_retrievals(agent_role="supervisor")
    assert by_role["total"] == 1
    by_flow = retrieval_log.query_retrievals(flow_key="9000-01-PLOOP")
    assert by_flow["total"] == 1
    by_handoff = retrieval_log.query_retrievals(handoff_id="H2")
    assert by_handoff["rows"][0]["query"] == "q-two"

    page = retrieval_log.query_retrievals(limit=2, offset=1)
    assert [row["query"] for row in page["rows"]] == ["q-three", "q-two"]
    assert page["total"] == 4  # total counts before the limit
    assert page["limit"] == 2 and page["offset"] == 1

    assert retrieval_log.query_retrievals(limit=9999)["limit"] == 500
    assert retrieval_log.query_retrievals(limit=0)["limit"] == 1
    assert retrieval_log.query_retrievals(offset=-3)["offset"] == 0


def test_query_decodes_sources_and_survives_a_malformed_row(tmp_path, monkeypatch):
    seed(tmp_path, monkeypatch, standard_rows())

    answer = retrieval_log.query_retrievals()
    by_query = {row["query"]: row for row in answer["rows"]}
    assert by_query["q-one"]["sources"] == ["a.md"]
    assert by_query["q-two"]["sources"] == []

    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO knowledge_retrieval_log"
            " (provider, scope, query, sources, created_at)"
            " VALUES ('stub', 'dpmtf', 'q-loose', 'not json',"
            " '2026-09-15 12:00:00')"
        )
        conn.commit()
    finally:
        conn.close()

    answer = retrieval_log.query_retrievals()
    loose = answer["rows"][0]
    assert loose["query"] == "q-loose"
    assert loose["sources"] == []  # malformed JSON yields [], not an error
    assert loose["result_count"] == 0
    assert answer["total"] == 5
    # every column is present on a decoded row
    assert {"id", "provider", "scope", "query", "result_count", "sources",
            "retrieved_token_count", "retrieval_duration_ms", "agent_role",
            "run_id", "handoff_id", "created_at", "flow_key"} <= set(loose)


# ── summarise_retrievals ─────────────────────────────────────────────────


def test_summary_counts_totals_per_scope_and_role(tmp_path, monkeypatch):
    seed(tmp_path, monkeypatch, standard_rows())

    summary = retrieval_log.summarise_retrievals()
    assert summary["retrievals"] == 4
    assert summary["results"] == 10
    assert summary["tokens"] == 100
    assert summary["duration_ms"] == 32
    assert summary["scopes"] == {"dpmtf": 3, "other": 1}
    assert summary["agent_roles"] == {"dsh": 3, "supervisor": 1}
    assert summary["first"] == "2026-09-15 08:00:00"
    assert summary["last"] == "2026-09-15 11:00:00"

    one_run = retrieval_log.summarise_retrievals(run_id="046")
    assert one_run["retrievals"] == 3
    assert one_run["scopes"] == {"dpmtf": 2, "other": 1}
    assert one_run["tokens"] == 70

    today = retrieval_log.summarise_retrievals(since="2026-09-15T00:00:00")
    assert today["retrievals"] == 4
    tomorrow = retrieval_log.summarise_retrievals(since="2026-09-16T00:00:00")
    assert tomorrow["retrievals"] == 0
    assert tomorrow["scopes"] == {} and tomorrow["first"] is None


# ── route ────────────────────────────────────────────────────────────────


def test_retrievals_route_answers_rows_and_summary_and_400s_on_bad_paging(
    tmp_path, monkeypatch
):
    seed(tmp_path, monkeypatch, standard_rows())
    client = TestClient(create_app())

    answer = client.get("/v1/retrievals")
    assert answer.status_code == 200
    body = answer.json()
    assert body["total"] == 4 and len(body["rows"]) == 4
    assert body["rows"][0]["query"] == "q-four"
    assert body["rows"][0]["sources"] == []

    filtered = client.get("/v1/retrievals?run_id=046&limit=2").json()
    assert filtered["total"] == 3 and len(filtered["rows"]) == 2
    assert filtered["limit"] == 2

    summary = client.get("/v1/retrievals?summary=true").json()
    assert summary["retrievals"] == 4
    assert summary["scopes"] == {"dpmtf": 3, "other": 1}

    bad_limit = client.get("/v1/retrievals?limit=many")
    assert bad_limit.status_code == 400
    assert "limit" in bad_limit.json()["detail"]
    bad_until = client.get("/v1/retrievals?until=soon")
    assert bad_until.status_code == 400
    assert "until" in bad_until.json()["detail"]
    # negative offsets are clamped, not refused
    clamped = client.get("/v1/retrievals?offset=-2").json()
    assert clamped["offset"] == 0


# ── empty state ──────────────────────────────────────────────────────────


def test_query_on_a_missing_database_is_the_empty_state(tmp_path, monkeypatch):
    write_ini(tmp_path, monkeypatch, db_name="gone/knowledge.db")

    answer = retrieval_log.query_retrievals()
    assert answer == {"rows": [], "total": 0, "limit": 50, "offset": 0}

    summary = retrieval_log.summarise_retrievals(run_id="046")
    assert summary["retrievals"] == 0
    assert summary["scopes"] == {} and summary["agent_roles"] == {}
    assert summary["first"] is None and summary["last"] is None

    assert not (tmp_path / "gone").exists()  # reading never creates the file


# ── CLI ──────────────────────────────────────────────────────────────────


def test_cli_retrievals_prints_lines_and_summary(tmp_path, monkeypatch, capsys):
    rows = standard_rows()
    rows[3]["query"] = "q-four " + "y" * 70  # long query gets truncated
    seed(tmp_path, monkeypatch, rows)

    assert cli.main(["retrievals"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 4
    fields = lines[0].split("\t")
    assert fields[:6] == [
        "2026-09-15 11:00:00", "dpmtf", "dsh", "", "046", "H4",
    ]
    assert fields[6:9] == ["4", "40", "11"]
    assert fields[9] == ("q-four " + "y" * 70)[:60]

    assert cli.main(["retrievals", "--run-id", "046", "--limit", "2"]) == 0
    assert len(capsys.readouterr().out.strip().splitlines()) == 2

    assert cli.main(["retrievals", "--summary"]) == 0
    out = capsys.readouterr().out
    assert "retrievals" in out and "duration_ms" in out
    summary_lines = [line for line in out.splitlines() if line.startswith("scopes")]
    assert '"dpmtf": 3' in summary_lines[0]

    assert cli.main(["retrievals", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total"] == 4 and len(payload["rows"]) == 4
    assert payload["rows"][0]["query"].startswith("q-four")
