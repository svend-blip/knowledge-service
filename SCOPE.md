# SCOPE — knowledge-service A2-3: admission from run directories, pending drafts

Treat this file as the complete project scope for this workspace
(`/home/svend/knowledge-service`). Re-initialise scope-mcp (`init_project`
with `reset: true`), ask only what is genuinely necessary, then build. The
service is live on `http://127.0.0.1:9140` from commit `206040a` (A2-2
reviewed and merged). Read DPMtF's `docs/LEARNING-ARTIFACT.md` and
`docs/governance-templates-v2/SUPERVISOR_PLANNING.md` §Phase 6 first (read
only): they bind what the decomposer writes and what the supervisor does.

## 1. Purpose

DPMtF's decomposer now writes `<runs_root>/<family>/runs/<run>/LEARNING-DRAFT.yaml`
at every SUCCESS closure with `admitted_by: pending`; the supervisor admits
it. Today admission means copying the file somewhere, editing
`admitted_by` by hand and calling `learning admit <path>`. Three gaps:

1. No command admits a run's draft in place with the admitter's name.
2. Nothing lists the drafts that wait for admission, or whether their run
   actually closed SUCCESS.
3. The ledger does not say where an admitted artifact came from.

## 2. Deliverable

### 2.1 Admission from a run directory (`knowledge_service/learning.py`, `cli.py`)

- `draft_path(family, run) -> Path`: `<runs_root>/<family>/runs/<run>/LEARNING-DRAFT.yaml`
  (`config.get_learning_runs_root()`).
- `admit_run(ref: str, admitted_by: str) -> int`: `ref` is
  `"<family>/<run>"` (reuse `_split_ref`). Refuses (exit 1, clean message):
  a malformed ref, a missing draft, an empty `admitted_by`, or the value
  `pending` (case-insensitive). Loads the draft, sets `admitted_by` to the
  given name, then runs exactly the existing `admit` path on the document
  (validate → closure check → supersedes → write → ledger → rebuild) —
  factor the document-level part of `admit(path)` into
  `admit_document(doc, source: str) -> int` and let both entry points call
  it. The draft file itself is never modified or moved.
- `validate_run(ref: str) -> int`: prints the violations of the run's
  draft (like `validate_file`), plus one line `run status: <SUCCESS|BLOCKED|…|missing>`
  from the END-REPORT's first Status line (Markdown stripped, as
  `_check_run_closed` does; `missing` when there is no END-REPORT). Never
  admits.
- Ledger line: the existing five columns unchanged, plus a sixth
  `| source=<path>` — the draft path for `admit_run`, the given file path
  for `admit`. The existing ledger test keeps its assertions on the first
  five columns.
- CLI: `learning admit-run <family>/<run> --admitted-by <name>`,
  `learning validate-run <family>/<run>`, `learning drafts` (2.2's list,
  one tab-separated line per draft). Existing subcommands unchanged.

### 2.2 Pending drafts (`learning.py`, `app.py`)

- `list_pending_drafts() -> list[dict]`: scans
  `<runs_root>/*/runs/*/LEARNING-DRAFT.yaml`; one dict per draft:
  `family`, `run`, `topic`, `evidence_level`, `run_status` (as in
  `validate_run`), `admitted` (an artifact for `family/run` exists under
  the learning dir OR its history), `valid` (bool), `violations` (count).
  Sorted by `family`, then `run`. A draft that does not parse is listed
  with `valid` false, `violations` 1 and `topic` empty. A missing
  `runs_root` is an empty list.
- `GET /v1/learning/drafts` → `{"drafts": [...]}`; `?pending=true`
  (default false) keeps only `admitted == false`. Read-only, no scope
  guard (like `/v1/learning`).

### 2.3 Tests (hermetic, temp dirs, stub provider), named exactly

- `test_admit_run_reads_the_draft_from_the_runs_root_and_sets_admitted_by`
  (a temp runs root with `2000/runs/041/LEARNING-DRAFT.yaml` carrying
  `admitted_by: pending` and an END-REPORT `**Status:** SUCCESS`; after
  `admit_run("2000/041", "svend")` the admitted artifact carries
  `admitted_by: svend`, the draft file is byte-identical, the ledger line
  ends with `source=` + the draft path)
