# SCOPE — knowledge-service 3A-1c: the test suite never touches the configured database

Treat this file as the complete project scope for this workspace
(`/home/svend/knowledge-service`). Re-initialise scope-mcp
(`init_project` with `reset: true`), ask only what is genuinely necessary,
then build.

## 1. Purpose

Measured 2026-09-14 at first install: the live service database
(`~/.local/share/knowledge-service/knowledge.db`) contained registry rows
`alpha` (changed, 3, location `/tmp/pytest-of-svend/.../alpha`) and `beta`
(missing, 0) — written by
`tests/test_knowledge_service.py::test_scopes_route_lists_registry_rows`
through `maintenance.record_index(...)`, although the fixture sets
`KNOWLEDGE_SERVICE_INI` to a temporary ini with a temporary `db_path`. So at
least one code path resolves the database without honouring the ini the
test set (a cached parser, an import-time default, or a module that reads
the path before the environment is patched). A test suite that can write
into the operator's database is a defect, whatever the path.

## 2. Deliverable

1. Find the path: which call in that test (and in every other test) reaches
   a database file outside `tmp_path`. Fix the cause in the package
   (typically: `config` must re-read `KNOWLEDGE_SERVICE_INI` on every call
   or expose an explicit reload; `db.connect()` must take the path from
   config at call time, never at import time). Do not fix it by patching the
   test alone.
2. A guard fixture in `tests/conftest.py` (autouse) that points
   `KNOWLEDGE_SERVICE_INI` at a temporary ini with a temporary `db_path`
   and `index_dir` for EVERY test, and asserts at teardown that no file
   under `~/.local/share/knowledge-service/` and no file under the shared
   index directory changed (mtime and size snapshot before/after). A test
   that trips it fails with the offending path in the message.
3. Tests, named exactly: `test_registry_rows_land_in_the_test_database_only`,
   `test_config_rereads_the_ini_env_on_every_call`,
   `test_guard_fixture_detects_a_write_outside_tmp` (the last one exercises
   the guard against a deliberate write to a temp copy of the operator
   layout, never the real one).
4. README: one paragraph "Running the tests" stating the isolation
   guarantee.

## 3. Constraints

As before: work only here; never modify DPMtF, mcp-light, the shared index
directory or `~/.local/share/knowledge-service/`; no commits; interpreter
`/home/svend/DPMtF-WebUI/venv/bin/python`; no new dependencies; en-US; no
services or models touched; `py_compile` every changed file; the sixteen
existing tests stay green. The live service on 9140 keeps running; do not
stop it.

## 4. Definition of Done

Testgoals green when the reviewer measures them; `git status` limited to
`knowledge_service/`, `tests/`, `README.md`; coverage recorded;
`complete_project` called.

```testgoals
id: TG1
what: the three named tests exist and pass and the whole suite is green
run: cd /home/svend/knowledge-service && PYTHONDONTWRITEBYTECODE=1 /home/svend/DPMtF-WebUI/venv/bin/python -m pytest -q -p no:cacheprovider tests -k "registry_rows_land_in_the_test_database_only or config_rereads_the_ini_env_on_every_call or guard_fixture_detects_a_write_outside_tmp" 2>&1 | tail -n 1 | grep -E "^3 passed" && PYTHONDONTWRITEBYTECODE=1 /home/svend/DPMtF-WebUI/venv/bin/python -m pytest -q -p no:cacheprovider tests 2>&1 | tail -n 1 | grep -E "^19 passed"
expect: exit 0

id: TG2
what: running the whole suite leaves the operator database and the shared index directory byte-identical
run: cd /home/svend/knowledge-service && a="$(stat -c '%s %Y' /home/svend/.local/share/knowledge-service/knowledge.db)" && b="$(ls -l --time-style=+%s /home/svend/.local/share/dpmtf/knowledge_index | md5sum)" && PYTHONDONTWRITEBYTECODE=1 /home/svend/DPMtF-WebUI/venv/bin/python -m pytest -q -p no:cacheprovider tests >/dev/null 2>&1; test "$a" = "$(stat -c '%s %Y' /home/svend/.local/share/knowledge-service/knowledge.db)" && test "$b" = "$(ls -l --time-style=+%s /home/svend/.local/share/dpmtf/knowledge_index | md5sum)" && test "$(sqlite3 /home/svend/.local/share/knowledge-service/knowledge.db "select count(*) from knowledge_indexes where scope in ('alpha','beta')")" = "0"
expect: exit 0

id: TG3
what: the guard fixture exists and is autouse
run: cd /home/svend/knowledge-service && test -f tests/conftest.py && grep -q "autouse=True" tests/conftest.py && grep -q "KNOWLEDGE_SERVICE_INI" tests/conftest.py
expect: exit 0

id: TG4
what: FENCE — only package, tests and README changed
run: cd /home/svend/knowledge-service && test -n "$(git status --porcelain)" && test -z "$(git status --porcelain | awk '{print $2}' | grep -v -E '^(knowledge_service/|tests/|README.md)')"
expect: exit 0
```

## 5. Initial Execution Instruction

`init_project` with `reset: true`; goals for 2.1–2.4; find the cause before
writing the guard; run TG1–TG4 (TG2 is safe to run: it only reads the
operator files); record coverage; `complete_project`; report `git status`
and the pasted output of TG1–TG4.
