# knowledge-service

A standalone knowledge retrieval service: the knowledge layer that used to
live inside DPMtF, extracted into its own FastAPI application with its own
SQLite registry and its own command line. It serves the same `/v1` contract
over HTTP so any harness (DPMtF, a plain script, another agent runtime) can
ask for passages from indexed repositories without importing DPMtF.

Nothing in `knowledge_service/` imports DPMtF code. Configuration comes from
this package's own `config` module; provider access goes through the small
`KnowledgeProvider` interface, so LEANN is one interchangeable backend rather
than the centre of the design.

## HTTP contract

Base path `/v1`. When `[service] token` is set, every route except
`/v1/health` requires the header `X-Knowledge-Token`.

| Method | Path | Parameters | Purpose |
|---|---|---|---|
| GET | `/v1/search` | `q`, `scope`, `top_k`, `token_budget`, `agent_role`, `flow_key`, `run_id`, `handoff_id` | Search one scope, record the retrieval |
| POST | `/v1/refresh` | body `{"scope": ..., "repo_path": ...}` | Re-index a scope when its repository changed |
| GET | `/v1/scopes` | — | List registered scopes with document counts and repository paths |
| GET | `/v1/learning` | `history` | List admitted learning artifacts (`history=true` for the older ones) |
| GET | `/v1/scope-for-path` | `path` | Resolve a repository path to its scope slug |
| GET | `/v1/health` | — | Provider, enabled flag, preflight result |

Status codes on `/v1/search`, in order: `403` with `{"detail": ...}` when
the scope guard denies the caller's scope; `404` with `{"detail": ...}` on a
scope the registry does not know; `503` with `{"detail": ...}` when the
scope's store files are absent under the index dir or the provider is not
ready (no free GPU yet); `503` again when the provider's search raises an
`OSError`, with the exception text as detail — a store error never surfaces
as a 500. The three learning scopes keep their empty-history answer of
`200` with an empty result list and take precedence over the 404/503 path;
error answers write no retrieval-log row. Disabled installations still get
the `200` envelope `{"enabled", "provider", "results", "bounded"}`;
`400` covers bad input (`repo_path` is not a directory, empty scope,
manifest inside the repository). `top_k` and `token_budget` fall back to
the configured values and are clamped to them: a caller may lower them,
never raise them.

Every search result carries `path`, `content`, `score`, `scope` and
`metadata` — the passage's stored extras with the identity keys removed:
`{}` for repository passages; for learning passages `evidence_level`,
`repository`, `family`, `run` and `confidence`, plus `origin` for ecosystem
passages and `superseded_by`/`retracted_at` on history ones when present.
`GET /v1/learning` lists admitted artifacts read-only — one object per
artifact with `family`, `run`, `topic`, `evidence_level`, `confidence`,
`admitted_by` and `supersedes`, sorted by family then run — without the
scope guard; `history=true` lists the superseded and retracted ones with
their two extra fields.

## Configuration

Looked up in this order, first hit wins:

1. `$KNOWLEDGE_SERVICE_INI`
2. `~/.config/knowledge-service/knowledge.ini`
3. `./knowledge.ini`

A missing file is fine — every key has the default below. See
`knowledge.ini.example`.

| Key (`[knowledge]`) | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Master switch; `false` returns the disabled envelope |
| `provider` | `none` | `none`, `leann` or `portable`; unknown keys behave like `none` |
| `scope` | `dpmtf-webui` | Default scope of this installation |
| `top_k` | `8` | Result count fallback and ceiling |
| `max_context_tokens` | `12000` | Context budget fallback and ceiling |
| `max_document_chars` | `20000` | Characters kept per indexed document |
| `index_dir` | `~/.local/share/dpmtf/knowledge_index` | Shared manifest/index directory, outside every repository |
| `min_free_vram_mib` | `4096` | Free GPU memory required before a search or index run |
| `leann_use_daemon` | `false` | Daemon-free search; nothing stays resident |

| Key (`[service]`) | Default | Meaning |
|---|---|---|
| `host` / `port` | `127.0.0.1` / `9140` | Listening address |
| `db_path` | `~/.local/share/knowledge-service/knowledge.db` | This service's own database |
| `token` | *(empty)* | Shared `X-Knowledge-Token` value; empty means no auth |
| `father_root` | *(empty)* | Repository whose scope is the default scope |

