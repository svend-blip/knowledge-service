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
| GET | `/v1/scopes` | — | List registered scopes with document counts |
| GET | `/v1/scope-for-path` | `path` | Resolve a repository path to its scope slug |
| GET | `/v1/health` | — | Provider, enabled flag, preflight result |

Status codes: `200` with the stable envelope `{"enabled", "provider",
"results", "bounded"}` when searching is on or disabled; `403` with
`{"detail": ...}` when the scope guard denies the caller's scope; `503` with
`{"detail": ...}` when the provider is not ready (no free GPU yet); `400` on
bad input (`repo_path` is not a directory, empty scope, manifest inside the
repository). `top_k` and `token_budget` fall back to the configured values
and are clamped to them: a caller may lower them, never raise them.

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
| `provider` | `none` | `none` or `leann`; unknown keys behave like `none` |
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
original ids, so re-running it changes nothing. Imported rows keep the
location they came with; a row whose path no longer exists is skipped until a
`refresh` records a directory again. Point `--from` at the live database file
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

## Windows

Today's implementation is Linux-only in three places: LEANN's compiled
backend, the CUDA free-memory preflight, and the systemd units. Path
normalisation itself already works for both styles through the slug rule in
`knowledge_service/scopes.py`. A `portable` provider that drops the compiled
backend and the CUDA dependency is a later part.
