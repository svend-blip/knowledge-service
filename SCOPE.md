# SCOPE — knowledge-service A3-1: the portable provider end-to-end, and Windows readiness

Treat this file as the complete project scope for this workspace
(`/home/svend/knowledge-service`). Re-initialise scope-mcp (`init_project`
with `reset: true`), ask only what is genuinely necessary, then build. The
service is live on `http://127.0.0.1:9140` from commit `2f6f61c`. Read
DPMtF's `docs/SCOPE-ADDENDUM-KNOWLEDGE-3-HARNESSES.md` §"Platform
independence" first (read only).

## 1. Purpose

The portable provider (SQLite + onnxruntime, no compiled backend, no GPU)
exists and passes its hermetic tests, but nothing yet proves it can carry a
real scope end-to-end, nothing compares its answers with the LEANN
provider's, and nothing proves the package imports and runs where LEANN,
torch and a GPU are absent (the Windows PC). This scope adds the
instrument and the proofs; the reviewer runs the live parts with the real
model on this machine.

## 2. Deliverable

### 2.1 Parity instrument (`scripts/provider_parity.py`, new)

`python scripts/provider_parity.py --scope <name> --repo <path> --queries <file> [--index-dir <tmp>] [--top-k 5] [--json]`
builds the scope twice into a temporary index directory — once with the
`leann` provider, once with the `portable` provider, both through the
package's own indexer/maintenance path (`knowledge_service.maintenance`
functions, not shell-outs) — runs every query line of `<file>` against
both, and prints one table row per query: `query`, `leann_top` (paths),
`portable_top`, `jaccard@k`, `leann_ms`, `portable_ms`, plus a summary
line with mean Jaccard, mean latency per provider, build seconds and
store size per provider. `--json` prints the same as one JSON document.
A provider whose preflight fails (no GPU, no model files) is reported in
the summary as `unavailable: <detail>` and its columns stay empty — the
script never raises for that. It writes only under `--index-dir` (default
a `tempfile.mkdtemp`) and never touches the shared index directory or the
registry database (use in-memory or temp registry paths).

### 2.2 Windows-readiness proofs (`tests/test_portable_readiness.py`, new)

Named exactly:
- `test_package_imports_without_leann_torch_or_gpu`: in a subprocess with
  an import hook that raises `ImportError` for `leann`,
  `leann_backend_hnsw` and `torch` (nothing else is blocked); importing
  `knowledge_service`, `knowledge_service.app`, `knowledge_service.cli`,
  `knowledge_service.portable_provider`, `knowledge_service.learning` and
  `knowledge_service.maintenance` succeeds, and `create_app()` returns an
  app when the configured provider is `portable`.
- `test_portable_provider_paths_are_pathlib_and_no_posix_only_calls`: the
  portable provider module and `config.py` use `pathlib` for every path
  they build (no `os.path.join` with `/` literals), and `chmod`, `fcntl`,
  `os.geteuid` and `signal` are not referenced in `portable_provider.py`
  or `learning.py` (grep over the source text; `db.py`'s owner-only file
  creation may keep its guarded `chmod`).
- `test_portable_build_and_search_round_trip_with_a_fake_embedder`:
  build a three-document manifest into a temp SQLite store with the fake
  embedder, search, and get the expected top-1 (exists in spirit already —
  make it the named proof if a similar test exists, otherwise add it).
- `test_parity_script_reports_an_unavailable_provider_instead_of_raising`
  (drive `scripts/provider_parity.py` in-process with a stub LEANN
  provider whose `preflight` raises `ProviderNotReady` and the fake
  portable embedder; the summary names `unavailable`).

### 2.3 CLI and README

- `knowledge_service.cli download-model` already exists; document it
  under `## Windows` with the exact sequence a fresh Windows machine runs:
  `pip install -r requirements-portable.txt` (a new file listing only
  what the portable path actually imports — derive it from the imports of
  the modules the service loads with `provider = portable`; do not
  guess), `download-model`, `knowledge.ini` with
  `provider = portable`, `refresh`, `search`. State that `leann` and
  `torch` are not installed on that machine and that the package must not
  require them.
- `## Parity` section: how to run `scripts/provider_parity.py` and how to
  read Jaccard@k.

## 3. Constraints

As before: work only here; never modify DPMtF, mcp-light, the shared
index directory or `~/.local/share/knowledge-service/`; no commits; no
network (the model download is the reviewer's); interpreter
`/home/svend/DPMtF-WebUI/venv/bin/python`; no new dependencies;
parameterized SQL; en-US; no services or models touched; `py_compile`
every changed file; the 51 existing tests stay green. The live service on
9140 keeps running and is not restarted by you.

## 4. Definition of Done

Testgoals green when the reviewer measures them; `git status` limited to
`knowledge_service/`, `scripts/`, `tests/`, `README.md`,
`requirements-portable.txt`; coverage recorded; `complete_project` called.

```testgoals
id: TG1
what: the four named readiness tests exist and pass and the whole suite is green
run: cd /home/svend/knowledge-service && for t in package_imports_without_leann_torch_or_gpu portable_provider_paths_are_pathlib_and_no_posix_only_calls portable_build_and_search_round_trip_with_a_fake_embedder parity_script_reports_an_unavailable_provider_instead_of_raising; do grep -q "def test_$t" tests/test_portable_readiness.py || exit 1; done && PYTHONDONTWRITEBYTECODE=1 /home/svend/DPMtF-WebUI/venv/bin/python -m pytest -q -p no:cacheprovider tests 2>&1 | tail -n 1 | grep -E "passed" | grep -vE "failed|error"
expect: exit 0

id: TG2
what: the parity script exists, compiles, and names both providers, jaccard and the unavailable path; the portable requirements file and README sections exist
run: cd /home/svend/knowledge-service && test -f scripts/provider_parity.py && /home/svend/DPMtF-WebUI/venv/bin/python -m py_compile scripts/provider_parity.py && grep -q "jaccard" scripts/provider_parity.py && grep -q "unavailable" scripts/provider_parity.py && grep -q "portable" scripts/provider_parity.py && test -f requirements-portable.txt && ! grep -q -i "^leann\|^torch" requirements-portable.txt && grep -q "^## Windows" README.md && grep -q "^## Parity" README.md && grep -q "download-model" README.md
expect: exit 0

id: TG3
what: FENCE — only the deliverable paths changed
run: cd /home/svend/knowledge-service && test -n "$(git status --porcelain)" && test -z "$(git status --porcelain | awk '{print $2}' | grep -v -E '^(knowledge_service/|scripts/|tests/|README.md|requirements-portable.txt)')"
expect: exit 0

id: TG4
what: LIVE (reviewer only) — with the real ONNX model downloaded, the parity script builds dpmtf-webui with both providers and reports Jaccard@5 and latencies
run: test -f /tmp/claude-1000/-home-svend-DPMtF-WebUI/e20394ae-27d0-4204-804f-5d6a2f5da054/scratchpad/a3-1-live/ok
expect: exit 0
```

## 5. Initial Execution Instruction

`init_project` with `reset: true`; goals for 2.1–2.3 in order; checkpoint
after each goal; ask now, in one message, only what is genuinely ambiguous;
implement; run TG1–TG3 and `py_compile`; record coverage;
`complete_project`; report `git status` and the pasted output of TG1–TG3.
TG4 is the reviewer's (it downloads the model); do not attempt it, do not
download anything and do not restart anything.
