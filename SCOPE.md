# SCOPE — knowledge-service A2-7: the retrieval log is prunable, and pruning is honest

Treat this file as the complete project scope for this workspace
(`/home/svend/knowledge-service`). Re-initialise scope-mcp (`init_project`
with `reset: true`), ask only what is genuinely necessary, then build. The
service is live on `http://127.0.0.1:9140` from commit `84eda5d`; the
retrieval log holds 663 rows covering 2026-09-12 to 2026-09-15 in a
288 KB database.

## 1. Purpose

Three days of ordinary use wrote 663 rows, and nothing removes any of
them. The log is the audit trail — A2-6 made it readable and addendum 2
§7 promises an operator can ask what a run retrieved — so it must not be
truncated casually, and it must not grow without a bound either. Give it
one deliberate, auditable pruning path: an operator says how much history
to keep, sees exactly what would go before anything goes, and finds the
deletion recorded afterwards.

## 2. Deliverable

### 2.1 Prune (`knowledge_service/retrieval_log.py`)

`prune_retrievals(*, older_than=None, keep_last=None, run_id=None, flow_key=None, agent_role=None, scope=None, dry_run=True) -> dict`

- **Selection.** `older_than` is an ISO-8601 timestamp or a plain day
  count (`"30"`, meaning 30 days before now, UTC); rows with
  `created_at` strictly older are selected. `keep_last` is a row count:
  every row except the newest N is selected. The two may be combined —
  a row must satisfy **both** to be selected, so `--older-than 30
  --keep-last 1000` keeps anything younger than 30 days *and* the newest
  thousand. The other filters narrow the selection exactly as in
  `query_retrievals`. At least one of `older_than` / `keep_last` is
  required: without one the call is a `ValueError`, never a full wipe.
- **Result.** `{"selected": n, "deleted": n, "dry_run": bool,
  "oldest": iso|None, "newest": iso|None, "by_scope": {scope: n},
  "by_agent_role": {role: n}, "remaining": n}` — `deleted` is 0 on a dry
  run, `remaining` is the count left in the table after the operation (or
  after the hypothetical one). Parameterized SQL only; one transaction.
- **Honesty.** `dry_run` defaults to **true**: a caller that asks for
  nothing gets a report and an untouched table. A real deletion appends
  one line to the service's own audit trail — the learning ledger's
  sibling, `<learning_dir>/LEDGER.md`, is for artifacts, so write instead
  to `<learning_dir>/RETRIEVAL-LEDGER.md` (created on first use, header
  `# Retrieval log ledger`): `- <ISO-8601> | pruned | <n> rows | <oldest>..<newest> | <the filter as given>`.
- A missing database or table is the empty state (`selected` 0), never an
  exception.

### 2.2 CLI

`retrievals prune` as a sub-verb of the existing `retrievals` command
(keep today's listing as the default behaviour of `retrievals` with no
sub-verb, so no existing invocation changes):
`retrievals prune [--older-than X] [--keep-last N] [--run-id X] [--flow-key X] [--agent-role X] [--scope X] [--apply] [--json]`.
Without `--apply` it is a dry run and says so in the first line of its
output; with `--apply` it deletes and prints the same report plus the
ledger line it wrote. Refuse with exit 2 and a clear message when neither
`--older-than` nor `--keep-last` is given.

### 2.3 Route

No route. Deletion is an operator act at the host, not an HTTP call; say
so in one sentence in the README so the omission reads as a decision.

### 2.4 README

`## Retrieval log` gains a `### Pruning` subsection: the two selection
rules and that they intersect, the dry-run default, `--apply`, the
ledger file, and the sentence that there is deliberately no HTTP route.

### 2.5 Tests, named exactly (in `tests/test_retrieval_log_query.py` or a new file)

- `test_prune_requires_a_selection_rule`
- `test_prune_dry_run_reports_without_deleting`
- `test_prune_older_than_and_keep_last_intersect`
- `test_prune_applies_and_writes_one_ledger_line`
- `test_prune_filters_narrow_the_selection_like_query`
- `test_prune_on_a_missing_database_is_the_empty_state`
- `test_cli_retrievals_still_lists_without_a_sub_verb`

