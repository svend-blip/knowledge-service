# SCOPE — knowledge-service, part 3A-1: the knowledge layer as a standalone service

Treat this file as the complete project scope for this workspace
(`/home/svend/knowledge-service`, a new repository). Persist nothing outside
this workspace except scope-mcp's own state. Ask only the clarification
questions that are genuinely necessary, then build.

## 1. Purpose

DPMtF's knowledge layer (package `knowledge/` in `/home/svend/DPMtF-WebUI`,
about 2 000 lines, plus nine `get_knowledge_*` getters in its `config.py`
and two routes in `routers/knowledge.py`) becomes a standalone service with
its own HTTP contract, so that DPMtF, mcp-light, simple-harness, FlowRunner,
FlowApps and DeepSeek Harness all consume the same provider-neutral API.
This is part 3A-1 of `docs/SCOPE-ADDENDUM-KNOWLEDGE-3-HARNESSES.md` in
DPMtF (read Part A and the "Platform independence" section there first;
that file is read-only for you). Switching DPMtF itself over to the service
is a later, separate task (3A-2) and is NOT part of this scope.

## 2. Deliverable

A Python package `knowledge_service/` and a FastAPI app, extracted from
DPMtF's `knowledge/` by copying and adapting — never by importing DPMtF.

### 2.1 Package

- `knowledge_service/provider.py` — `KnowledgeProvider` (index, update,
  remove, search, preflight), `ProviderNotReady`, `NoneProvider`: verbatim
  from DPMtF.
- `knowledge_service/leann_provider.py` — verbatim from DPMtF, with the one
  change that `config` is this package's own config module.
- `knowledge_service/indexer.py`, `maintenance.py` (incl. `refresh_scope`,
  `cap_content`, `detect_changes`), `scopes.py`, `scope_guard.py`,
  `retrieval_log.py`, `search.py` (`resolve_provider(name, scope)`) —
  adapted from DPMtF: every `import config` becomes
  `from knowledge_service import config`; every database access goes to
  this service's own database (2.3); nothing imports `bridge_lib` or any
  DPMtF module. `retrieval.py` (the prompt-block renderer) is NOT moved:
  it stays a DPMtF client concern.
- `knowledge_service/config.py` — reads `knowledge.ini` (path from env
  `KNOWLEDGE_SERVICE_INI`, default `~/.config/knowledge-service/knowledge.ini`,
  falling back to `./knowledge.ini`) with the same keys and defaults as
  DPMtF's `[knowledge]` section today (`enabled`, `provider`, `scope`,
  `top_k`, `max_context_tokens`, `max_document_chars`, `index_dir`,
  `min_free_vram_mib`, `leann_use_daemon`) plus a `[service]` section
  (`host` 127.0.0.1, `port` 9140, `db_path`, `token` empty = no auth,
  `father_root` = the path that maps to the default scope). Ship
  `knowledge.ini.example`.
- `knowledge_service/scopes.py::scope_for_path(path)` normalises BOTH
  Windows and POSIX paths: trailing `/` or `\\` stripped, the final
  directory name lowercased, drive-letter case ignored;
  `C:\\Projects\\FlowRunner\\` and `/home/svend/FlowRunner/` both give
  `flowrunner`; a path equal to `father_root` (resolved) gives the
  configured default scope.

### 2.2 HTTP contract (`knowledge_service/app.py`)

```text
GET  /v1/search          q, scope, top_k, token_budget, agent_role, flow_key, run_id, handoff_id
POST /v1/refresh         {"scope": ..., "repo_path": ...}
GET  /v1/scopes          [{scope, provider, status, document_count, indexed_at}]
GET  /v1/scope-for-path  ?path=...  -> {"scope": ...}
GET  /v1/health          {"status": "ok", "provider", "enabled", "preflight": {"ok", "detail"}}
```

Response shapes and status codes exactly as DPMtF's `/api/knowledge/search`
and `/api/knowledge/refresh` today (disabled envelope; 403 with `detail`
on a denied scope; 503 with `detail` on `ProviderNotReady`; 400 on bad
input). When `[service] token` is set, every route except `/v1/health`
requires header `X-Knowledge-Token`. Every `/v1/search` that reaches a
provider writes one retrieval-log row.

### 2.3 Database and one-time import

