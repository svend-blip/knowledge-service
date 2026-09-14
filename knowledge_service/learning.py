"""Validated learning artifacts: the experience and ecosystem stores.

One YAML document per closed run lives under
``<learning_dir>/<repository>/<family>/<run>.yaml``, where ``<repository>``
is the repository slug of the artifact's ``repository`` field (the same
rule ``GET /v1/scope-for-path`` applies to paths); superseded or retracted
artifacts move to ``<learning_dir>/history/<repository>/<family>/<run>.yaml`` with a
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
from knowledge_service import db
from knowledge_service import maintenance as knowledge_maintenance
from knowledge_service.scopes import scope_for_path

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
    "migrate_legacy_layout",
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
    and ``supersedes`` entries are ``"<repository>/<family>/<run>"`` or the
    legacy ``"<family>/<run>"`` (meaning the same repository as the artifact)
    references; anything else is reported malformed either way. An empty
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
                        f"supersedes entry must be "
                        f"\"repository/family/run\" or \"family/run\", "
                        f"got {entry!r}"
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


def _split_ref(ref) -> tuple[str | None, str, str] | None:
    """Split a reference into ``(repository, family, run)``; ``None`` malformed.

    A three-part ``"<repository>/<family>/<run>"`` reference carries its own
    repository slug; the legacy two-part ``"<family>/<run>"`` means the father
    repository and returns ``None`` for it. Anything else (one part, four or
    more, an empty segment) is malformed.
    """
    if not isinstance(ref, str):
        return None
    parts = [part.strip() for part in ref.split("/")]
    if len(parts) not in (2, 3) or not all(parts):
        return None
    if len(parts) == 3:
        repository, family, run = parts
        return repository, family, run
    family, run = parts
    return None, family, run


def normalise(doc: dict, run_dir: str | None = None) -> tuple[dict, list[str]]:
    """Return ``(copy, sentences)``: the mechanical fixes and one line each.

    Three mechanical slips the decomposers make are fixed on a copy (the
    draft file is never touched): a scalar ``architecture_implications``,
    ``failed_approaches`` or ``important_files`` becomes a one-item list
    (an empty string becomes an empty list); a ``validation.testgoals`` list
    of ids becomes ``"<n>/<n> green"``; integer ``family`` and ``run`` become
    strings — zero-padded to the width of ``run_dir`` when that run-directory
    name is zero-padded, plain strings otherwise. Everything else is carried
    over unchanged. ``validate`` runs on the returned document. Never raises.
    """
    sentences: list[str] = []
    if not isinstance(doc, dict):
        return {}, sentences
    out = dict(doc)

    for key in ("architecture_implications", "failed_approaches",
                "important_files"):
        if key not in out or isinstance(out[key], list):
            continue
        value = out[key]
        if value == "" or value is None:
            out[key] = []
            sentences.append(f"{key}: empty value became an empty list")
        else:
            out[key] = [value]
            sentences.append(
                f"{key}: scalar value wrapped into a one-item list"
            )

    validation = out.get("validation")
    if isinstance(validation, dict) and isinstance(
        validation.get("testgoals"), list
    ):
        count = len(validation["testgoals"])
        rewritten = f"{count}/{count} green"
        out["validation"] = {**validation, "testgoals": rewritten}
        sentences.append(
            f"validation.testgoals: list of {count} ids became"
            f" \"{rewritten}\""
        )

    padded = (
        isinstance(run_dir, str)
        and len(run_dir) > 1
        and run_dir.startswith("0")
    )
    for key in ("family", "run"):
        value = out.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        fixed = str(value).zfill(len(run_dir)) if padded else str(value)
        out[key] = fixed
        if padded:
            sentences.append(
                f"{key}: integer became the padded string \"{fixed}\""
                " matching the run directory name"
            )
        else:
            sentences.append(
                f"{key}: integer became the plain string \"{fixed}\""
            )
    return out, sentences


# ── paths ───────────────────────────────────────────────────────────────


def learning_dir() -> Path:
    """The directory holding artifacts, manifests and the ledger."""
    return Path(config.get_learning_dir()).expanduser()


def _repository_slug(value: str | None) -> str:
    """The scope slug of a repository name (the ``scope-for-path`` rule)."""
    return scope_for_path(value or "")


def _registry_rows() -> list[tuple[str, str]]:
    """``(scope, repository_path)`` rows of registered repository scopes."""
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT scope, repository_path FROM knowledge_indexes "
            "WHERE repository_path <> '' ORDER BY scope"
        ).fetchall()
    finally:
        conn.close()
    return [
        (str(row["scope"]), str(row["repository_path"]))
        for row in rows
        if str(row["scope"]) not in LEARNING_SCOPES
    ]


def _repository_runs_root(repository: str | None) -> Path:
    """The runs root of one repository: ``<repository_path>/.flowrunner``.

    ``[learning] runs_root`` stays the father repository's default only; a
    registered repository lives beside the directory its registry row
    records as ``repository_path``.
    """
    slug = _repository_slug(repository)
    location = ""
    for scope, row_location in _registry_rows():
        if scope == slug:
            location = row_location
            break
    if location:
        return Path(location).expanduser() / ".flowrunner"
    return Path(config.get_learning_runs_root()).expanduser()


def _repository_is_known(repository: str) -> bool:
    """Whether a repository has a registry row or is the father repository."""
    if repository == config.get_scope():
        return True
    return any(scope == repository for scope, _ in _registry_rows())


def draft_path(repository: str | None, family: str, run: str) -> Path:
    """The decomposer's draft file for one run, inside its run directory.

    ``<runs root>/<family>/runs/<run>/LEARNING-DRAFT.yaml``, written by the
    chain at SUCCESS closure with ``admitted_by: pending`` and never moved by
    this service. The runs root follows the repository's registry row
    (``<repository_path>/.flowrunner``); ``None`` means the father repository and
    keeps the configured ``runs_root`` default.
    """
    return (
        _repository_runs_root(repository)
        / str(family)
        / "runs"
        / str(run)
        / "LEARNING-DRAFT.yaml"
    )


def _artifact_path(repository: str | None, family: str, run: str) -> Path:
    return learning_dir() / _repository_slug(repository) / str(family) / f"{run}.yaml"


def _history_path(repository: str | None, family: str, run: str) -> Path:
    return (
        learning_dir()
        / "history"
        / _repository_slug(repository)
        / str(family)
        / f"{run}.yaml"
    )


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


def _move_to_history(
    repository: str | None, family: str, run: str, extra: dict[str, str]
) -> None:
    """Move ``<repository>/<family>/<run>.yaml`` into history with ``extra``."""
    source = _artifact_path(repository, family, run)
    if not source.is_file():
        return
    doc, message = _load_yaml(source)
    if doc is None:
        _warn(message)
        doc = {}
    doc = dict(doc)
    doc.update(extra)
    _dump_yaml(_history_path(repository, family, run), doc)
    source.unlink()


def _check_run_closed(family: str, run: str, repository: str | None = None) -> str:
    """Return an error message when the run is not closed SUCCESS, else ``""``.

    Closure is proven by an ``END-REPORT.md`` whose first ``Status`` line
    contains ``SUCCESS``. The runs directory lives under the repository's own
    runs root. A missing runs directory skips the check with a warning (foreign-machine admission); a present directory without proof is
    a refusal.
    """
    runs_root = _repository_runs_root(repository)
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


def _run_status(family: str, run: str, repository: str | None = None) -> str:
    """The word from the run's first ``Status`` line, else ``missing``."""
    report = draft_path(repository, family, run).parent / "END-REPORT.md"
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