Every existing test stays green.

## 3. Constraints

As before: work only here; never modify DPMtF, mcp-light, the shared index
directory or `~/.local/share/knowledge-service/` (the tests use temp
databases and temp learning directories — **never** the operator's log);
no commits; no network; interpreter `/home/svend/DPMtF-WebUI/venv/bin/python`;
no new dependencies; parameterized SQL; en-US; no services or models
touched; `py_compile` every changed file; the 77 existing tests stay green.
The live service on 9140 keeps running and is not restarted by you.

## 4. Definition of Done

```testgoals
id: TG1
what: the seven named tests exist and pass and the whole suite is green
run: cd /home/svend/knowledge-service && for t in prune_requires_a_selection_rule prune_dry_run_reports_without_deleting prune_older_than_and_keep_last_intersect prune_applies_and_writes_one_ledger_line prune_filters_narrow_the_selection_like_query prune_on_a_missing_database_is_the_empty_state cli_retrievals_still_lists_without_a_sub_verb; do grep -rq "def test_$t" tests/ || exit 1; done && PYTHONDONTWRITEBYTECODE=1 /home/svend/DPMtF-WebUI/venv/bin/python -m pytest -q -p no:cacheprovider tests 2>&1 | tail -n 1 | grep -E "passed" | grep -vE "failed|error"
expect: exit 0

id: TG2
what: the function, the sub-verb, the ledger and the README section exist, and no route was added
run: cd /home/svend/knowledge-service && grep -q "def prune_retrievals" knowledge_service/retrieval_log.py && grep -q "RETRIEVAL-LEDGER.md" knowledge_service/retrieval_log.py && grep -q '"prune"' knowledge_service/cli.py && grep -q "^### Pruning" README.md && ! grep -q "/v1/retrievals/prune" knowledge_service/app.py
expect: exit 0

id: TG3
what: the dry-run default holds — a prune call with no --apply leaves a temp database untouched
run: cd /home/svend/knowledge-service && /home/svend/DPMtF-WebUI/venv/bin/python -c "
import os, sqlite3, tempfile, pathlib
d = tempfile.mkdtemp(); db = os.path.join(d, 'k.db')
os.environ['KNOWLEDGE_SERVICE_INI'] = os.path.join(d, 'k.ini')
pathlib.Path(os.environ['KNOWLEDGE_SERVICE_INI']).write_text('[knowledge]\nenabled = true\nprovider = none\nindex_dir = ' + d + '\n\n[service]\ndb_path = ' + db + '\n\n[learning]\ndir = ' + d + '/learning\n')
from knowledge_service import db as kdb, retrieval_log, config
config.reload(); kdb.ensure_schema()
conn = sqlite3.connect(db)
conn.execute(\"INSERT INTO knowledge_retrieval_log (provider, scope, query, created_at) VALUES ('p','s','q','2020-01-01 00:00:00')\"); conn.commit(); conn.close()
r = retrieval_log.prune_retrievals(older_than='30')
assert r['dry_run'] is True and r['selected'] == 1 and r['deleted'] == 0, r
n = sqlite3.connect(db).execute('SELECT COUNT(*) FROM knowledge_retrieval_log').fetchone()[0]
assert n == 1, n
print('ok')"
expect: exit 0

id: TG4
what: FENCE — only the deliverable paths changed
run: cd /home/svend/knowledge-service && test -n "$(git status --porcelain)" && test -z "$(git status --porcelain | awk '{print $2}' | grep -v -E '^(knowledge_service/|tests/|README.md)')"
expect: exit 0
```

## 5. Initial Execution Instruction

`init_project` with `reset: true`; goals for 2.1–2.5 in order; checkpoint
after each goal; ask now, in one message, only what is genuinely ambiguous;
implement; run TG1–TG4 and `py_compile`; record coverage;
`complete_project`; report `git status` and the pasted output of TG1–TG4.
Never touch the operator's database at `~/.local/share/knowledge-service/`.
