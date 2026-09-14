# SCOPE — knowledge-service A2-5: admission normalises the two schema slips decomposers make

Treat this file as the complete project scope for this workspace
(`/home/svend/knowledge-service`). Re-initialise scope-mcp (`init_project`
with `reset: true`), ask only what is genuinely necessary, then build. The
service is live on `http://127.0.0.1:9140` from commit `e5e375c`; four
artifacts are admitted.

## 1. Purpose

Every LEARNING-DRAFT.yaml a chain decomposer has written so far (four out
of four, two repositories, two model vendors) failed `validate` on the
same two slips and had to be hand-edited by the supervisor before
`admit-run`: `architecture_implications` written as one string instead of
a one-item list, and `validation.testgoals` written as a list of ids
instead of the `"n/m green"` string. The schema stays strict (the artifact
on disk is always canonical), but admission should carry the two
mechanical fixes itself, visibly.

## 2. Deliverable

### 2.1 Normalisation (`knowledge_service/learning.py`)

- `normalise(doc) -> tuple[dict, list[str]]`: returns a copy with, when
  present, (a) a scalar `architecture_implications`, `failed_approaches`
  or `important_files` wrapped into a one-item list (an empty string
  becomes an empty list), (b) `validation.testgoals` given as a list of
  ids turned into `"<n>/<n> green"` (n = its length), (c) `family` and
  `run` given as integers turned into zero-padded strings only when the
  draft's own run directory name is zero-padded (else plain strings), and
  a list of one human sentence per change. Anything else is untouched;
  `validate` still runs on the result.
- `admit_run` and `admit` apply `normalise` before `validate` (the
  file on disk is never modified; the admitted artifact is the normalised
  document), and every change becomes its own ledger line
  `| normalised | <repository>/<family>/<run> | <sentence>` written
  before the `admitted` line. `validate_run` prints the sentences under
  `would normalise:` before the violations of the normalised document,
  so a supervisor sees what admission will change.
- `--strict` on `admit-run` and `admit` disables normalisation (the
  document must validate as written).

### 2.2 Route

`GET /v1/learning/drafts` rows gain `normalisations` (the sentences, empty
when none) and `valid` reflects the normalised document; `violations`
counts what remains after normalisation.

### 2.3 Tests, named exactly (in `tests/test_learning.py`)

- `test_normalise_wraps_scalars_and_rewrites_testgoals_and_reports_each_change`
- `test_admit_run_normalises_and_ledgers_before_admitting`
  (draft with both slips on disk → admitted artifact canonical, draft
  file byte-identical, two `normalised` lines then one `admitted` line)
- `test_strict_admission_refuses_what_normalisation_would_fix`
- `test_drafts_route_reports_normalisations_and_post_normalisation_validity`

### 2.4 README

`## Validated learning`: three sentences on normalisation, the ledger
lines and `--strict`.

## 3. Constraints

As before: work only here; never modify DPMtF, mcp-light, the shared
index directory or `~/.local/share/knowledge-service/`; no commits; no
network; interpreter `/home/svend/DPMtF-WebUI/venv/bin/python`; no new
dependencies; en-US; no services or models touched; `py_compile` every
changed file; the 67 existing tests stay green. The live service on 9140
keeps running and is not restarted by you. Never read or write under any
real `.flowrunner/` — tests use temp roots.

## 4. Definition of Done

```testgoals
id: TG1
what: the four named tests exist and pass and the whole suite is green
run: cd /home/svend/knowledge-service && for t in normalise_wraps_scalars_and_rewrites_testgoals_and_reports_each_change admit_run_normalises_and_ledgers_before_admitting strict_admission_refuses_what_normalisation_would_fix drafts_route_reports_normalisations_and_post_normalisation_validity; do grep -q "def test_$t" tests/test_learning.py || exit 1; done && PYTHONDONTWRITEBYTECODE=1 /home/svend/DPMtF-WebUI/venv/bin/python -m pytest -q -p no:cacheprovider tests 2>&1 | tail -n 1 | grep -E "passed" | grep -vE "failed|error"
expect: exit 0

id: TG2
what: normalise exists, admission ledgers it, strict is a flag, the route carries it, the README says so
run: cd /home/svend/knowledge-service && grep -q "def normalise" knowledge_service/learning.py && grep -q '"normalised"' knowledge_service/learning.py && grep -q '"--strict"' knowledge_service/cli.py && grep -q "normalisations" knowledge_service/app.py && grep -q -i "normalis" README.md
expect: exit 0

id: TG3
what: FENCE — only the deliverable paths changed
run: cd /home/svend/knowledge-service && test -n "$(git status --porcelain)" && test -z "$(git status --porcelain | awk '{print $2}' | grep -v -E '^(knowledge_service/|tests/|README.md)')"
expect: exit 0

id: TG4
what: LIVE (reviewer only) — after the service restart, validate-run on a real draft with a slip shows the normalisation sentence and admit-run of the next real draft needs no hand edit
run: test -f /tmp/claude-1000/-home-svend-DPMtF-WebUI/e20394ae-27d0-4204-804f-5d6a2f5da054/scratchpad/a2-5-live/ok
expect: exit 0
```

## 5. Initial Execution Instruction

`init_project` with `reset: true`; goals for 2.1–2.4 in order; checkpoint
after each goal; ask now, in one message, only what is genuinely ambiguous;
implement; run TG1–TG3 and `py_compile`; record coverage;
`complete_project`; report `git status` and the pasted output of TG1–TG3.
TG4 is the reviewer's after the service restart; do not attempt it and do
not restart anything.
