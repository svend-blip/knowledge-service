# SCOPE — knowledge-service A2-1: validated learning artifacts (addendum 2, service side)

Treat this file as the complete project scope for this workspace
(`/home/svend/knowledge-service`). Re-initialise scope-mcp (`init_project`
with `reset: true`), ask only what is genuinely necessary, then build. Read
DPMtF's `docs/SCOPE-ADDENDUM-KNOWLEDGE-2-VALIDATED-LEARNING.md` first (read
only); the Human decisions D1–D4 in it are binding. This scope is the
service side only: storing, admitting, superseding, retracting and
searching learning artifacts. Producing the drafts inside DPMtF's chains is
a later DPMtF run.

## 1. Purpose

Two new scopes join the ten repository scopes: `experience` (validated
learning artifacts, one YAML per closed run) and `ecosystem` (the
architecture implications promoted out of those artifacts). Both are
ordinary stores of this service (built from manifests, not from a
repository), filtered at search time by evidence level, superseded by
manifest edit and rebuild, and fully auditable from the CLI.

## 2. Deliverable

### 2.1 Artifact schema and storage (`knowledge_service/learning.py`)

- Artifacts live under `<learning_dir>/<family>/<run>.yaml`
  (`[learning] dir`, default `<index_dir>/learning`); superseded or
  retracted ones move to `<learning_dir>/history/<family>/<run>.yaml` with
  `superseded_by` or `retracted_at` written into them.
- Schema, every key required (empty lists allowed): `topic`, `scope`
  (must be `experience`), `repository`, `family`, `run`, `problem`,
  `approach`, `result`, `failed_approaches[]`, `important_files[]`
  (repository-relative, forward slashes), `architecture_implications[]`,
  `validation{evidence_level, verdicts[], testgoals}`, `confidence`
  (high|medium|low), `supersedes[]` (`"<family>/<run>"`), `admitted_by`.
  `evidence_level` is one of `tests`, `measured_runtime`,
  `approved_architecture`, `reviewer_conclusion`, `observation`,
  `hypothesis`.
- `validate(doc) -> list[str]` returns every violation; `admit(path)`
  refuses (exit 1, clean message) when validation fails, when
  `evidence_level == "hypothesis"`, or when the artifact names a run that
  is not closed SUCCESS — closure is proven by an `END-REPORT.md` whose
  first `Status` line contains `SUCCESS`, found under `[learning] runs_root`
  (default `~/DPMtF-WebUI/.flowrunner/<family>/runs/<run>/`; the check is
  skipped with a warning when the directory does not exist, so a foreign
  machine can still admit by hand).
- `admit` applies `supersedes`: each named artifact is moved to history with
  `superseded_by`; then both manifests are rewritten and both scopes rebuilt
  (2.3). `retract(<family>/<run>)` moves an artifact to history with
  `retracted_at` and rebuilds. `list()` prints family/run, topic,
  evidence level, confidence, admitted_by.

### 2.2 Manifests and passages

- `experience` manifest: one passage per artifact; `content` is a rendered
  text of the artifact (topic, problem, approach, result, failed
  approaches, important files, validation summary); passage metadata
  carries `scope`, `path` (`<family>/<run>.yaml`), `evidence_level`,
  `repository`, `family`, `run`, `confidence`.
- `ecosystem` manifest: one passage per non-empty
  `architecture_implications` entry, `content` = the implication text plus
  its origin (family/run/topic), metadata as above plus `origin`.
- `experience-history` manifest from the history directory, same shape,
  plus `superseded_by` / `retracted_at` in metadata.

### 2.3 Building non-repository scopes (`maintenance.py`)

`refresh_manifest_scope(scope, manifest_path) -> dict`: preflight, then
`provider.index(manifest)` and `record_index(scope, provider, manifest,
count, "changed")` — no repository scan, no change detection. Used for
`experience`, `ecosystem` and `experience-history`. `refresh-all` skips
these three scopes (their registry rows carry `location` under the
learning directory, which is how they are told apart).

### 2.4 Search (`app.py`, `search.py`)

- `/v1/search` gains `evidence_level` (minimum level; default
  `approved_architecture`, i.e. the three strongest levels pass) and
  `include_history` (bool, default false). For scope `experience` the
  level filter is applied through the provider's metadata filters as an
  equality set over the admitted levels (LEANN and portable both support
  equality filters; implement the "at least" semantics as a list of
  allowed values). `include_history=true` searches `experience-history`
  instead. Other scopes ignore both parameters.
- `/v1/scopes` lists the three learning scopes like any other.

### 2.5 CLI

`python -m knowledge_service.cli learning {validate <yaml>|admit <yaml>|retract <family>/<run>|list|rebuild}`.
Clean errors, exit 1 on refusal, and one line per admission or retraction
appended to `<learning_dir>/LEDGER.md` (timestamp, action, family/run,
evidence level, admitted_by).