The service owns `knowledge.db` (SQLite, path from config) with the tables
`knowledge_indexes`, `knowledge_scope_grants`, `knowledge_retrieval_log`
and `knowledge_exclusions`, created by `knowledge_service/db.py` with the
same columns and constraints as DPMtF's migrations 107, 109 and 110 (read
them under `/home/svend/DPMtF-WebUI/scripts/db/`). A CLI
`python -m knowledge_service.cli import-registry --from <dpmtf.db>` copies
those four tables' rows once (idempotent; `INSERT OR IGNORE`). Grants are
then owned by the service: `python -m knowledge_service.cli grant <scope>
<agent_role> [--flow <key>]` and `revoke`.

### 2.4 CLI

`python -m knowledge_service.cli` with `serve`, `refresh <scope> <repo_path>`,
`refresh-all --from-registry [--dry-run]` (every scope in
`knowledge_indexes` whose recorded repository path exists), `scopes`,
`grant`, `revoke`, `import-registry`. Clean errors, no tracebacks, exit 1
on failure.

### 2.5 Packaging

`requirements.txt` (fastapi, uvicorn, leann and its backends, torch —
the versions installed in `/home/svend/DPMtF-WebUI/venv`), `pyproject.toml`
with the package, `systemd/knowledge-service.service` (user unit,
`ExecStart=%h/knowledge-service/venv/bin/python -m knowledge_service.cli serve`),
and a README: what it is, the contract, config, install on Linux, the GPU
requirement and coexistence budget (copy the facts from DPMtF's
`docs/knowledge_indexing.md` "GPU requirement"), and a "Windows" section
that states honestly what is Linux-only today (LEANN's compiled backend,
nvidia-smi preflight, systemd) and that a `portable` provider is a later
part.

### 2.6 Tests (`tests/`, pytest)

Named exactly, all hermetic (stub provider, temp database, temp index dir,
`torch` stubbed where preflight is touched; no GPU, no live DPMtF):
`test_scope_for_path_normalises_windows_and_posix`,
`test_scope_for_path_maps_father_root_to_default_scope`,
`test_search_route_disabled_envelope`,
`test_search_route_denied_is_403_with_detail`,
`test_search_route_not_ready_is_503`,
`test_search_route_writes_one_log_row`,
`test_refresh_route_noop_touches_nothing`,
`test_scopes_route_lists_registry_rows`,
`test_health_route_reports_provider_and_preflight`,
`test_token_header_guards_every_route_except_health`,
`test_import_registry_is_idempotent`,
`test_cli_grant_and_revoke`,
`test_package_never_imports_dpmtf` (asserts no module under
`knowledge_service/` imports `config` at top level from outside the
package, `bridge_lib`, or anything under `/home/svend/DPMtF-WebUI`).

## 3. Constraints

- Read DPMtF's `knowledge/`, `config.py` getters, `routers/knowledge.py`,
  `scripts/db/107*`, `109*`, `110*` and `docs/knowledge_indexing.md` as
  much as you need. Never modify anything under `/home/svend/DPMtF-WebUI`,
  `/home/svend/mcp-light` or the shared index directory
  `~/.local/share/dpmtf/knowledge_index` (the live stores live there; the
  service reuses them read-only in this part — `index_dir` in the example
  ini points at it).
- Do not commit, stage or push. Do not start any service on a port; the
  reviewer runs the live testgoals. Do not start, stop or restart models.
- Interpreter for every check: `/home/svend/DPMtF-WebUI/venv/bin/python`
  (it has every dependency); do not create a venv here.
- No dependency beyond what that venv already has. `py_compile` every file.
- en-US everywhere. Parameterized SQL only. No hardcoded `/home/svend`
  paths in package code (paths come from config or arguments; tests may use
  temp dirs; the README may show example paths).

## 4. Definition of Done

Testgoals below green when the reviewer measures them; `git status` shows
only files under `knowledge_service/`, `tests/`, `systemd/`, plus
`README.md`, `requirements.txt`, `pyproject.toml`, `knowledge.ini.example`;
coverage recorded against 2.1–2.6; `complete_project` called.