def _ledger_line(
    action: str,
    repository: str | None,
    family: str,
    run: str,
    doc: dict,
    source: str = "",
) -> None:
    """Append one ledger line: stamp, action, repository/family/run, level, ..."""
    validation = doc.get("validation") or {}
    level = validation.get("evidence_level", "") if isinstance(validation, dict) else ""
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ledger = learning_dir() / "LEDGER.md"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    header_needed = not ledger.is_file()
    key = f"{_repository_slug(repository)}/{family}/{run}"
    with ledger.open("a", encoding="utf-8") as handle:
        if header_needed:
            handle.write("# Learning Ledger\n\n")
        handle.write(
            f"- {stamp} | {action} | {key} | {level} | "
            f"{doc.get('admitted_by', '')} | source={source}\n"
        )


def _append_normalised_line(
    repository: str | None, family: str, run: str, sentence: str
) -> None:
    """Append one ``normalised`` ledger line for one mechanical fix."""
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ledger = learning_dir() / "LEDGER.md"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    header_needed = not ledger.is_file()
    key = f"{_repository_slug(repository)}/{family}/{run}"
    action = "normalised"
    with ledger.open("a", encoding="utf-8") as handle:
        if header_needed:
            handle.write("# Learning Ledger\n\n")
        handle.write(f"- {stamp} | {action} | {key} | {sentence}\n")


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


