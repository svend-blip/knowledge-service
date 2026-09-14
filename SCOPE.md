# SCOPE — knowledge-service A2-2: scope error contract, passage metadata in results, learning listing

Treat this file as the complete project scope for this workspace
(`/home/svend/knowledge-service`). Re-initialise scope-mcp (`init_project`
with `reset: true`), ask only what is genuinely necessary, then build. The
service is live on `http://127.0.0.1:9140` from commit `74c6ecd`; A2-1
(validated learning artifacts) is closed and reviewed. This scope closes
three gaps the review measured live.

## 1. Purpose

1. A search on a scope the service does not know leaks a provider
   `FileNotFoundError` as HTTP 500 (measured: `scope=no-such-scope-xyz`).
   Clients need a contract, not a traceback.
2. Search results carry only `path`, `content`, `score`, `scope`; the
   evidence level, origin run and supersession state that A2-1 stores in
   passage metadata never reach a caller, so a consumer cannot show why a
   learning passage is trustworthy or where it came from.
3. Admitted artifacts are listable only through the CLI on the host; a
   client on another machine (DPMtF's supervisor, a harness) has no
   read-only view.

## 2. Deliverable

### 2.1 Scope error contract (`knowledge_service/app.py`)

On `GET /v1/search`, after the scope guard (a denied scope stays 403, so an
unauthorised caller learns nothing about scope existence) and after the
disabled envelope, and before any provider is constructed:

- A scope with no `knowledge_indexes` row that is not one of the three
  learning scopes → **404** `{"detail": "unknown scope: <scope>"}`, no
  retrieval-log row.
- A scope whose row exists but whose store files are absent under
  `index_dir` (for either provider — ask the provider class through one
  new classmethod `store_exists(scope) -> bool`, implemented by both
  providers) → **503** `{"detail": "store missing for scope <scope>; refresh it"}`,
  no retrieval-log row. The learning-scope empty case from A2-1 keeps its
  200-empty behaviour and takes precedence for those three scopes.
- Any `OSError` (including `FileNotFoundError`) raised by
  `provider.search` → **503** with the exception text as `detail`; never a
  500. `ProviderNotReady` stays 503 as today.

`/v1/scopes` and `/v1/refresh` are unchanged.

### 2.2 Passage metadata in results (`leann_provider.py`, `portable_provider.py`, `app.py`)

Every result dict gains `"metadata": {...}` — the passage's stored extra
metadata with the identity keys `id`, `path`, `scope` removed. For
repository scopes that is `{}`. For the learning scopes it is what A2-1
stores: `evidence_level`, `repository`, `family`, `run`, `confidence`, and
`origin` for ecosystem passages; for history passages additionally
`superseded_by` or `retracted_at` when the history manifest carries them
(`learning.py`'s history records must include those two fields in their
metadata when present in the artifact — add them there). Both providers
return the same shape; `/v1/search` passes it through unchanged. The token
budget logic is untouched (it measures `content` only).

### 2.3 Learning listing route (`app.py`, `learning.py`)

`GET /v1/learning` → `{"artifacts": [...]}`, one object per admitted
artifact: `family`, `run`, `topic`, `evidence_level`, `confidence`,
`admitted_by`, `supersedes` (list). `GET /v1/learning?history=true` lists
the history directory instead, each object additionally carrying
`superseded_by` (string or null) and `retracted_at` (string or null).
Sorted by `family`, then `run`. Read-only; no scope guard (the learning
scopes are public, like the CLI listing). `learning.py` exposes the
listing as a pure function `list_artifact_records(history: bool = False) -> list[dict]`
that the CLI's `learning list` also uses (its printed lines stay
byte-identical).

### 2.4 Tests (hermetic, temp dirs, stub provider), named exactly

- `test_search_unknown_scope_is_404_without_a_log_row`
- `test_search_known_scope_with_missing_store_is_503`
- `test_provider_store_errors_never_leak_as_500` (a stub whose `search`
  raises `FileNotFoundError` → 503 with the message as detail)
