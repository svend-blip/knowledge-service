"""Validated learning artifacts: the experience and ecosystem stores.

One YAML document per closed run lives under
``<learning_dir>/<family>/<run>.yaml``; superseded or retracted artifacts
move to ``<learning_dir>/history/<family>/<run>.yaml`` with a
``superseded_by`` or ``retracted_at`` field written into them. Three JSONL
manifests are rebuilt from those directories — ``experience`` (one rendered
passage per artifact), ``ecosystem`` (one passage per non-empty
``architecture_implications`` entry) and ``experience-history`` (the same
shape for the history directory) — and each scope is rebuilt through
``maintenance.refresh_manifest_scope``: no repository scan, no change
detection.

Admission is mechanical: ``validate`` lists every schema violation,
``admit`` refuses a failing document, a ``hypothesis`` evidence level, and a
run that is not closed SUCCESS (proven by an ``END-REPORT.md`` whose first
``Status`` line contains ``SUCCESS`` under ``[learning] runs_root``; a
missing runs directory skips the check with a warning so a foreign machine
can still admit by hand). Every admission, supersede and retraction is one
ledger line in ``<learning_dir>/LEDGER.md``. No model is involved: DPMtF's
chain roles produce the drafts and the supervisor admits them.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

from knowledge_service import config
from knowledge_service import maintenance as knowledge_maintenance

__all__ = [
    "EVIDENCE_LEVELS",
    "DEFAULT_EVIDENCE_LEVEL",
    "LEARNING_SCOPES",
    "validate",
    "levels_at_least",
    "learning_dir",
    "draft_path",
    "validate_file",
    "validate_run",
    "admit",
    "admit_document",
    "admit_run",
    "retract",
    "list_artifact_records",
    "list_artifacts",
    "list_pending_drafts",
    "print_drafts",
    "rebuild",
    "build_manifests",
]

# Strongest to weakest, per addendum 2 §4. ``hypothesis`` is deliberately not
# in the list: it is never admissible, so ``validate`` treats it as a
# violation in exactly one place.
EVIDENCE_LEVELS = (
    "tests",
    "measured_runtime",
    "approved_architecture",
    "reviewer_conclusion",
    "observation",
)
DEFAULT_EVIDENCE_LEVEL = "approved_architecture"
CONFIDENCE_LEVELS = ("high", "medium", "low")

# The three non-repository scopes of this service, in rebuild order. Their
# registry rows carry ``location`` under the learning directory, which is how
# ``refresh-all`` tells them apart from repository scopes.
LEARNING_SCOPES = ("experience", "ecosystem", "experience-history")

_REQUIRED_KEYS = (
    "topic",
    "scope",
    "repository",
    "family",
    "run",
    "problem",
    "approach",
    "result",
    "failed_approaches",
    "important_files",
    "architecture_implications",
    "validation",
    "confidence",
    "supersedes",
    "admitted_by",
)
_LIST_KEYS = (
    "failed_approaches",
    "important_files",
    "architecture_implications",
    "supersedes",
)
_VALIDATION_KEYS = ("evidence_level", "verdicts", "testgoals")


# ── schema ──────────────────────────────────────────────────────────────


def validate(doc) -> list[str]:
    """Return every violation of the learning-artifact schema.

    Every key in ``_REQUIRED_KEYS`` is required (empty lists are allowed);
    ``scope`` must be ``experience``, confidence and evidence level come from
    the fixed vocabularies, ``validation`` carries exactly its three keys,
    and ``supersedes`` entries are ``"<family>/<run>"`` references. An empty
    list means the document is valid. Never raises.
    """
    violations: list[str] = []
    if not isinstance(doc, dict):
        return ["document is not a YAML mapping"]

    for key in _REQUIRED_KEYS:
        if key not in doc:
            violations.append(f"missing required key: {key}")

    scope = doc.get("scope")
    if isinstance(scope, str) and scope != "experience":
        violations.append(f"scope must be \"experience\", got {scope!r}")

    confidence = doc.get("confidence")
    if isinstance(confidence, str) and confidence not in CONFIDENCE_LEVELS:
        violations.append(
            "confidence must be one of "
            + ", ".join(CONFIDENCE_LEVELS)
            + f", got {confidence!r}"
        )

    for key in _LIST_KEYS:
        if key not in doc:
            continue
        value = doc[key]
        if not isinstance(value, list):
            violations.append(f"{key} must be a list")
            continue
        if key == "supersedes":
            for entry in value:
                if _split_ref(entry) is None:
                    violations.append(
                        f"supersedes entry must be \"family/run\", got {entry!r}"
                    )

    validation = doc.get("validation")
    if "validation" in doc:
        if not isinstance(validation, dict):
            violations.append("validation must be a mapping")
        else:
            for key in _VALIDATION_KEYS:
                if key not in validation:
                    violations.append(f"missing required key: validation.{key}")
            level = validation.get("evidence_level")
            if isinstance(level, str) and level not in EVIDENCE_LEVELS:
                violations.append(
                    "validation.evidence_level must be one of "
                    + ", ".join(EVIDENCE_LEVELS)
                    + f", got {level!r}; hypothesis is never admitted"
                )
            verdicts = validation.get("verdicts")
            if "verdicts" in validation and not isinstance(verdicts, list):
                violations.append("validation.verdicts must be a list")

    return violations


def levels_at_least(level: str | None) -> list[str]:
    """Return the evidence levels at least as strong as ``level``.

    Strength follows the order of ``EVIDENCE_LEVELS`` (index 0 strongest).
    ``None`` or an unknown level falls back to the configured default
    (``approved_architecture``, i.e. the three strongest levels pass).
    """
    if level in EVIDENCE_LEVELS:
        rank = EVIDENCE_LEVELS.index(level)
    else:
        rank = EVIDENCE_LEVELS.index(DEFAULT_EVIDENCE_LEVEL)
    return list(EVIDENCE_LEVELS[: rank + 1])


def _split_ref(ref) -> tuple[str, str] | None:
    """Split a ``"<family>/<run>"`` reference; ``None`` when malformed."""
    if not isinstance(ref, str) or "/" not in ref:
        return None
    family, _, run = ref.partition("/")
    family, run = family.strip(), run.strip()
    if not family or not run or "/" in run:
        return None
    return family, run


# ── paths ───────────────────────────────────────────────────────────────


def learning_dir() -> Path:
    """The directory holding artifacts, manifests and the ledger."""
    return Path(config.get_learning_dir()).expanduser()


def draft_path(family: str, run: str) -> Path:
    """The decomposer's draft file for one run, inside its run directory.

    ``<runs_root>/<family>/runs/<run>/LEARNING-DRAFT.yaml``, written by the
    chain at SUCCESS closure with ``admitted_by: pending`` and never moved by
    this service.
    """
    runs_root = Path(config.get_learning_runs_root()).expanduser()
    return runs_root / str(family) / "runs" / str(run) / "LEARNING-DRAFT.yaml"


def _artifact_path(family: str, run: str) -> Path:
    return learning_dir() / str(family) / f"{run}.yaml"


def _history_path(family: str, run: str) -> Path:
    return learning_dir() / "history" / str(family) / f"{run}.yaml"


def _iter_artifacts(directory: Path) -> list[Path]:
    """Yaml files under ``directory``, sorted by path, missing dir is empty.

    The first component of every relative path is the family, so the history
    subtree (whose first component is ``history``) is never mixed into the
    live listing. Entries are also skipped when the given directory itself is
    the history directory's parent.
    """
    if not directory.is_dir():
        return []
    paths: list[Path] = []
    for path in directory.rglob("*.yaml"):
        parts = path.relative_to(directory).parts
        if parts and parts[0] == "history" and directory.name != "history":
            continue
        paths.append(path)
    return sorted(paths, key=str)


# ── admission ───────────────────────────────────────────────────────────


def _fail(message: str) -> int:
    """Print one clean error line and return the failure exit code."""
    print(f"learning: error: {message}", file=sys.stderr)
    return 1


def _warn(message: str) -> None:
    print(f"learning: warning: {message}", file=sys.stderr)


def _load_yaml(path: Path) -> tuple[dict | None, str]:
    """Load one artifact YAML; returns ``(doc, error_message)``."""
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return None, f"cannot read {path}: {exc}"
    except yaml.YAMLError as exc:
        return None, f"invalid YAML in {path}: {exc}"
    if not isinstance(loaded, dict):
        return None, f"{path} does not hold a YAML mapping"
    return loaded, ""


def _dump_yaml(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )


def _move_to_history(family: str, run: str, extra: dict[str, str]) -> None:
    """Move ``<family>/<run>.yaml`` into history, writing ``extra`` into it."""
    source = _artifact_path(family, run)
    if not source.is_file():
        return
    doc, message = _load_yaml(source)
    if doc is None:
        _warn(message)
        doc = {}
    doc = dict(doc)
    doc.update(extra)
    _dump_yaml(_history_path(family, run), doc)
    source.unlink()


def _check_run_closed(family: str, run: str) -> str:
    """Return an error message when the run is not closed SUCCESS, else ``""``.

    Closure is proven by an ``END-REPORT.md`` whose first ``Status`` line
    contains ``SUCCESS``. A missing runs directory skips the check with a
    warning (foreign-machine admission); a present directory without proof is
    a refusal.
    """
    runs_root = Path(config.get_learning_runs_root()).expanduser()
    run_dir = runs_root / str(family) / "runs" / str(run)
    if not run_dir.is_dir():
        _warn(
            f"runs directory {run_dir} does not exist; skipping the SUCCESS check"
        )
        return ""
    report = run_dir / "END-REPORT.md"
    if not report.is_file():
        return f"{family}/{run} is not closed SUCCESS: no END-REPORT.md in {run_dir}"
    status_line = ""
    try:
        text = report.read_text(encoding="utf-8")
    except OSError as exc:
        return f"cannot read {report}: {exc}"
    for line in text.splitlines():
        # Real END-REPORTs write the status in Markdown bold — typically
        # ``**Status:** SUCCESS``, sometimes ``**Status: SUCCESS**`` with a
        # trailing note. Strip the decoration characters and whitespace that
        # can precede the word, in any order, then match on the clean line.
        cleaned = line.lstrip("*_`#>- \t")
        if cleaned.startswith("Status"):
            status_line = cleaned
            break
    if not status_line:
        return f"{family}/{run} is not closed SUCCESS: no Status line in {report}"
    if "SUCCESS" not in status_line:
        return f"{family}/{run} is not closed SUCCESS: {status_line}"
    return ""


def _status_word(status_line: str) -> str:
    """The bare status word of one cleaned ``Status`` line.

    ``**Status:** SUCCESS`` and ``**Status: SUCCESS** — run closed.`` both
    yield ``SUCCESS``: everything after a spaced dash is a trailing note, and
    the Markdown decoration around the word is stripped.
    """
    value = status_line.partition(":")[2]
    for separator in (" — ", " – ", " - ", "\t"):
        value = value.split(separator)[0]
    value = value.strip().strip("*_`").strip()
    return value or "missing"


def _run_status(family: str, run: str) -> str:
    """The word from the run's first ``Status`` line, else ``missing``."""
    report = draft_path(family, run).parent / "END-REPORT.md"
    if not report.is_file():
        return "missing"
    try:
        text = report.read_text(encoding="utf-8")
    except OSError as exc:
        _warn(f"cannot read {report}: {exc}")
        return "missing"
    for line in text.splitlines():
        cleaned = line.lstrip("*_`#>- \t")
        if cleaned.startswith("Status"):
            return _status_word(cleaned)
    return "missing"