def admit(path: str, strict: bool = False) -> int:
    """Admit one learning artifact; return the process exit code.

    Refusals (exit 1, clean message): a failing schema validation, a
    ``hypothesis`` evidence level, or a run that is not closed SUCCESS. The
    mechanical normalisation runs on the loaded document first unless
    ``strict`` is set; ``strict`` requires it to validate exactly as written.
    On
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
    return admit_document(doc, str(source), strict=strict)


def admit_document(doc: dict, source: str, strict: bool = False) -> int:
    """Run the whole admission on one loaded document; ``source`` is the origin.

    The document-level half of admission, shared by ``admit`` (which loads a
    given file) and ``admit_run`` (which loads a run's draft in place):
    normalise the mechanical slips unless ``strict``, validate, migrate
    legacy-layout artifacts, prove the run closed SUCCESS, apply
    ``supersedes``, write the artifact under its repository slug, append one
    ``normalised`` ledger line per change before the ``admitted`` line
    carrying ``source``, rebuild. The source document itself is never
    modified or moved.
    """
    sentences: list[str] = []
    if not strict:
        doc, sentences = normalise(
            doc, run_dir=Path(source).expanduser().parent.name
        )
    violations = validate(doc)
    if violations:
        for violation in violations:
            print(violation)
        return _fail(f"refusing {source}: {len(violations)} schema violation(s)")

    migrate_legacy_layout()

    family = str(doc["family"])
    run = str(doc["run"])
    repository = _repository_slug(str(doc.get("repository", "")))

    refusal = _check_run_closed(family, run, repository)
    if refusal:
        return _fail(refusal)

    key = f"{repository}/{family}/{run}"
    for ref in doc.get("supersedes") or []:
        parts = _split_ref(ref)
        if parts is None:
            continue  # validate() already rejected malformed references
        older_repository, older_family, older_run = parts
        _move_to_history(
            older_repository or repository,
            older_family,
            older_run,
            {"superseded_by": key},
        )

    _dump_yaml(_artifact_path(repository, family, run), dict(doc))
    for sentence in sentences:
        _append_normalised_line(repository, family, run, sentence)
    _ledger_line("admitted", repository, family, run, doc, source)
    return rebuild()


def admit_run(ref: str, admitted_by: str, strict: bool = False) -> int:
    """Admit the draft of one closed run in place; return the exit code.

    ``ref`` is ``"<repository>/<family>/<run>"``; the legacy two-part
    ``"<family>/<run>"`` means the father repository. The draft is read from
    the run directory (``draft_path``), its ``admitted_by`` is replaced with
    the admitter's name, and exactly the document-level admission path runs
    on it (``normalise`` first unless ``strict``). Refusals (exit 1, clean
    message): a malformed ref, an unknown
    repository, a missing draft, a draft whose ``repository`` field does not
    match the repository it was found under, an empty ``admitted_by``, the
    value ``pending`` in any casing, plus everything ``admit_document``
    refuses. The draft file itself is never touched.
    """
    parts = _split_ref(ref)
    if parts is None:
        return _fail(
            f"reference must be \"repository/family/run\" or \"family/run\", "
            f"got {ref!r}"
        )
    named_repository, family, run = parts
    repository = _repository_slug(named_repository)
    if named_repository and not _repository_is_known(repository):
        return _fail(
            f"unknown repository: {repository};"
            " register its path with set-repository"
        )

    artifact = draft_path(named_repository, family, run)
    if not artifact.is_file():
        return _fail(
            f"no draft for {repository}/{family}/{run}: {artifact} does not exist"
        )

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
    draft_repository = _repository_slug(str(doc.get("repository", "")))
    if draft_repository != repository:
        return _fail(
            f"draft repository \"{draft_repository}\" does not match "
            f"repository \"{repository}\""
        )
    doc = dict(doc)
    doc["admitted_by"] = name
    return admit_document(doc, str(artifact), strict=strict)


def validate_run(ref: str) -> int:
    """Print what admission would normalise, then violations and run status.

    Sentences for the mechanical fixes print under ``would normalise:``
    first; violations of the normalised document then print exactly as
    ``validate_file`` prints them, then one line
    ``run status: <SUCCESS|BLOCKED|…|missing>`` from the END-REPORT's first
    ``Status`` line (Markdown stripped, ``missing`` when there is none). Exit
    code follows the violations.
    """
    parts = _split_ref(ref)
    if parts is None:
        return _fail(
            f"reference must be \"repository/family/run\" or \"family/run\", "
            f"got {ref!r}"
        )
    named_repository, family, run = parts
    repository = _repository_slug(named_repository)
    if named_repository and not _repository_is_known(repository):
        return _fail(
            f"unknown repository: {repository};"
            " register its path with set-repository"
        )

    artifact = draft_path(named_repository, family, run)
    if not artifact.is_file():
        return _fail(
            f"no draft for {repository}/{family}/{run}: {artifact} does not exist"
        )
    doc, message = _load_yaml(artifact)
    if doc is None:
        return _fail(message)

    doc, sentences = normalise(doc, run_dir=run)
    if sentences:
        print("would normalise:")
        for sentence in sentences:
            print(f"- {sentence}")
    violations = validate(doc)
    for violation in violations:
        print(violation)
    print(f"run status: {_run_status(family, run, repository)}")
    return 0 if not violations else 1


def retract(ref: str) -> int:
    """Retract one admitted artifact; return the process exit code.

    ``ref`` takes the same three-part (or legacy two-part) form as the other
    learning commands. The artifact moves to history with ``retracted_at``
    written into it and the manifests and scopes are rebuilt; history stays
    retrievable through ``include_history=true``. Legacy-layout artifacts are
    migrated first, so the retraction finds them under their repository slug.
    """
    parts = _split_ref(ref)
    if parts is None:
        return _fail(
            f"reference must be \"repository/family/run\" or \"family/run\", "
            f"got {ref!r}"
        )
    named_repository, family, run = parts
    repository = _repository_slug(named_repository)
    if named_repository and not _repository_is_known(repository):
        return _fail(
            f"unknown repository: {repository};"
            " register its path with set-repository"
        )

    migrate_legacy_layout()
    source = _artifact_path(repository, family, run)
    if not source.is_file():
        return _fail(
            f"no admitted artifact for {repository}/{family}/{run} "
            f"under {learning_dir()}"
        )
    doc, message = _load_yaml(source)
    if doc is None:
        return _fail(message)

    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _move_to_history(repository, family, run, {"retracted_at": stamp})
    _ledger_line("retracted", repository, family, run, doc, str(source))
    return rebuild()


def list_artifact_records(history: bool = False) -> list[dict]:
    """Return one plain dict per artifact, newest listing data first.

    Live artifacts come from the learning directory, history artifacts from
    its ``history`` subtree. Every record carries ``repository`` (the slug of
    the artifact's repository), ``family``, ``run``,
    ``topic``, ``evidence_level``, ``confidence``, ``admitted_by`` and
    ``supersedes`` (a list); history records additionally carry
    ``superseded_by`` and ``retracted_at`` as string or ``None``. Sorted by
    ``repository``, then ``family``, then ``run``. Pure read: nothing is
    written or rebuilt.
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
            "repository": _repository_slug(str(doc.get("repository", ""))),
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
    records.sort(key=lambda item: (item["repository"], item["family"], item["run"]))
    return records


def list_artifacts() -> int:
    """Print one line per artifact: repository/family/run, topic, level, etc."""
    for record in list_artifact_records():
        print(
            f"{record['repository']}/{record['family']}/{record['run']}\t"
            f"{record['topic']}\t"
            f"{record['evidence_level']}\t{record['confidence']}\t"
            f"{record['admitted_by']}"
        )
    return 0


def list_pending_drafts() -> list[dict]:
    """Return one plain dict per run-directory draft, newest listing first.

    Scans ``<runs root>/*/runs/*/LEARNING-DRAFT.yaml`` under every registered
    repository's runs root (``<repository_path>/.flowrunner``), then under
    the father repository's configured ``runs_root`` when it has no registry
    row. Every record carries ``repository`` (the slug the draft was found
    under), ``family``, ``run``, ``topic``, ``evidence_level``,
    ``run_status`` (the word from the run's END-REPORT, ``missing`` when there
    is none), ``admitted`` (an artifact for that ``repository/family/run``
    exists under the learning directory or its history), ``valid``,
    ``violations`` (the schema violations left after the mechanical
    normalisation) and ``normalisations`` (one sentence per fix ``normalise``
    applies). Sorted by ``family``,
    then ``run``, then ``repository``. A draft that does not parse is listed
    with ``valid`` false, one violation and an empty topic. Missing runs roots
    contribute nothing. Pure read: nothing is written or rebuilt.
    """
    roots: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for scope, location in _registry_rows():
        roots.append((scope, Path(location).expanduser() / ".flowrunner"))
        seen.add(scope)
    father = config.get_scope()
    if father not in seen:
        roots.append(
            (father, Path(config.get_learning_runs_root()).expanduser())
        )

    drafts: list[dict] = []
    for repository, runs_root in roots:
        if not runs_root.is_dir():
            continue
        for artifact in runs_root.glob("*/runs/*/LEARNING-DRAFT.yaml"):
            parts = artifact.relative_to(runs_root).parts
            family, run = str(parts[0]), str(parts[2])
            record: dict = {
                "repository": repository,
                "family": family,
                "run": run,
                "topic": "",
                "evidence_level": "",
                "run_status": _run_status(family, run, repository),
                "admitted": (
                    _artifact_path(repository, family, run).is_file()
                    or _history_path(repository, family, run).is_file()
                ),
            }
            doc, message = _load_yaml(artifact)
            if doc is None:
                _warn(message)
                record.update(
                    {"topic": "", "valid": False, "violations": 1,
                     "normalisations": []}
                )
                drafts.append(record)
                continue
            doc, sentences = normalise(doc, run_dir=run)
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
                    "normalisations": sentences,
                }
            )
            drafts.append(record)

    drafts.sort(
        key=lambda item: (item["family"], item["run"], item["repository"])
    )
    return drafts