- `test_admit_run_refuses_pending_missing_and_unclosed`
  (`--admitted-by pending` → 1; missing draft → 1; END-REPORT
  `**Status:** BLOCKED` → 1 and nothing written)
- `test_validate_run_reports_violations_and_run_status_without_admitting`
- `test_drafts_route_lists_pending_drafts_with_run_status`
  (two families, three drafts, one already admitted → `admitted` true;
  `?pending=true` drops it; a broken YAML is listed invalid)
- `test_cli_learning_admit_run_and_drafts` (through the CLI entry point:
  `learning drafts` prints one line per draft; `learning admit-run` exits 0
  and the same path as the function)

Every existing test stays green.

### 2.4 README

`## Validated learning`: the three new commands, the drafts route, the
ledger's `source=` column, and the sentence that the supervisor admits
with `learning admit-run <family>/<run> --admitted-by <name>`.

## 3. Constraints

As before: work only here; never modify DPMtF, mcp-light, the shared
index directory or `~/.local/share/knowledge-service/`; no commits; no
network; interpreter `/home/svend/DPMtF-WebUI/venv/bin/python`; no new
dependencies; parameterized SQL; en-US; no services or models touched;
`py_compile` every changed file; the 46 existing tests stay green. The
live service on 9140 keeps running and is not restarted by you. Never
read or write under DPMtF's `.flowrunner/` — the tests use temp runs roots.

## 4. Definition of Done

Testgoals green when the reviewer measures them; `git status` limited to
`knowledge_service/`, `tests/`, `README.md`; coverage recorded;
`complete_project` called.

```testgoals
id: TG1
what: the five named tests exist and pass and the whole suite is green
run: cd /home/svend/knowledge-service && for t in admit_run_reads_the_draft_from_the_runs_root_and_sets_admitted_by admit_run_refuses_pending_missing_and_unclosed validate_run_reports_violations_and_run_status_without_admitting drafts_route_lists_pending_drafts_with_run_status cli_learning_admit_run_and_drafts; do grep -q "def test_$t" tests/*.py || exit 1; done && PYTHONDONTWRITEBYTECODE=1 /home/svend/DPMtF-WebUI/venv/bin/python -m pytest -q -p no:cacheprovider tests 2>&1 | tail -n 1 | grep -E "passed" | grep -vE "failed|error"
expect: exit 0

id: TG2
what: the functions, the route, the CLI verbs and the README carry the new surface
run: cd /home/svend/knowledge-service && grep -q "def admit_run" knowledge_service/learning.py && grep -q "def validate_run" knowledge_service/learning.py && grep -q "def list_pending_drafts" knowledge_service/learning.py && grep -q "def admit_document" knowledge_service/learning.py && grep -q "/v1/learning/drafts" knowledge_service/app.py && grep -q '"admit-run"' knowledge_service/cli.py && grep -q '"validate-run"' knowledge_service/cli.py && grep -q '"drafts"' knowledge_service/cli.py && grep -q "admit-run" README.md
expect: exit 0

id: TG3
what: FENCE — only the deliverable paths changed
run: cd /home/svend/knowledge-service && test -n "$(git status --porcelain)" && test -z "$(git status --porcelain | awk '{print $2}' | grep -v -E '^(knowledge_service/|tests/|README.md)')"
expect: exit 0

id: TG4
what: LIVE (reviewer only) — the first real LEARNING-DRAFT of a closed DPMtF run is admitted with admit-run against the live config and answers on 9140
run: test -f /tmp/claude-1000/-home-svend-DPMtF-WebUI/e20394ae-27d0-4204-804f-5d6a2f5da054/scratchpad/a2-3-live/ok
expect: exit 0
```

## 5. Initial Execution Instruction

`init_project` with `reset: true`; goals for 2.1–2.4 in order; checkpoint
after each goal; ask now, in one message, only what is genuinely ambiguous;
implement; run TG1–TG3 and `py_compile`; record coverage;
`complete_project`; report `git status` and the pasted output of TG1–TG3.
TG4 is the reviewer's, after the reviewer restarts the service; do not
attempt it and do not restart anything.