def _ledger_line(action: str, family: str, run: str, doc: dict, source: str = "") -> None:
    """Append one ledger line: stamp, action, family/run, level, admitted_by, source."""
    validation = doc.get("validation") or {}
    level = validation.get("evidence_level", "") if isinstance(validation, dict) else ""
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ledger = learning_dir() / "LEDGER.md"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    header_needed = not ledger.is_file()
    with ledger.open("a", encoding="utf-8") as handle:
        if header_needed:
            handle.write("# Learning Ledger\n\n")
        handle.write(
            f"- {stamp} | {action} | {family}/{run} | {level} | "
            f"{doc.get('admitted_by', '')} | source={source}\n"
        )


def validate_file(path: str) -> int:
    """Validate one artifact YAML; print violations, return the exit code."""
    artifact = Path(path).expanduser()
    if not artifact.is_file():
        return _fail(f"file does not exist: {artifact}")
    doc, message = _load_yaml(artifact)
    if doc is None:
        return _fail(message)
    violations = validate(doc)
    for violation in violations:
        print(violation)
    return 0 if not violations else 1


def admit(path: str) -> int:
    """Admit one learning artifact; return the process exit code.

    Refusals (exit 1, clean message): a failing schema validation, a
    ``hypothesis`` evidence level, or a run that is not closed SUCCESS. On
    success the artifact is written under the learning directory, every name
    in ``supersedes`` moves to history with ``superseded_by``, one ledger line
    is appended, and both live manifests plus their scopes are rebuilt (the
    history manifest too, so the moved artifact stays retrievable as
    history).
    """
    source = Path(path).expanduser()
    if not source.is_file():
        return _fail(f"file does not exist: {source}")
    doc, message = _load_yaml(source)
    if doc is None:
        return _fail(message)
    return admit_document(doc, str(source))


