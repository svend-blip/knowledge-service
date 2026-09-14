# SCOPE — knowledge-service 3A-3: the `portable` provider (no compiled backend, no GPU)

Treat this file as the complete project scope for this workspace
(`/home/svend/knowledge-service`). Re-initialise scope-mcp
(`init_project` with `reset: true`), ask only what is genuinely necessary,
then build.

## 1. Purpose

Platform independence is a standing requirement (DPMtF
`docs/SCOPE-ADDENDUM-KNOWLEDGE-3-HARNESSES.md`, "Platform independence").
The `leann` provider needs its compiled HNSW backend and a CUDA GPU; a
Windows FlowApp or a machine without a GPU has nothing. Add a second
provider behind the same interface that runs anywhere Python and
`onnxruntime` run: passages and metadata in SQLite, embeddings from a
small multilingual ONNX model on the CPU, cosine search over numpy. Slower
and simpler, same manifests, same scopes, same routes, same tests.

`onnxruntime` 1.30.0 (CPU) is installed in this repository's venv and
approved as a dependency; `huggingface_hub`, `tokenizers` and `numpy` are
already present. The interpreter for every check remains
`/home/svend/DPMtF-WebUI/venv/bin/python` for the hermetic tests; the
reviewer runs the live testgoal with `venv/bin/python` here.

## 2. Deliverable

### 2.1 `knowledge_service/portable_provider.py`

`PortableProvider(KnowledgeProvider)` with constructor
`(index_path, model_dir=None, embedder=None)`:

- **Store:** one SQLite file `<index_path>` (the same `<index_dir>/<scope>.leann`
  naming is NOT reused; the portable store is `<index_dir>/<scope>.portable.db`)
  with table `passages(id TEXT PRIMARY KEY, scope TEXT, path TEXT,
  content TEXT, embedding BLOB, dim INTEGER, model TEXT)`.
- **`index(manifest)`:** reads the JSONL manifest exactly as
  `LeannProvider._read_manifest` does, embeds `content` in batches, writes
  rows (`INSERT OR REPLACE` by the stable id `<scope>:<path>`), records the
  model id. **`update(manifest)`:** same as index (replace by id) — this
  provider supports it natively. **`remove(manifest)`:** deletes the
  manifest's ids. Both return `None`; neither raises `NotImplementedError`.
- **`search(query, scope, filters, top_k, token_budget)`:** embeds the query,
  computes cosine similarity against every stored embedding whose `scope`
  matches (and whose metadata matches `filters` equality entries), returns
  the same result shape as `LeannProvider` (`path`, `content`, `score`,
  `scope`), applies `top_k` and the whitespace token budget with the same
  truncation rule.
- **`preflight()`:** `onnxruntime` importable and the model files present
  under `model_dir`; otherwise `ProviderNotReady("knowledge provider not ready: <reason>")`.
  No GPU, no `torch`, no `nvidia-smi` anywhere in this module.
- **Embedder:** a small class wrapping `onnxruntime.InferenceSession` on
  `<model_dir>/onnx/model.onnx` and `tokenizers.Tokenizer.from_file(<model_dir>/tokenizer.json)`,
  mean pooling over the attention mask, L2-normalised float32 vectors;
  batch size 32; max 256 tokens per passage. Injectable through the
  constructor so tests use a fake.

### 2.2 Registration and config

- `search.py::resolve_provider(name, scope)` binds `portable` to
  `PortableProvider` with `index_path = <index_dir>/<scope>.portable.db`
  and `model_dir` from config.
- `config.py`: `[portable] model_dir` (default
  `~/.local/share/knowledge-service/models/paraphrase-multilingual-MiniLM-L12-v2`)
  and `[portable] model_id` (default
  `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`).
  `knowledge.ini.example` documents both and the choice `provider = portable`.
- `/v1/health` reports `provider` and the portable preflight detail
  (`model present: yes|no`) when the portable provider is configured.

### 2.3 CLI

`python -m knowledge_service.cli download-model [--model-id ID] [--model-dir DIR]`
fetches with `huggingface_hub.snapshot_download` only the files needed
(`onnx/model.onnx`, `tokenizer.json`, `config.json`, `tokenizer_config.json`,
`special_tokens_map.json`), prints the directory and the file sizes, exit 1
with a clean message when offline. No other command changes.