Relative paths resolve under the home directory.

| Key (`[portable]`) | Default | Meaning |
|---|---|---|
| `model_dir` | `~/.local/share/knowledge-service/models/paraphrase-multilingual-MiniLM-L12-v2` | Directory holding `onnx/model.onnx` and `tokenizer.json` |
| `model_id` | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` | Model id recorded on every indexed passage |

## Portable provider

Measured 2026-09-14 by the reviewer on this host (CPU only,
`paraphrase-multilingual-MiniLM-L12-v2`, 470 MB ONNX): building the
`flowrunner` scope (656 passages) took 14.7 s, the store is 4.9 MB, and a
search answers in 0.05 s. For comparison the `leann` provider builds the
same scope in ~10 s on the GPU and searches in 8–9 s in-process (daemon-free)
or ~1 s with a warm daemon. Top-3 results differ between the two models but
both put the right file first on a concrete question
(`internal/runtimechild/runtimechild.go` for "How does the desktop start a
runtime child?").


Two providers sit behind the same `KnowledgeProvider` interface. `leann` needs
its compiled HNSW backend and a CUDA GPU. `portable` needs nothing but Python
and `onnxruntime`: passages, metadata and embeddings live in one SQLite file per
scope — `<index_dir>/<scope>.portable.db`, table `passages(id, scope, path,
content, embedding, dim, model)` — the embeddings come from a small multilingual
ONNX model running on the CPU, and a search is a cosine comparison of the query
vector against the stored ones. Same manifests, same scopes, same `/v1` routes,
same result shape (`path`, `content`, `score`, `scope`), same `top_k` default and
token-budget truncation rule; slower and simpler.

Choose `portable` where the GPU is not there or the compiled backend is not
available: a Windows FlowApp, a small Linux box, a container. Keep `leann` where
the GPU is resident and the throughput matters. Both stores coexist in one index
directory because the portable store has its own suffix.

```sh
# one-time, needs a network: fetch only the files the provider reads
./venv/bin/python -m knowledge_service.cli download-model
```

`download-model` prints the model directory and the size of each downloaded file
(`onnx/model.onnx`, `tokenizer.json`, `config.json`, `tokenizer_config.json`,
`special_tokens_map.json`), or one clean line and exit code 1 when there is no
network. Point at it from the ini with `provider = portable`;
`/v1/health` then reports the model directory state as
`"preflight": {"ok": true, "detail": "model present: yes"}`.

Measured numbers for the live run are filled in by the reviewer after TG6
(build time for the flowrunner manifest, first-search latency, store size).

| Provider | Runs on | Needs |
|---|---|---|
| `leann` | Linux | CUDA GPU plus the compiled LEANN backend |
| `portable` | Any platform with Python 3.12 and `onnxruntime` wheels (Linux, Windows, macOS) | CPU only |

## Install on Linux

```sh
cd ~/knowledge-service
python3 -m venv venv
./venv/bin/pip install -r requirements.txt

mkdir -p ~/.config/knowledge-service
cp knowledge.ini.example ~/.config/knowledge-service/knowledge.ini
# then edit: enabled, provider, index_dir, father_root, token
```

Bring the registry over once, then index what the registry names:

```sh
./venv/bin/python -m knowledge_service.cli import-registry \
    --from ~/DPMtF-WebUI/databases/dpmtf.db
