# SCOPE — knowledge-service A2-4: the learning artifact key carries the repository

Treat this file as the complete project scope for this workspace
(`/home/svend/knowledge-service`). Re-initialise scope-mcp (`init_project`
with `reset: true`), ask only what is genuinely necessary, then build. The
service is live on `http://127.0.0.1:9140` from commit `2d2ba3d`; one real
artifact is admitted (`2000/040`, repository DPMtF-WebUI).

## 1. Purpose

Learning artifacts are keyed `<family>/<run>` (`<learning_dir>/<family>/<run>.yaml`,
ledger refs, `supersedes`, passage paths). Two repositories now run the
same family numbers: DPMtF-WebUI's run `2000/001` and FlowRunner's run
`2000/001` (its own `.flowrunner/2000/` root, first run closed 2026-09-14).
Their artifacts would overwrite each other, and `admit-run` / `drafts` look
only under one `runs_root` (DPMtF's). The repository must be part of the
key, and drafts must be found under every repository the service knows.

## 2. Deliverable

### 2.1 Key and layout (`knowledge_service/learning.py`)

- The artifact identity is `<repository>/<family>/<run>`, where
  `<repository>` is the artifact's `repository` field normalised to the
  scope slug the service already uses for that repository (the slug rule
  of `/v1/scope-for-path`; `DPMtF-WebUI` → `dpmtf-webui`, `FlowRunner` →
  `flowrunner`). Files live at `<learning_dir>/<repository>/<family>/<run>.yaml`,
  history at `<learning_dir>/history/<repository>/<family>/<run>.yaml`.
  Passage `path` and ledger refs use the three-part key.
- `supersedes` entries accept `"<repository>/<family>/<run>"` and, for
  compatibility, `"<family>/<run>"` meaning the same repository as the
  artifact; `validate` reports a malformed reference either way.
- One-time migration on `learning rebuild` and on every `admit`: an
  artifact found at the legacy `<learning_dir>/<family>/<run>.yaml` (or
  its history twin) is moved under its `repository` slug, one ledger
  line `migrated` per artifact, idempotent. Manifests are rebuilt from the
  new layout.

### 2.2 Runs roots per repository (`learning.py`, `config.py`)

- `[learning] runs_root` becomes the DPMtF default only; the runs root of
  any repository is `<registry location>/.flowrunner`, where the location
  is the `knowledge_indexes` row of that repository's scope (the same rows
  `/v1/scopes` lists). A repository without a registry row falls back to
  `[learning] runs_root` when its slug is the father's, else is reported
  `unknown repository`.
- `draft_path(repository, family, run)`, `admit_run("<repository>/<family>/<run>", admitted_by)`,
  `validate_run(...)`: three-part refs; the legacy two-part ref means the
  father repository. `list_pending_drafts()` scans
  `<runs root>/*/runs/*/LEARNING-DRAFT.yaml` for every registered
  repository and carries `repository` on each row; `/v1/learning/drafts`
  and `learning drafts` show it. The admitted draft's `repository` field
  must match the repository it was found under (else refused).

### 2.3 Routes and CLI

`GET /v1/learning` rows gain `repository`; `?repository=<slug>` filters.
`learning list`, `learning drafts`, `admit-run`, `validate-run`, `retract`
take three-part refs (two-part = father). README §Validated learning
updated for the key, the layout, the migration line and the per-repository
runs roots.

### 2.4 Tests (hermetic, temp dirs, stub provider), named exactly

- `test_artifact_key_carries_the_repository_slug` (two artifacts
  `dpmtf-webui/2000/001` and `flowrunner/2000/001` coexist; passage paths
  and ledger lines carry the three-part key)
- `test_legacy_layout_is_migrated_once_with_a_ledger_line`
- `test_supersedes_accepts_two_and_three_part_references`
- `test_drafts_are_found_under_every_registered_repository` (a temp
  registry with two repository scopes whose locations hold `.flowrunner`
  trees; `list_pending_drafts` returns both with `repository`;
  `?repository=` filters)
- `test_admit_run_refuses_a_draft_whose_repository_does_not_match`
- `test_learning_routes_and_cli_take_three_part_refs`

Every existing test stays green (adapt only the ones that assert the old
two-part layout, keeping their meaning).

## 3. Constraints

As before: work only here; never modify DPMtF, mcp-light, the shared
index directory or `~/.local/share/knowledge-service/`; no commits; no
network; interpreter `/home/svend/DPMtF-WebUI/venv/bin/python`; no new
dependencies; parameterized SQL; en-US; no services or models touched;
`py_compile` every changed file; the 59 existing tests stay green. The
live service on 9140 keeps running and is not restarted by you. Never
read or write under any real `.flowrunner/` — tests use temp roots.

## 4. Definition of Done

```testgoals
id: TG1
what: the six named tests exist and pass and the whole suite is green
run: cd /home/svend/knowledge-service && for t in artifact_key_carries_the_repository_slug legacy_layout_is_migrated_once_with_a_ledger_line supersedes_accepts_two_and_three_part_references drafts_are_found_under_every_registered_repository admit_run_refuses_a_draft_whose_repository_does_not_match learning_routes_and_cli_take_three_part_refs; do grep -q "def test_$t" tests/*.py || exit 1; done && PYTHONDONTWRITEBYTECODE=1 /home/svend/DPMtF-WebUI/venv/bin/python -m pytest -q -p no:cacheprovider tests 2>&1 | tail -n 1 | grep -E "passed" | grep -vE "failed|error"
expect: exit 0

id: TG2
what: the three-part key, the migration and the per-repository runs roots are wired and documented
run: cd /home/svend/knowledge-service && grep -q "migrated" knowledge_service/learning.py && grep -q "repository" knowledge_service/learning.py && grep -q "\.flowrunner" knowledge_service/learning.py && grep -q '"repository"' knowledge_service/app.py && grep -q "three-part\|<repository>/<family>/<run>" README.md
expect: exit 0

id: TG3
what: FENCE — only the deliverable paths changed
run: cd /home/svend/knowledge-service && test -n "$(git status --porcelain)" && test -z "$(git status --porcelain | awk '{print $2}' | grep -v -E '^(knowledge_service/|tests/|README.md|knowledge.ini.example)')"
expect: exit 0

id: TG4
what: LIVE (reviewer only) — on 9140 the admitted artifact answers under dpmtf-webui/2000/040 after the migration and /v1/learning/drafts lists both repositories
run: test -f /tmp/claude-1000/-home-svend-DPMtF-WebUI/e20394ae-27d0-4204-804f-5d6a2f5da054/scratchpad/a2-4-live/ok
expect: exit 0
```

## 5. Initial Execution Instruction

`init_project` with `reset: true`; goals for 2.1–2.4 in order; checkpoint
after each goal; ask now, in one message, only what is genuinely ambiguous;
implement; run TG1–TG3 and `py_compile`; record coverage;
`complete_project`; report `git status` and the pasted output of TG1–TG3.
TG4 is the reviewer's after the service restart; do not attempt it and do
not restart anything.