- `test_search_results_carry_passage_metadata` (an experience passage
  returns `evidence_level`, `family`, `run`; a repository passage returns
  `{}`; identity keys are not repeated inside `metadata`)
- `test_portable_provider_results_carry_metadata` (the portable store with
  the fake embedder, same assertions)
- `test_learning_route_lists_admitted_and_history` (after one admit and
  one supersede in a temp learning dir: live list has the newer artifact,
  history list has the older with `superseded_by`)

Every existing test stays green; the stub provider gains `store_exists`.

### 2.5 README

`## HTTP contract` (or the nearest existing section): the 403/404/503
order for `/v1/search`, the `metadata` field with its keys per scope kind,
and `GET /v1/learning[?history=true]`.

## 3. Constraints

As before: work only here; never modify DPMtF, mcp-light, the shared
index directory or `~/.local/share/knowledge-service/`; no commits; no
network; interpreter `/home/svend/DPMtF-WebUI/venv/bin/python`; no new
dependencies; parameterized SQL; en-US; no services or models touched;
`py_compile` every changed file; the 40 existing tests stay green. The
live service on 9140 keeps running and is not restarted by you.

## 4. Definition of Done

Testgoals green when the reviewer measures them; `git status` limited to
`knowledge_service/`, `tests/`, `README.md`; coverage recorded;
`complete_project` called.

```testgoals
id: TG1
what: the six named tests exist and pass and the whole suite is green
run: cd /home/svend/knowledge-service && for t in search_unknown_scope_is_404_without_a_log_row search_known_scope_with_missing_store_is_503 provider_store_errors_never_leak_as_500 search_results_carry_passage_metadata portable_provider_results_carry_metadata learning_route_lists_admitted_and_history; do grep -q "def test_$t" tests/*.py || exit 1; done && PYTHONDONTWRITEBYTECODE=1 /home/svend/DPMtF-WebUI/venv/bin/python -m pytest -q -p no:cacheprovider tests 2>&1 | tail -n 1 | grep -E "passed" | grep -vE "failed|error"
expect: exit 0

id: TG2
what: the contract is wired — 404 detail, store_exists on both providers, metadata in both providers, the listing route and function
run: cd /home/svend/knowledge-service && grep -q "unknown scope" knowledge_service/app.py && grep -q "def store_exists" knowledge_service/leann_provider.py && grep -q "def store_exists" knowledge_service/portable_provider.py && grep -q '"metadata"' knowledge_service/leann_provider.py && grep -q '"metadata"' knowledge_service/portable_provider.py && grep -q "/v1/learning" knowledge_service/app.py && grep -q "def list_artifact_records" knowledge_service/learning.py
expect: exit 0

id: TG3
what: FENCE — only the deliverable paths changed
run: cd /home/svend/knowledge-service && test -n "$(git status --porcelain)" && test -z "$(git status --porcelain | awk '{print $2}' | grep -v -E '^(knowledge_service/|tests/|README.md)')"
expect: exit 0

id: TG4
what: LIVE (reviewer only) — on 9140 an unknown scope answers 404, a known scope answers 200 with metadata objects, and /v1/learning answers
run: cd /home/svend/knowledge-service && test -f /tmp/claude-1000/-home-svend-DPMtF-WebUI/e20394ae-27d0-4204-804f-5d6a2f5da054/scratchpad/a2-2-live/ok
expect: exit 0
```

## 5. Initial Execution Instruction

`init_project` with `reset: true`; goals for 2.1–2.5 in order; checkpoint
after each goal; ask now, in one message, only what is genuinely ambiguous;
implement; run TG1–TG3 and `py_compile`; record coverage;
`complete_project`; report `git status` and the pasted output of TG1–TG3.
TG4 is the reviewer's live measurement after the reviewer restarts the
service; do not attempt it and do not restart anything.