### 2.6 Tests (hermetic, temp dirs, stub provider), named exactly

`test_learning_schema_rejects_missing_keys_and_hypothesis`,
`test_admit_refuses_a_run_without_success_end_report`,
`test_admit_writes_manifests_and_rebuilds_both_scopes`,
`test_supersede_moves_the_older_artifact_to_history`,
`test_retract_moves_to_history_and_rebuilds`,
`test_search_filters_experience_by_evidence_level`,
`test_search_include_history_targets_the_history_scope`,
`test_refresh_all_skips_learning_scopes`,
`test_learning_ledger_records_every_admission`.

### 2.7 README

Section "Validated learning": the schema, evidence levels and the default
filter, supersede and retract mechanics, the ledger, and the CLI. State
that DPMtF's chain roles produce the drafts and the supervisor admits
them (a later DPMtF run).

## 3. Constraints

As before: work only here; never modify DPMtF, mcp-light, the shared
index directory or `~/.local/share/knowledge-service/`; no commits; no
network; interpreter `/home/svend/DPMtF-WebUI/venv/bin/python`; no new
dependencies (`pyyaml` is already installed); parameterized SQL; en-US;
no services or models touched; `py_compile` every changed file; the 27
existing tests stay green. The live service on 9140 keeps running.

## 4. Definition of Done

Testgoals green when the reviewer measures them; `git status` limited to
`knowledge_service/`, `tests/`, `README.md`, `knowledge.ini.example`;
coverage recorded; `complete_project` called.

```testgoals
id: TG1
what: the nine named tests exist and pass and the whole suite is green
run: cd /home/svend/knowledge-service && for t in learning_schema_rejects_missing_keys_and_hypothesis admit_refuses_a_run_without_success_end_report admit_writes_manifests_and_rebuilds_both_scopes supersede_moves_the_older_artifact_to_history retract_moves_to_history_and_rebuilds search_filters_experience_by_evidence_level search_include_history_targets_the_history_scope refresh_all_skips_learning_scopes learning_ledger_records_every_admission; do grep -q "def test_$t" tests/*.py || exit 1; done && PYTHONDONTWRITEBYTECODE=1 /home/svend/DPMtF-WebUI/venv/bin/python -m pytest -q -p no:cacheprovider tests 2>&1 | tail -n 1 | grep -E "passed" | grep -vE "failed|error"
expect: exit 0

id: TG2
what: validate and the evidence rules behave on a temp artifact
run: cd /home/svend/knowledge-service && /home/svend/DPMtF-WebUI/venv/bin/python -c "import sys; sys.path.insert(0, '.'); from knowledge_service import learning; doc = {'topic': 't', 'scope': 'experience', 'repository': 'flowrunner', 'family': '2000', 'run': '029', 'problem': 'p', 'approach': 'a', 'result': 'r', 'failed_approaches': [], 'important_files': ['knowledge/search.py'], 'architecture_implications': [], 'validation': {'evidence_level': 'tests', 'verdicts': ['002 APPROVED'], 'testgoals': '7/7'}, 'confidence': 'high', 'supersedes': [], 'admitted_by': 'supervisor'}; assert learning.validate(doc) == [], learning.validate(doc); bad = dict(doc); bad['validation'] = dict(doc['validation'], evidence_level='hypothesis'); assert learning.validate(bad), 'hypothesis must be a violation'; missing = dict(doc); del missing['problem']; assert learning.validate(missing); print('ok')"
expect: exit 0

id: TG3
what: the search route accepts evidence_level and include_history and the CLI has the learning group
run: cd /home/svend/knowledge-service && grep -q "evidence_level" knowledge_service/app.py && grep -q "include_history" knowledge_service/app.py && grep -q '"learning"' knowledge_service/cli.py && grep -q "def refresh_manifest_scope" knowledge_service/maintenance.py
expect: exit 0

id: TG4
what: LIVE (reviewer only) — one real artifact is admitted, searchable in experience, promoted to ecosystem, and superseded into history
run: cd /home/svend/knowledge-service && test -f /tmp/claude-1000/-home-svend-DPMtF-WebUI/e20394ae-27d0-4204-804f-5d6a2f5da054/scratchpad/learning-live/ok
expect: exit 0

id: TG5
what: FENCE — only the deliverable paths changed
run: cd /home/svend/knowledge-service && test -n "$(git status --porcelain)" && test -z "$(git status --porcelain | awk '{print $2}' | grep -v -E '^(knowledge_service/|tests/|README.md|knowledge.ini.example)')"
expect: exit 0
```

## 5. Initial Execution Instruction

`init_project` with `reset: true`; goals for 2.1–2.7 in order; checkpoint
after each goal; ask now, in one message, only what is genuinely ambiguous;
implement; run TG1–TG3 and `py_compile`; record coverage;
`complete_project`; report `git status` and the pasted output of TG1–TG3.
TG4 is the reviewer's live measurement against a temporary learning
directory; do not attempt it.
