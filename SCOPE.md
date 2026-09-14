# SCOPE — knowledge-service 3A-1b: the retrieval log records the caller's flow key

Treat this file as the complete project scope for this workspace
(`/home/svend/knowledge-service`). It follows 3A-1 (commit cc3c275, reviewed
and pushed). Re-initialise scope-mcp for this scope (`init_project` with
`reset: true`), ask only what is genuinely necessary, then build.

## 1. Purpose

DPMtF's run 034 (2026-09-14, commit 876d8c7) added a nullable `flow_key`
column to `knowledge_retrieval_log` and writes the caller's flow key on
every retrieval, so DeepSeek Harness usage is auditable per workspace. The
service's schema (3A-1) predates that change: parity with DPMtF is broken
by exactly that one column, and `/v1/search` accepts `flow_key` but does not
store it. Close the gap.

## 2. Deliverable

1. `knowledge_service/db.py`: `SCHEMA_SQL` creates `knowledge_retrieval_log`
   with `flow_key TEXT DEFAULT NULL` after `handoff_id`; `ensure_schema`
   also upgrades an existing database that lacks the column
   (`ALTER TABLE ... ADD COLUMN`, detected with `pragma table_info`), so a
   3A-1 database keeps working and keeps its rows.
2. `knowledge_service/retrieval_log.py::record_retrieval` gains keyword
   `flow_key: str | None = None` and writes it.
3. `/v1/search` passes its `flow_key` query parameter to the log row.
4. `import-registry` copies `flow_key` when the source table has it and
   leaves it NULL otherwise (DPMtF databases from before run 034).
5. Tests, named exactly, in `tests/test_knowledge_service.py`:
   `test_schema_has_flow_key_and_upgrades_an_older_database`,
   `test_search_route_records_the_flow_key`,
   `test_import_registry_copies_flow_key_when_present`.
6. README: the retrieval-log field list mentions `flow_key`.

## 3. Constraints

As in 3A-1: work only here; never modify DPMtF, mcp-light or the shared
index directory; no commits; interpreter `/home/svend/DPMtF-WebUI/venv/bin/python`;
no new dependencies; parameterized SQL; en-US; no services started, no
models touched; `py_compile` every changed file; the thirteen 3A-1 tests
stay green.

## 4. Definition of Done

Testgoals green when the reviewer measures them; `git status` shows only
`knowledge_service/db.py`, `knowledge_service/retrieval_log.py`,
`knowledge_service/app.py`, `knowledge_service/cli.py` (if the importer
lives there), `tests/test_knowledge_service.py`, `README.md`; coverage
recorded; `complete_project` called.

```testgoals
id: TG1
what: the three named tests exist and pass and the 3A-1 tests stay green
run: cd /home/svend/knowledge-service && PYTHONDONTWRITEBYTECODE=1 /home/svend/DPMtF-WebUI/venv/bin/python -m pytest -q -p no:cacheprovider tests -k "schema_has_flow_key_and_upgrades_an_older_database or search_route_records_the_flow_key or import_registry_copies_flow_key_when_present" 2>&1 | tail -n 1 | grep -E "^3 passed" && PYTHONDONTWRITEBYTECODE=1 /home/svend/DPMtF-WebUI/venv/bin/python -m pytest -q -p no:cacheprovider tests 2>&1 | tail -n 1 | grep -E "^16 passed"
expect: exit 0

id: TG2
what: a fresh service database has the column and an older one is upgraded in place
run: cd /home/svend/knowledge-service && /home/svend/DPMtF-WebUI/venv/bin/python -c "import sqlite3, tempfile, os, sys; sys.path.insert(0, '.'); from knowledge_service import db; d = tempfile.mkdtemp(); c = sqlite3.connect(os.path.join(d, 'new.db')); db.ensure_schema(c); cols = [r[1] for r in c.execute('pragma table_info(knowledge_retrieval_log)')]; assert 'flow_key' in cols, cols; o = sqlite3.connect(os.path.join(d, 'old.db')); o.executescript('create table knowledge_retrieval_log (id integer primary key autoincrement, provider text not null, scope text not null, query text not null, result_count integer not null default 0, sources text not null default \"[]\", token_count integer not null default 0, duration_ms integer not null default 0, agent_role text, run_id text, handoff_id text, created_at text not null default current_timestamp)'); o.execute(\"insert into knowledge_retrieval_log (provider, scope, query) values ('p','s','q')\"); o.commit(); db.ensure_schema(o); cols2 = [r[1] for r in o.execute('pragma table_info(knowledge_retrieval_log)')]; n = o.execute('select count(*) from knowledge_retrieval_log').fetchone()[0]; raise SystemExit(0 if 'flow_key' in cols2 and n == 1 else 1)"
expect: exit 0

id: TG3
what: schema parity with the live DPMtF database on all four tables
run: cd /home/svend/knowledge-service && /home/svend/DPMtF-WebUI/venv/bin/python -c "import sqlite3, tempfile, os, sys; sys.path.insert(0, '.'); from knowledge_service import db; d = tempfile.mkdtemp(); c = sqlite3.connect(os.path.join(d, 'k.db')); db.ensure_schema(c); t = sqlite3.connect('file:/home/svend/DPMtF-WebUI/databases/dpmtf.db?mode=ro', uri=True); bad = [n for n in ('knowledge_indexes','knowledge_scope_grants','knowledge_retrieval_log','knowledge_exclusions') if [r[1] for r in c.execute(f'pragma table_info({n})')] != [r[1] for r in t.execute(f'pragma table_info({n})')]]; print(bad); raise SystemExit(0 if not bad else 1)"
expect: exit 0

id: TG4
what: FENCE — only the named files changed
run: cd /home/svend/knowledge-service && test -n "$(git status --porcelain)" && test -z "$(git status --porcelain | awk '{print $2}' | grep -v -E '^(knowledge_service/db.py|knowledge_service/retrieval_log.py|knowledge_service/app.py|knowledge_service/cli.py|tests/test_knowledge_service.py|README.md)$')"
expect: exit 0
```

## 5. Initial Execution Instruction

`init_project` with `reset: true`; goals for 2.1–2.6; implement; run TG1–TG3
and `py_compile`; record coverage; `complete_project`; report `git status`
and the pasted output of TG1–TG3.