def admit_document(doc: dict, source: str) -> int:
    """Run the whole admission on one loaded document; ``source`` is the origin.

    The document-level half of admission, shared by ``admit`` (which loads a
    given file) and ``admit_run`` (which loads a run's draft in place):
    validate, prove the run closed SUCCESS, apply ``supersedes``, write the
    artifact, append one ledger line carrying ``source``, rebuild. The source
    document itself is never modified or moved.
    """
    violations = validate(doc)
    if violations:
        for violation in violations:
            print(violation)
        return _fail(f"refusing {source}: {len(violations)} schema violation(s)")

    family = str(doc["family"])
    run = str(doc["run"])

    refusal = _check_run_closed(family, run)
    if refusal:
        return _fail(refusal)

    for ref in doc.get("supersedes") or []:
        parts = _split_ref(ref)
        if parts is None:
            continue  # validate() already rejected malformed references
        older_family, older_run = parts
        _move_to_history(older_family, older_run, {"superseded_by": f"{family}/{run}"})

    _dump_yaml(_artifact_path(family, run), dict(doc))
    _ledger_line("admitted", family, run, doc, source)
    return rebuild()


def admit_run(ref: str, admitted_by: str) -> int:
    """Admit the draft of one closed run in place; return the exit code.

    ``ref`` is ``"<family>/<run>"``; the draft is read from the run directory
    (``draft_path``), its ``admitted_by`` is replaced with the admitter's
    name, and exactly the document-level admission path runs on it. Refusals
    (exit 1, clean message): a malformed ref, a missing draft, an empty
    ``admitted_by``, the value ``pending`` in any casing, plus everything
    ``admit_document`` refuses. The draft file itself is never touched.
    """
    parts = _split_ref(ref)
    if parts is None:
        return _fail(f"reference must be \"family/run\", got {ref!r}")
    family, run = parts

    artifact = draft_path(family, run)
    if not artifact.is_file():
        return _fail(f"no draft for {family}/{run}: {artifact} does not exist")

    name = (admitted_by or "").strip()
    if not name:
        return _fail("--admitted-by must name the admitter and cannot be empty")
    if name.lower() == "pending":
        return _fail(
            "--admitted-by must name the admitter, not the placeholder "
            "\"pending\"; pass the supervisor's name"
        )

    doc, message = _load_yaml(artifact)
    if doc is None:
        return _fail(message)
    doc = dict(doc)
    doc["admitted_by"] = name
    return admit_document(doc, str(artifact))