```testgoals
id: TG1
what: the thirteen named tests exist and pass
run: cd /home/svend/knowledge-service && PYTHONDONTWRITEBYTECODE=1 /home/svend/DPMtF-WebUI/venv/bin/python -m pytest -q -p no:cacheprovider tests -k "scope_for_path_normalises_windows_and_posix or scope_for_path_maps_father_root_to_default_scope or search_route_disabled_envelope or search_route_denied_is_403_with_detail or search_route_not_ready_is_503 or search_route_writes_one_log_row or refresh_route_noop_touches_nothing or scopes_route_lists_registry_rows or health_route_reports_provider_and_preflight or token_header_guards_every_route_except_health or import_registry_is_idempotent or cli_grant_and_revoke or package_never_imports_dpmtf" 2>&1 | tail -n 1 | grep -E "^13 passed"
expect: exit 0

id: TG2
what: the package imports without DPMtF on the path and normalises both path styles
run: cd /home/svend/knowledge-service && /home/svend/DPMtF-WebUI/venv/bin/python -c "import sys; sys.path[:] = [p for p in sys.path if 'DPMtF-WebUI' not in p]; sys.path.insert(0, '.'); from knowledge_service import scopes, app, provider, leann_provider; assert scopes.scope_for_path('C:\\\\Projects\\\\FlowRunner\\\\') == 'flowrunner'; assert scopes.scope_for_path('/home/x/FlowRunner/') == 'flowrunner'; print('ok')"
expect: exit 0

id: TG3
what: no module in the package imports DPMtF
run: cd /home/svend/knowledge-service && test -d knowledge_service && test -f knowledge_service/app.py && ! grep -rn -E "^(import config|from config import|import bridge_lib|from bridge_lib|from knowledge import|import knowledge\.)" knowledge_service/ && ! grep -rn "DPMtF-WebUI" knowledge_service/
expect: exit 0

id: TG4
what: the registry import is idempotent against a copy of the live DPMtF database
run: cd /home/svend/knowledge-service && d="$(mktemp -d)" && cp /home/svend/DPMtF-WebUI/databases/dpmtf.db "$d/src.db" && KNOWLEDGE_SERVICE_INI="$d/k.ini" sh -c "printf '[knowledge]\nindex_dir = %s\n[service]\ndb_path = %s/knowledge.db\n' /home/svend/.local/share/dpmtf/knowledge_index $d > $d/k.ini" && KNOWLEDGE_SERVICE_INI="$d/k.ini" /home/svend/DPMtF-WebUI/venv/bin/python -m knowledge_service.cli import-registry --from "$d/src.db" >/dev/null && KNOWLEDGE_SERVICE_INI="$d/k.ini" /home/svend/DPMtF-WebUI/venv/bin/python -m knowledge_service.cli import-registry --from "$d/src.db" >/dev/null && test "$(sqlite3 $d/knowledge.db 'select count(*) from knowledge_indexes')" -ge 10 && test "$(sqlite3 $d/knowledge.db "select count(*) from knowledge_scope_grants where agent_role='dsh'")" = "1"
expect: exit 0

id: TG5
what: LIVE (reviewer only) — the service on port 9140 lists ten scopes, answers a search, normalises a Windows path and denies an internal scope without a grant
run: curl -s -m 10 http://127.0.0.1:9140/v1/scopes | /home/svend/DPMtF-WebUI/venv/bin/python -c "import sys, json; r = json.load(sys.stdin); raise SystemExit(0 if len(r) >= 10 else 1)" && curl -s -m 60 "http://127.0.0.1:9140/v1/search?q=How%20is%20a%20FlowApp%20exported&scope=flowrunner&agent_role=probe&top_k=2" | /home/svend/DPMtF-WebUI/venv/bin/python -c "import sys, json; r = json.load(sys.stdin); raise SystemExit(0 if r.get('enabled') and len(r.get('results', [])) >= 1 else 1)" && curl -s -m 10 "http://127.0.0.1:9140/v1/scope-for-path?path=C%3A%5CProjects%5CFlowRunner%5C" | grep -q '"flowrunner"' && test "$(curl -s -m 10 -o /dev/null -w '%{http_code}' "http://127.0.0.1:9140/v1/search?q=x&scope=dpmtf-webui&agent_role=nobody")" = "403"
expect: exit 0

id: TG6
what: FENCE — only the deliverable paths exist in this repository
run: cd /home/svend/knowledge-service && test -n "$(git status --porcelain)" && test -z "$(git status --porcelain | awk '{print $2}' | grep -v -E '^(knowledge_service/|tests/|systemd/|README.md|requirements.txt|pyproject.toml|knowledge.ini.example)')"
expect: exit 0
```

## 5. Initial Execution Instruction

1. `init_project` this scope in scope-mcp; derive goals for 2.1–2.6 in that
   order; checkpoint after each goal — this is larger than one context
   window.
2. Ask now, in one message, only what is genuinely ambiguous.
3. Extract, adapt, test; run TG1–TG4 and `py_compile`; record coverage;
   `complete_project`.
4. Report: `git status` and the pasted output of TG1–TG4. Do not run TG5.