def print_drafts() -> int:
    """Print one tab-separated line per draft awaiting or holding admission."""
    for record in list_pending_drafts():
        print(
            f"{record['repository']}/{record['family']}/{record['run']}\t"
            f"{record['topic']}\t"
            f"{record['evidence_level']}\t{record['run_status']}\t"
            f"{'admitted' if record['admitted'] else 'pending'}\t"
            f"{'valid' if record['valid'] else 'invalid'}\t"
            f"{record['violations']}"
        )
    return 0


# ── migration ───────────────────────────────────────────────────────────


def migrate_legacy_layout() -> int:
    """Move legacy two-part artifacts under their repository slug once.

    Legacy files sat at ``<learning_dir>/<family>/<run>.yaml`` and their
    history twins at ``<learning_dir>/history/<family>/<run>.yaml``; the
    three-part layout puts each under the slug of its ``repository`` field.
    Every moved artifact gets exactly one ``migrated`` ledger line; the next
    call finds nothing left to move, so the migration is idempotent. Broken
    documents are skipped with a warning. Returns the number of moved files.
    """
    directory = learning_dir()
    moved = 0
    bases: tuple[tuple[Path, bool], ...] = (
        (directory, False),
        (directory / "history", True),
    )
    for base, is_history in bases:
        if not base.is_dir():
            continue
        for artifact in sorted(base.glob("*/*.yaml"), key=str):
            parts = artifact.relative_to(base).parts
            if parts[0] == "history" and not is_history:
                continue
            doc, message = _load_yaml(artifact)
            if doc is None:
                _warn(message)
                continue
            repository = _repository_slug(str(doc.get("repository", "")))
            family = str(doc.get("family", parts[0]))
            run = str(doc.get("run", artifact.stem))
            target = (
                _history_path(repository, family, run)
                if is_history
                else _artifact_path(repository, family, run)
            )
            if target == artifact:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(artifact.read_bytes())
            artifact.unlink()
            _ledger_line("migrated", repository, family, run, doc, str(artifact))
            moved += 1
    return moved


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
        repository = _repository_slug(str(doc.get("repository", "")))
        path = f"{repository}/{doc.get('family', '')}/{doc.get('run', '')}.yaml"
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
        repository = _repository_slug(str(doc.get("repository", "")))
        path = f"{repository}/{doc.get('family', '')}/{doc.get('run', '')}.yaml"
        origin = (
            f"{repository}/{doc.get('family', '')}/{doc.get('run', '')}/"
            f"{doc.get('topic', '')}"
        )
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
    """Rebuild the learning manifests and refresh their three scopes.

    Legacy-layout artifacts are migrated first, so the manifests are always
    rebuilt from the three-part layout.
    """
    migrate_legacy_layout()
    counts = build_manifests()
    directory = learning_dir()
    for scope in LEARNING_SCOPES:
        knowledge_maintenance.refresh_manifest_scope(
            scope, str(directory / f"{scope}.jsonl")
        )
    return 0