def validate_run(ref: str) -> int:
    """Print one run's draft violations and its run status; never admits.

    Violations print exactly as ``validate_file`` prints them, then one line
    ``run status: <SUCCESS|BLOCKED|…|missing>`` from the END-REPORT's first
    ``Status`` line (Markdown stripped, ``missing`` when there is none). Exit
    code follows the violations.
    """
    parts = _split_ref(ref)
    if parts is None:
        return _fail(f"reference must be \"family/run\", got {ref!r}")
    family, run = parts

    artifact = draft_path(family, run)
    if not artifact.is_file():
        return _fail(f"no draft for {family}/{run}: {artifact} does not exist")
    doc, message = _load_yaml(artifact)
    if doc is None:
        return _fail(message)

    violations = validate(doc)
    for violation in violations:
        print(violation)
    print(f"run status: {_run_status(family, run)}")
    return 0 if not violations else 1


def retract(ref: str) -> int:
    """Retract one admitted artifact; return the process exit code.

    The artifact moves to history with ``retracted_at`` written into it and
    the manifests and scopes are rebuilt; history stays retrievable through
    ``include_history=true``.
    """
    parts = _split_ref(ref)
    if parts is None:
        return _fail(f"reference must be \"family/run\", got {ref!r}")
    family, run = parts
    source = _artifact_path(family, run)
    if not source.is_file():
        return _fail(f"no admitted artifact for {family}/{run} under {learning_dir()}")
    doc, message = _load_yaml(source)
    if doc is None:
        return _fail(message)

    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _move_to_history(family, run, {"retracted_at": stamp})
    _ledger_line("retracted", family, run, doc, str(source))
    return rebuild()