### 2.4 Tests (hermetic, fake embedder, temp dirs), named exactly

`test_portable_index_search_roundtrip_with_fake_embedder`,
`test_portable_update_replaces_and_remove_deletes`,
`test_portable_scope_filter_and_token_budget`,
`test_portable_preflight_reports_missing_model`,
`test_resolve_provider_binds_portable_store_path`,
`test_provider_contract_is_shared` (one parametrised contract test over
`none` and `portable`: `index` then `search` shape, `preflight` behaviour).
The existing 19 tests stay green.

### 2.5 README

Section "portable provider": when to use it, the model, the store file,
`download-model`, measured numbers (the reviewer fills in the live figures
after TG6), and the platform table: `leann` Linux + CUDA, `portable` any
platform with Python 3.12 and onnxruntime wheels (Linux, Windows, macOS).
`requirements.txt` gains `onnxruntime`.

## 3. Constraints

As before: work only here; never modify DPMtF, mcp-light, the shared index
directory or `~/.local/share/knowledge-service/`; no commits; no network
(do not run `download-model` yourself — the reviewer does); no services or
models touched; `py_compile` every changed file; parameterized SQL;
en-US; no new dependency beyond `onnxruntime`.

## 4. Definition of Done

Testgoals green when the reviewer measures them; `git status` limited to
`knowledge_service/`, `tests/`, `README.md`, `requirements.txt`,
`knowledge.ini.example`; coverage recorded; `complete_project` called.

```testgoals
id: TG1
what: the six named tests exist and pass and the whole suite is green
run: cd /home/svend/knowledge-service && grep -q "def test_onnx_embedder_pools_a_realistic_session_output" tests/test_knowledge_service.py && PYTHONDONTWRITEBYTECODE=1 /home/svend/DPMtF-WebUI/venv/bin/python -m pytest -q -p no:cacheprovider tests -k "portable_index_search_roundtrip_with_fake_embedder or portable_update_replaces_and_remove_deletes or portable_scope_filter_and_token_budget or portable_preflight_reports_missing_model or resolve_provider_binds_portable_store_path or provider_contract_is_shared or onnx_embedder_pools_a_realistic_session_output" 2>&1 | tail -n 1 | grep -E "^[7-9] passed|^[1-9][0-9] passed" && PYTHONDONTWRITEBYTECODE=1 /home/svend/DPMtF-WebUI/venv/bin/python -m pytest -q -p no:cacheprovider tests 2>&1 | tail -n 1 | grep -E "passed" | grep -vE "failed|error"
expect: exit 0

id: TG2
what: the portable module imports without torch, leann or nvidia-smi and the provider resolves
run: cd /home/svend/knowledge-service && ! grep -nE "^(import torch|from torch|import leann|from leann)|nvidia-smi" knowledge_service/portable_provider.py && /home/svend/DPMtF-WebUI/venv/bin/python -c "import sys; sys.path.insert(0, '.'); from knowledge_service import search, portable_provider; f = search.resolve_provider('portable', scope='flowrunner'); p = f(); assert isinstance(p, portable_provider.PortableProvider); assert str(getattr(p, '_index_path', getattr(p, 'index_path', ''))).endswith('flowrunner.portable.db'); print('ok')"
expect: exit 0

id: TG3
what: update and remove are real operations on the portable store
run: cd /home/svend/knowledge-service && /home/svend/DPMtF-WebUI/venv/bin/python -c "import sys, json, tempfile, os; sys.path.insert(0, '.'); from knowledge_service.portable_provider import PortableProvider; d = tempfile.mkdtemp(); m = os.path.join(d, 'm.jsonl'); open(m, 'w').write(json.dumps({'scope': 's', 'path': 'a.md', 'content': 'apple pie'}) + chr(10)); E = type('E', (), {'dim': 3, 'embed': lambda self, texts: [[1.0, 0.0, 0.0] if 'apple' in t else [0.0, 1.0, 0.0] for t in texts]}); p = PortableProvider(index_path=os.path.join(d, 's.portable.db'), embedder=E()); p.index(m); r = p.search('apple', scope='s', top_k=1); assert r and r[0]['path'] == 'a.md', r; open(m, 'w').write(json.dumps({'scope': 's', 'path': 'a.md', 'content': 'banana'}) + chr(10)); p.update(m); r2 = p.search('apple', scope='s', top_k=1); assert r2 and r2[0]['content'] == 'banana', r2; p.remove(m); assert p.search('apple', scope='s', top_k=1) == []; print('ok')"
expect: exit 0

id: TG4
what: requirements and the example ini carry the portable provider
run: cd /home/svend/knowledge-service && grep -q "^onnxruntime" requirements.txt && grep -q "\[portable\]" knowledge.ini.example && grep -q "download-model" README.md
expect: exit 0

id: TG5
what: FENCE — only the deliverable paths changed
run: cd /home/svend/knowledge-service && test -n "$(git status --porcelain)" && test -z "$(git status --porcelain | awk '{print $2}' | grep -v -E '^(knowledge_service/|tests/|README.md|requirements.txt|knowledge.ini.example)')"
expect: exit 0

id: TG6
what: LIVE (reviewer only) — the real model is downloaded, the flowrunner manifest is indexed with the portable provider into a temp store, and a search returns FlowRunner sources
run: cd /home/svend/knowledge-service && d="$(mktemp -d)" && venv/bin/python -m knowledge_service.cli download-model --model-dir "$d/model" >/dev/null && venv/bin/python -c "import sys, time; sys.path.insert(0, '.'); from knowledge_service.portable_provider import PortableProvider; p = PortableProvider(index_path=sys.argv[1] + '/flowrunner.portable.db', model_dir=sys.argv[1] + '/model'); t = time.perf_counter(); p.index('/home/svend/.local/share/dpmtf/knowledge_index/flowrunner.jsonl'); b = time.perf_counter() - t; t = time.perf_counter(); r = p.search('How is a FlowApp exported and imported?', scope='flowrunner', top_k=3); s = time.perf_counter() - t; print(f'build {b:.1f}s search {s:.2f}s', [x['path'] for x in r]); raise SystemExit(0 if r and any('export' in x['path'].lower() or 'import' in x['path'].lower() for x in r) else 1)" "$d"
expect: exit 0
```