./venv/bin/python -m knowledge_service.cli refresh flowrunner ~/Projects/FlowRunner
./venv/bin/python -m knowledge_service.cli refresh-all --from-registry
./venv/bin/python -m knowledge_service.cli scopes
```

`import-registry` copies `knowledge_indexes`, `knowledge_scope_grants`,
`knowledge_retrieval_log` and `knowledge_exclusions` once, keeping the
original ids, so re-running it changes nothing. Imported rows keep the location
they came with (the manifest path of their source registry) and leave
`repository_path` empty. Every `refresh` records the resolved repository
directory on the row's `repository_path`; `GET /v1/scopes` carries it, the
learning runs root hangs off it (`<repository_path>/.flowrunner`), and an
imported row gets the path through `set-repository <scope> <path>`.
`refresh-all --from-registry` refreshes rows whose `repository_path` is a
directory — or whose empty `repository_path` pairs with a directory
`location` — and skips the rest with a message naming the missing
`repository_path` and the command. Point `--from` at the live database file
itself (`~/DPMtF-WebUI/databases/dpmtf.db`) so its `dpmtf.db-wal` sibling is
read along with it; a copy of only the main file can miss the most recent
commits, including freshly recorded grants.

Every `/v1/search` that reaches a provider appends one
`knowledge_retrieval_log` row with `provider`, `scope`, `query`,
`result_count`, `sources`, `retrieved_token_count`,
`retrieval_duration_ms`, `agent_role`, `run_id`, `handoff_id`, `flow_key` and
`created_at`. `flow_key` is the caller's flow key (nullable): the same value
the scope guard matched, so retrievals are auditable per workspace. A database
created before that column existed gains it on the next start — `ensure_schema`
adds it in place and keeps the existing rows — and rows imported from such a
database simply have a NULL there.

Grant internal scopes to the roles that may read them (public scopes need no
grant). A database whose grant table is still empty starts with one baseline
grant — the configured default scope for this installation's supervisor role
(`dsh`), with no flow restriction — so a fresh installation can read its own
repository; from then on `grant` and `revoke` own the table:

```sh
./venv/bin/python -m knowledge_service.cli grant dpmtf-webui dsh
./venv/bin/python -m knowledge_service.cli grant dpmtf-webui builder --flow 9000-01-PLOOP
./venv/bin/python -m knowledge_service.cli revoke dpmtf-webui builder --flow 9000-01-PLOOP
```

Serve it (foreground) and from systemd:

```sh
./venv/bin/python -m knowledge_service.cli serve

mkdir -p ~/.config/systemd/user
cp systemd/knowledge-service.service systemd/knowledge-service-refresh.* \
   ~/.config/systemd/user/
systemctl --user enable --now knowledge-service.service
systemctl --user enable --now knowledge-service-refresh.timer
```

The service is a `simple` unit; the timer runs `refresh-all --from-registry`
daily, and both units are `Nice=10` so retrieval never competes with a local
model for the GPU.

## Retrieval log

Every real provider retrieval appends one row to `knowledge_retrieval_log`:
provider, scope, query, result count, JSON source list, retrieved token
count, duration in milliseconds, agent role, run id, handoff id, flow key,
and the `created_at` timestamp. `GET /v1/retrievals` reads those rows with
the same token header as every other route: the optional filters `run_id`,
`handoff_id`, `flow_key`, `agent_role`, `scope`, `since` and `until` combine
with AND, `limit` (default 50, capped at 500) and `offset` page the answer
newest-first, and `summary=true` returns totals instead of rows. A bad
`limit`, `offset` or timestamp is a 400 naming the parameter; a missing
database is the empty result, never an error. The CLI reads the same data
through `retrievals`:

```sh
# what did this run retrieve?
python -m knowledge_service.cli retrievals --run-id 046

# what has this role retrieved today?
python -m knowledge_service.cli retrievals --agent-role dsh \
    --since 2026-09-15T00:00:00

# how much did scope dpmtf-webui serve this week?
python -m knowledge_service.cli retrievals --scope dpmtf-webui \
    --since 2026-09-08T00:00:00 --summary