def list_artifact_records(history: bool = False) -> list[dict]:
    """Return one plain dict per artifact, newest listing data first.

    Live artifacts come from the learning directory, history artifacts from
    its ``history`` subtree. Every record carries ``family``, ``run``,
    ``topic``, ``evidence_level``, ``confidence``, ``admitted_by`` and
    ``supersedes`` (a list); history records additionally carry
    ``superseded_by`` and ``retracted_at`` as string or ``None``. Sorted by
    ``family``, then ``run``. Pure read: nothing is written or rebuilt.
    """
    directory = learning_dir()
    if history:
        directory = directory / "history"
    records: list[dict] = []
    for artifact in _iter_artifacts(directory):
        doc, message = _load_yaml(artifact)
        if doc is None:
            _warn(message)
            continue
        validation = doc.get("validation") or {}
        level = validation.get("evidence_level", "") if isinstance(validation, dict) else ""
        record = {
            "family": str(doc.get("family", "")),
            "run": str(doc.get("run", "")),
            "topic": str(doc.get("topic", "")),
            "evidence_level": str(level),
            "confidence": str(doc.get("confidence", "")),
            "admitted_by": str(doc.get("admitted_by", "")),
            "supersedes": [str(item) for item in (doc.get("supersedes") or [])],
        }
        if history:
            record["superseded_by"] = (
                str(doc["superseded_by"]) if doc.get("superseded_by") else None
            )
            record["retracted_at"] = (
                str(doc["retracted_at"]) if doc.get("retracted_at") else None
            )
        records.append(record)
    records.sort(key=lambda item: (item["family"], item["run"]))
    return records


def list_artifacts() -> int:
    """Print one line per admitted artifact: family/run, topic, level, etc."""
    for record in list_artifact_records():
        print(
            f"{record['family']}/{record['run']}\t{record['topic']}\t"
            f"{record['evidence_level']}\t{record['confidence']}\t"
            f"{record['admitted_by']}"
        )
    return 0


def list_pending_drafts() -> list[dict]:
    """Return one plain dict per run-directory draft, newest listing first.

    Scans ``<runs_root>/*/runs/*/LEARNING-DRAFT.yaml``; every record carries
    ``family``, ``run``, ``topic``, ``evidence_level``, ``run_status`` (the
    word from the run's END-REPORT, ``missing`` when there is none),
    ``admitted`` (an artifact for that ``family/run`` exists under the
    learning directory or its history), ``valid`` and ``violations`` (the
    number of schema violations). Sorted by ``family``, then ``run``. A draft
    that does not parse is listed with ``valid`` false, one violation and an
    empty topic. A missing runs root is an empty list. Pure read: nothing is
    written or rebuilt.
    """
    runs_root = Path(config.get_learning_runs_root()).expanduser()
    if not runs_root.is_dir():
        return []

    drafts: list[dict] = []
    for artifact in runs_root.glob("*/runs/*/LEARNING-DRAFT.yaml"):
        parts = artifact.relative_to(runs_root).parts
        family, run = str(parts[0]), str(parts[2])
        record: dict = {
            "family": family,
            "run": run,
            "topic": "",
            "evidence_level": "",
            "run_status": _run_status(family, run),
            "admitted": (
                _artifact_path(family, run).is_file()
                or _history_path(family, run).is_file()
            ),
        }
        doc, message = _load_yaml(artifact)
        if doc is None:
            _warn(message)
            record.update({"topic": "", "valid": False, "violations": 1})
            drafts.append(record)
            continue
        violations = validate(doc)
        validation = doc.get("validation") or {}
        level = (
            validation.get("evidence_level", "")
            if isinstance(validation, dict)
            else ""
        )
        record.update(
            {
                "topic": str(doc.get("topic", "")),
                "evidence_level": str(level),
                "valid": not violations,
                "violations": len(violations),
            }
        )
        drafts.append(record)

    drafts.sort(key=lambda item: (item["family"], item["run"]))
    return drafts


def print_drafts() -> int:
    """Print one tab-separated line per draft awaiting or holding admission."""
    for record in list_pending_drafts():
        print(
            f"{record['family']}/{record['run']}\t{record['topic']}\t"
            f"{record['evidence_level']}\t{record['run_status']}\t"
            f"{'admitted' if record['admitted'] else 'pending'}\t"
            f"{'valid' if record['valid'] else 'invalid'}\t"
            f"{record['violations']}"
        )
    return 0


# ── manifests ───────────────────────────────────────────────────────────


