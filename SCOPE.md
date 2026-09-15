# SCOPE — knowledge-service A2-6: the retrieval log is readable

Treat this file as the complete project scope for this workspace
(`/home/svend/knowledge-service`). Re-initialise scope-mcp (`init_project`
with `reset: true`), ask only what is genuinely necessary, then build. The
service is live on `http://127.0.0.1:9140` from commit `92bbbd3`; its
`knowledge_retrieval_log` holds 641 rows and nothing can read them.

## 1. Purpose

Every search writes one row (`provider`, `scope`, `query`, `result_count`,
`sources` JSON, `retrieved_token_count`, `retrieval_duration_ms`,
`agent_role`, `run_id`, `handoff_id`, `created_at`, `flow_key`), but the
service exposes no way to read them: `GET /v1/scopes` lists the registry,
`/v1/learning` the artifacts, and the log is reachable only with `sqlite3`
on the host. Addendum 2 §7 promises the operator can "list what a given run
retrieved", and the criterion-7 measurement of 2026-09-15 went looking for
exactly that and found DPMtF's local log instead, which is empty when a
role retrieves through mcp-light. Make the log readable, with the same
read-only, parameterized discipline as the rest of the service.

## 2. Deliverable

### 2.1 Query function (`knowledge_service/retrieval_log.py`)

`query_retrievals(*, run_id=None, handoff_id=None, flow_key=None, agent_role=None, scope=None, since=None, until=None, limit=50, offset=0) -> dict`

- Every filter is optional and combines with AND; `since`/`until` are
  ISO-8601 strings compared against `created_at`; unknown/None filters are
  simply absent from the WHERE clause. Parameterized SQL only.
- `limit` is clamped to 1..500 (default 50), `offset` to >= 0.
- Returns `{"rows": [...], "total": <int>, "limit": n, "offset": n}` where
  `total` is the count matching the filters before the limit, and each row
  carries every column with `sources` decoded from JSON into a list (a row
  whose `sources` does not parse yields `[]` and is not an error).
- Newest first (`created_at DESC, id DESC`).
- Read-only: it opens the configured database read-only and never creates
  it; a missing database or table is the empty state
  (`{"rows": [], "total": 0, ...}`), never an exception.

`summarise_retrievals(*, run_id=None, flow_key=None, ...same filters...) -> dict`
returns `{"retrievals": n, "results": n, "tokens": n, "duration_ms": n,
"scopes": {scope: n}, "agent_roles": {role: n}, "first": iso|None,
"last": iso|None}` over the matching rows (no limit).

### 2.2 Route (`knowledge_service/app.py`)

`GET /v1/retrievals` with the same query parameters plus `summary=true`
(default false): `summary=false` answers `query_retrievals`'s dict,
`summary=true` answers `summarise_retrievals`'s dict. Read-only, no scope
guard (the log is operator data, not passage content), the token header
applies as on every other route. Invalid `limit`/`offset`/timestamps are a
400 naming the parameter.

### 2.3 CLI (`knowledge_service/cli.py`)

`retrievals [--run-id X] [--handoff-id X] [--flow-key X] [--agent-role X]
[--scope X] [--since ISO] [--until ISO] [--limit N] [--offset N]
[--summary] [--json]`: tab-separated lines
(`created_at, scope, agent_role, flow_key, run_id, handoff_id,
result_count, retrieved_token_count, retrieval_duration_ms, query`
truncated to 60 characters) or, with `--summary`, the summary as aligned
`key value` lines; `--json` prints the function's dict verbatim.

### 2.4 README

`## Retrieval log`: what a row holds, the route with its parameters, the
CLI with one example per question an operator actually asks ("what did
this run retrieve", "what has this role retrieved today", "how much did
scope X serve this week").

### 2.5 Tests, named exactly (in `tests/test_knowledge_service.py` or a new `tests/test_retrieval_log_query.py`)

- `test_query_filters_combine_and_page_newest_first`
- `test_query_decodes_sources_and_survives_a_malformed_row`
- `test_summary_counts_totals_per_scope_and_role`
- `test_retrievals_route_answers_rows_and_summary_and_400s_on_bad_paging`
- `test_query_on_a_missing_database_is_the_empty_state`
- `test_cli_retrievals_prints_lines_and_summary`

Every existing test stays green.

## 3. Constraints

As before: work only here; never modify DPMtF, mcp-light, the shared index
directory or `~/.local/share/knowledge-service/`; no commits; no network;
interpreter `/home/svend/DPMtF-WebUI/venv/bin/python`; no new dependencies;
parameterized SQL; en-US; no services or models touched; `py_compile` every
changed file; the 71 existing tests stay green. The live service on 9140
keeps running and is not restarted by you. Tests use temp databases only —
never the operator's.

## 4. Definition of Done

```testgoals
id: TG1
what: the six named tests exist and pass and the whole suite is green
run: cd /home/svend/knowledge-service && for t in query_filters_combine_and_page_newest_first query_decodes_sources_and_survives_a_malformed_row summary_counts_totals_per_scope_and_role retrievals_route_answers_rows_and_summary_and_400s_on_bad_paging query_on_a_missing_database_is_the_empty_state cli_retrievals_prints_lines_and_summary; do grep -rq "def test_$t" tests/ || exit 1; done && PYTHONDONTWRITEBYTECODE=1 /home/svend/DPMtF-WebUI/venv/bin/python -m pytest -q -p no:cacheprovider tests 2>&1 | tail -n 1 | grep -E "passed" | grep -vE "failed|error"
expect: exit 0

id: TG2
what: the functions, the route and the CLI verb exist and the README documents them
run: cd /home/svend/knowledge-service && grep -q "def query_retrievals" knowledge_service/retrieval_log.py && grep -q "def summarise_retrievals" knowledge_service/retrieval_log.py && grep -q "/v1/retrievals" knowledge_service/app.py && grep -q '"retrievals"' knowledge_service/cli.py && grep -q "^## Retrieval log" README.md
expect: exit 0

id: TG3
what: FENCE — only the deliverable paths changed
run: cd /home/svend/knowledge-service && test -n "$(git status --porcelain)" && test -z "$(git status --porcelain | awk '{print $2}' | grep -v -E '^(knowledge_service/|tests/|README.md)')"
expect: exit 0

id: TG4
what: LIVE (reviewer only) — after the restart, the route and the CLI answer over the operator's 641 rows and the run-046 sessions' retrievals are findable
run: test -f /tmp/claude-1000/-home-svend-DPMtF-WebUI/e20394ae-27d0-4204-804f-5d6a2f5da054/scratchpad/a2-6-live/ok
expect: exit 0
```

## 5. Initial Execution Instruction

`init_project` with `reset: true`; goals for 2.1–2.5 in order; checkpoint
after each goal; ask now, in one message, only what is genuinely ambiguous;
implement; run TG1–TG3 and `py_compile`; record coverage;
`complete_project`; report `git status` and the pasted output of TG1–TG3.
TG4 is the reviewer's after the service restart; do not attempt it and do
not restart anything.