```

Rows print tab-separated (`created_at`, scope, role, flow, run, handoff,
result/token/duration counts, query truncated to 60 characters);
`--summary` prints aligned `key value` totals and `--json` the raw answer.

## Running the tests

```sh
PYTHONDONTWRITEBYTECODE=1 ./venv/bin/python -m pytest -q tests
```

Every test runs isolated from the operator's files. An autouse fixture in
`tests/conftest.py` points `KNOWLEDGE_SERVICE_INI` at a temporary INI whose
`db_path` and `index_dir` are inside pytest's own `tmp_path`, so registry rows,
grants and retrieval-log rows can only land in that temporary database — and
because the config loader re-reads that variable on every call, the value the
fixture set is what the code sees. At teardown the same fixture compares the
size and modification time of every file under
`~/.local/share/knowledge-service/` and under the shared index directory with
the snapshot taken before the test ran, and fails with the offending path in
the message when something changed there. A test that writes outside its
temporary directories therefore fails loudly instead of quietly leaving rows in
the live service database.

## GPU requirement

Copied from DPMtF's `docs/knowledge_indexing.md` ("GPU requirement"):

Retrieval still needs a free GPU: the LEANN call path does its embedding work
on the GPU at query time, not only during indexing. The default is
daemon-free (`leann_use_daemon = false`): no `hnsw_embedding_server` daemon is
spawned and nothing stays resident after the search. Measured 2026-09-14 on
the `dpmtf-webui` store (656 passages, free GPU):

| mode | per search | resident afterwards |
|---|---|---|
| daemon | 7.1 s cold, 1.0 s warm | daemon 1.6–2.2 GB, 900 s TTL |
| `use_daemon=False` | 8.3–9.3 s every time | nothing |

Same top-3 results on three queries. One retrieval per dispatch makes 9
seconds acceptable, so the daemon's failure modes are not worth the
warm-cache speedup. With the GPU held by a resident local model, a
649-document build did not finish in 10 minutes and searches against a warm
server aborted with `SIGABRT` on the compiled-in 30-second ZMQ timeout (that
timeout only applies with the daemon switched on).

## Coexistence budget

The service never evicts a resident model: `preflight()` compares free GPU
memory against `min_free_vram_mib` and refuses with a 503 when the margin is
too small, so a search costs one short GPU burst instead of pushing the local
model out. Measured on this machine with the local chat model resident
(25.8 GB held) plus a second model at ~2.7 GB: a LEANN search peaked at about
2.4 GB and finished with roughly 3.1 GB still free, so the practical gate is
**at least 2500 MiB free** before an index run or a search. The default
`min_free_vram_mib = 4096` keeps that margin; lower it only when the resident
footprint is known to be smaller. A refused retrieval is a normal outcome, not
an error to retry in a loop — the next dispatch tries again.

## Exclusions and content cap

Indexing reads plain text documents and skips: dotted names and the
directories in `_DEFAULT_EXCLUDED_NAMES` (vendored caches, build output),
binary and image suffixes, files whose first lines look like secrets
(`SECRET=`, `API_KEY=`, `PASSWORD=`, `TOKEN=`, a `PRIVATE KEY` header), and
patterns from `.knowledgeignore` at the repository root plus the
`knowledge_exclusions` rows of scopes registered for this repository. Each
document is stored at most `max_document_chars` long
(`knowledge_service/indexer.py`, `cap_content`). See DPMtF's
`docs/knowledge_indexing.md` for the same list with its measurement history.

## Scopes and path slugs

`GET /v1/scope-for-path` maps a repository path to its scope slug: trailing
separators are dropped, the final path component is lower-cased, and drive
letters are compared case-insensitively, so `C:\Projects\FlowRunner\`,
`C:/Projects/FlowRunner/` and `/home/svend/FlowRunner/` all give
`flowrunner`. A path equal to `father_root`, an empty path, or a bare `/`
returns the configured default scope. Non-internal scopes are always
reachable; internal ones (`dpmtf`, `dpmtf-*`) need a matching grant in
`knowledge_scope_grants`, where a `NULL` `flow_key` means any flow.

## Validated learning

Three stores sit beside the repository scopes and hold what closed runs
learned: `experience` (validated learning artifacts), `ecosystem` (the
architecture implications promoted out of them), and `experience-history`
(the superseded and retracted ones). They are built from manifests, not
from a repository scan, and every artifact is a YAML document under
`<learning_dir>/<repository>/<family>/<run>.yaml`. The identity is the
three-part key `<repository>/<family>/<run>`, where `<repository>` is the
artifact's repository normalised to the scope slug of `/v1/scope-for-path`
(`DPMtF-WebUI` → `dpmtf-webui`, `FlowRunner` → `flowrunner`), so two
repositories sharing a family number never overwrite each other. History
lives at `<learning_dir>/history/<repository>/<family>/<run>.yaml`. An
artifact still sitting at the legacy `<learning_dir>/<family>/<run>.yaml`
(or its history twin) is moved under its slug once — on `learning rebuild`
and on every admit — with one `migrated` ledger line each; manifests are
always rebuilt from the new layout. Every document carries `topic`,
`problem`, `approach`, `result`, `failed_approaches`, `important_files`
(repository-relative, forward slashes), `architecture_implications`,
`confidence`, and a `validation` block. Roles draft artifacts inside their
chains; the supervisor admits them — no model is involved in admission.

Evidence levels order by strength: `tests`, `measured_runtime`,
`approved_architecture`, `reviewer_conclusion`, `observation`
(`hypothesis` is never admitted). `GET /v1/search` for the `experience`
scope always filters through the provider's metadata filters with the
levels at least as strong as `evidence_level` (default
`approved_architecture`, so the three strongest levels pass); other scopes
ignore the parameter. With `include_history=true` the search targets
`experience-history` instead — otherwise history does not compete for the
budget.

```bash
# validate one artifact YAML (exit 1 on violations)
python -m knowledge_service.cli learning validate draft.yaml
# admit a closed SUCCESS run: writes the artifact, applies supersedes,
# rebuilds both scopes and their manifests
python -m knowledge_service.cli learning admit draft.yaml
# the same admission straight from the run directory, naming the admitter;
# refs are three-part — the legacy two-part form means the father repository
python -m knowledge_service.cli learning admit-run dpmtf-webui/2000/041 --admitted-by svend
python -m knowledge_service.cli learning admit-run 2000/041 --admitted-by svend
# one run draft's violations plus its END-REPORT status, without admitting
python -m knowledge_service.cli learning validate-run flowrunner/2000/041
python -m knowledge_service.cli learning drafts    # lists every registered repository
python -m knowledge_service.cli learning retract dpmtf-webui/2000/029
python -m knowledge_service.cli learning list
python -m knowledge_service.cli learning rebuild
```

Admission refuses (exit 1, clean message) a failing schema check, a
`hypothesis` level, or a run that is not closed SUCCESS — proven by an
`END-REPORT.md` whose first `Status` line contains `SUCCESS` under the
repository's own runs root — `<repository_path>/.flowrunner` beside the
directory its registry row records, through `refresh` or `set-repository`;
the rows `GET /v1/scopes` lists. `[learning] runs_root` stays
the father repository's default only, and an unregistered repository whose
slug is not the father's is reported `unknown repository` naming
`set-repository`. With no runs
directory present the check is skipped with a warning, so a foreign machine
can still admit by hand. The status line may carry Markdown
bold — `**Status:** SUCCESS` and `**Status: SUCCESS** — run closed.` both
prove closure, decoration before the word is ignored. Before validating, admission
normalises the mechanical slips itself: a scalar
`architecture_implications`, `failed_approaches` or `important_files`
becomes a one-item list (an empty string an empty list), a
`validation.testgoals` list of ids becomes `"<n>/<n> green"`, and integer
`family`/`run` become strings padded to match a zero-padded run directory
name — the draft file keeps its bytes, the admitted artifact is the
normalised document, and `GET /v1/learning/drafts` reports the sentences as
`normalisations` beside the post-normalisation `valid` and `violations`.
Every change writes its own `| normalised | <repository>/<family>/<run> |
<sentence>` ledger line before the `admitted` line, and `learning
validate-run` previews them under `would normalise:`. `--strict` on
`admit-run` and `admit` disables the fixes, so the document must validate
exactly as written. Superseding and retracting move
the older artifact to `<learning_dir>/history/...` with `superseded_by` /
`retracted_at` written into it; it stays retrievable through
`include_history=true`. Every admission, supersede and retraction is one
line in `<learning_dir>/LEDGER.md`. `refresh-all` skips the three learning
scopes (their registry rows carry the learning directory as location); they
are rebuilt by `learning rebuild` in seconds. An empty manifest leaves no
store behind, and searching an emptied learning scope answers with an empty
result list instead of reaching the provider.

The supervisor admits with
`learning admit-run <repository>/<family>/<run> --admitted-by <name>` (the
legacy two-part `<family>/<run>` means the father repository): the draft is
read from `<runs root>/<family>/runs/<run>/LEARNING-DRAFT.yaml` inside that
repository's runs root, its `admitted_by` is replaced with the name (the
placeholder `pending` itself is refused, as is an empty name), and exactly
the `learning admit` path runs on that document — the draft file itself is
never modified or moved. A draft whose `repository` field does not match the
repository it was found under is refused.
`learning validate-run <repository>/<family>/<run>` prints the same draft's
violations plus one `run status: <SUCCESS|BLOCKED|…|missing>` line from the
END-REPORT's first `Status` line (`missing` when there is none) and never
admits.
`learning drafts` lists every run-directory draft under every registered
repository, one tab-separated line each (`repository/family/run`, topic,
evidence level, run status, admitted/pending, valid/invalid, violation
count, sorted by family then run), and `GET /v1/learning/drafts` answers
that list as `{"drafts": [...]}` — read-only, no scope guard, like
`/v1/learning`; `?pending=true` keeps only the drafts whose artifact is not
admitted yet. `GET /v1/learning` rows carry `repository`, and
`GET /v1/learning?repository=<slug>` filters them to one repository. Every
ledger line carries a sixth column
`source=<path>` naming where the admitted artifact came from: the draft path
for `admit-run`, the given file path for `admit`.

The two config keys are `[learning] dir` (default `<index_dir>/learning`)
and `[learning] runs_root`. See `knowledge.ini.example`.

## Windows

Today's `leann` implementation is Linux-only in three places: LEANN's compiled
backend, the CUDA free-memory preflight, and the systemd units. Path
normalisation itself already works for both styles through the slug rule in
`knowledge_service/scopes.py`. The `portable` provider above is the part that
drops the compiled backend and the CUDA dependency, so a Windows host runs the
same service with `provider = portable` and needs no GPU.

The exact sequence a fresh Windows machine runs:

```bat
py -3.12 -m venv venv
venv\Scripts\pip install -r requirements-portable.txt
venv\Scripts\python -m knowledge_service.cli download-model
notepad %USERPROFILE%\.config\knowledge-service\knowledge.ini
rem   in the editor: enabled = true, provider = portable
venv\Scripts\python -m knowledge_service.cli refresh flowrunner C:\Projects\FlowRunner
curl "http://127.0.0.1:9140/v1/search?q=runtime+child&scope=flowrunner"
```

`download-model` fetches the ONNX files the portable provider reads
(`onnx/model.onnx`, `tokenizer.json` and the small config files), needs a
network once, and prints the model directory. `requirements-portable.txt`
lists exactly what the portable path imports — `fastapi`, `uvicorn`,
`pydantic`, `PyYAML`, and the lazily imported `onnxruntime`, `tokenizers` and
`numpy` — plus `pytest` and `httpx` so the readiness tests run from one
install. `leann`, the compiled LEANN backends and `torch` are not installed on
that machine and the package must not require them: everything above the
provider boundary works without them, which is what
`tests/test_portable_readiness.py` proves.

## Parity

`scripts/provider_parity.py` builds one scope twice into a temporary index
directory — once with the `leann` provider, once with `portable`, both
through the package's own maintenance path — and runs every query line of a
file against both stores:

```sh
python scripts/provider_parity.py --scope flowrunner \
    --repo ~/FlowRunner --queries queries.txt [--index-dir tmp] [--top-k 5] [--json]
```

One table row per query (`query`, `leann_top`, `portable_top`, `jaccard@k`,
`leann_ms`, `portable_ms`) plus a summary line with the mean Jaccard, the mean
answer latency, the build seconds and the store size per provider; `--json`
prints the same data as one JSON document. Everything is written under
`--index-dir` (default a fresh `mkdtemp`), never into the shared index
directory or the registry database.

Reading `jaccard@k`: the fraction of shared result paths in the two top-`k`
lists — `1.0` means the providers answered with the same files, values around
`0.6–0.8` mean the heads agree and only the tail differs, below `0.5` the two
providers genuinely disagree on that query and the wording of the query is
worth a look. A provider whose preflight fails (no GPU, no model files) shows
up in the summary as `unavailable: <detail>` with its columns empty; the
script itself never fails for that.