def _render_experience(doc: dict) -> str:
    """Render one artifact into the experience passage text."""
    validation = doc.get("validation") or {}
    lines = [
        str(doc.get("topic", "")),
        f"Problem: {doc.get('problem', '')}",
        f"Approach: {doc.get('approach', '')}",
        f"Result: {doc.get('result', '')}",
    ]
    failed = doc.get("failed_approaches") or []
    lines.append("Failed approaches:")
    lines.extend(f"- {item}" for item in failed)
    files = doc.get("important_files") or []
    lines.append("Important files:")
    lines.extend(f"- {item}" for item in files)
    if isinstance(validation, dict):
        lines.append(
            "Validation: evidence "
            f"{validation.get('evidence_level', '')}; verdicts "
            f"{', '.join(str(v) for v in validation.get('verdicts') or [])}; "
            f"testgoals {validation.get('testgoals', '')}"
        )
    return "\n".join(lines)


def _passage_metadata(doc: dict, scope_name: str, path: str) -> dict:
    """Passage metadata common to every learning scope."""
    validation = doc.get("validation") or {}
    level = validation.get("evidence_level", "") if isinstance(validation, dict) else ""
    return {
        "scope": scope_name,
        "path": path,
        "evidence_level": str(level),
        "repository": str(doc.get("repository", "")),
        "family": str(doc.get("family", "")),
        "run": str(doc.get("run", "")),
        "confidence": str(doc.get("confidence", "")),
    }


def _history_extra(doc: dict, metadata: dict) -> dict:
    """History passages also carry superseded_by / retracted_at."""
    metadata = dict(metadata)
    if doc.get("superseded_by"):
        metadata["superseded_by"] = str(doc["superseded_by"])
    if doc.get("retracted_at"):
        metadata["retracted_at"] = str(doc["retracted_at"])
    return metadata


def _write_manifest(manifest_path: Path, records: list[dict]) -> int:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return len(records)


def _artifact_records(directory: Path, scope_name: str) -> list[dict]:
    """Render every artifact under ``directory`` into manifest records."""
    records: list[dict] = []
    for artifact in _iter_artifacts(directory):
        doc, message = _load_yaml(artifact)
        if doc is None:
            _warn(message)
            continue
        path = f"{doc.get('family', '')}/{doc.get('run', '')}.yaml"
        metadata = _passage_metadata(doc, scope_name, path)
        if scope_name == "experience-history":
            metadata = _history_extra(doc, metadata)
        records.append(
            {
                "scope": scope_name,
                "path": path,
                "content": _render_experience(doc),
                "metadata": metadata,
            }
        )
    return records


def _ecosystem_records(directory: Path) -> list[dict]:
    """One passage per non-empty architecture implication of live artifacts."""
    records: list[dict] = []
    for artifact in _iter_artifacts(directory):
        doc, message = _load_yaml(artifact)
        if doc is None:
            _warn(message)
            continue
        implications = [
            str(item)
            for item in (doc.get("architecture_implications") or [])
            if str(item).strip()
        ]
        path = f"{doc.get('family', '')}/{doc.get('run', '')}.yaml"
        origin = f"{doc.get('family', '')}/{doc.get('run', '')}/{doc.get('topic', '')}"
        for index, implication in enumerate(implications, 1):
            metadata = _passage_metadata(doc, "ecosystem", f"{path}#{index}")
            metadata["origin"] = origin
            records.append(
                {
                    "scope": "ecosystem",
                    "path": f"{path}#{index}",
                    "content": f"{implication}\nOrigin: {origin}",
                    "metadata": metadata,
                }
            )
    return records


def build_manifests() -> dict[str, int]:
    """Rewrite the three learning manifests from the artifact directories.

    Returns ``{scope: passage_count}``. The manifests sit beside the
    artifacts under the learning directory; nothing is read from a repository.
    """
    directory = learning_dir()
    history = directory / "history"
    counts = {
        "experience": _write_manifest(
            directory / "experience.jsonl", _artifact_records(directory, "experience")
        ),
        "ecosystem": _write_manifest(
            directory / "ecosystem.jsonl", _ecosystem_records(directory)
        ),
    }
    counts["experience-history"] = _write_manifest(
        directory / "experience-history.jsonl",
        _artifact_records(history, "experience-history"),
    )
    return counts


def rebuild() -> int:
    """Rebuild the learning manifests and refresh their three scopes."""
    counts = build_manifests()
    directory = learning_dir()
    for scope in LEARNING_SCOPES:
        knowledge_maintenance.refresh_manifest_scope(
            scope, str(directory / f"{scope}.jsonl")
        )
    return 0