## 4b. Correction 1 (reviewer, 2026-09-14 12:40Z) — the real embedder returns a 3-D result

Measured with the downloaded model (`OnnxEmbedder(model_dir).embed(["apple pie", "How is a FlowApp exported?"])`):
each returned "vector" is a list of lists, and `index()` then dies in
`_encode_vector` with `struct.error: required argument is not a float`. Cause
in `OnnxEmbedder._embed_batch`: `counts = mask.sum(axis=1)` already has shape
`(batch, 1)`; dividing `sums` (shape `(batch, hidden)`) by `counts[:, None]`
(shape `(batch, 1, 1)`) broadcasts to `(batch, batch, hidden)`. Fix: divide by
`counts` as it is. The fake-embedder tests could not see this because they
bypass pooling entirely. Required in this correction:

1. The one-line fix in `_embed_batch`, and an assertion right after pooling
   that `pooled.ndim == 2` and `pooled.shape[0] == len(texts)`.
2. A hermetic test, named exactly `test_onnx_embedder_pools_a_realistic_session_output`,
   that builds `OnnxEmbedder` with an injected fake session and tokenizer
   (`session.run` returns one array of shape `(batch, seq, hidden)`;
   `get_inputs()` names `input_ids` and `attention_mask`) and asserts the
   embedder returns `len(texts)` vectors of `hidden` floats, L2-normalised,
   and that padding positions do not change the pooled value. The
   constructor must therefore accept an optional session/tokenizer pair for
   tests without loading files.
3. TG1's named list gains that test (seven names). Nothing else changes.

Report as before with git status and TG1–TG5; the reviewer re-runs TG6.

## 5. Initial Execution Instruction

`init_project` with `reset: true`; goals for 2.1–2.5; implement; run TG1–TG5
and `py_compile`; record coverage; `complete_project`; report `git status`
and the pasted output of TG1–TG5. Do not run TG6 and do not download the
model.
