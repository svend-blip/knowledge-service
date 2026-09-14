"""Command-line entry point for the standalone knowledge service.

Invoked as ``python -m knowledge_service.cli <command>``. Commands:
``serve``, ``refresh``, ``refresh-all``, ``scopes``, ``grant``, ``revoke``,
``import-registry``, ``download-model``, ``learning`` (with subcommands
``validate``, ``admit``, ``retract``, ``list``, ``rebuild``). Errors are one
clear line on stderr and exit code 1 — never a traceback.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from knowledge_service import config, db
from knowledge_service import maintenance as knowledge_maintenance
from knowledge_service import indexer as knowledge_indexer
from knowledge_service import learning as knowledge_learning
from knowledge_service.provider import ProviderNotReady

__all__ = ["main"]


def _fail(message: str) -> int:
    """Print one clear error line and return the failure exit code."""
    print(f"knowledge-service: error: {message}", file=sys.stderr)
    return 1


# ── commands ────────────────────────────────────────────────────────────


def _cmd_serve(_args: argparse.Namespace) -> int:
    """Run the HTTP service in this process."""
    try:
        import uvicorn

        from knowledge_service.app import app
    except ImportError as exc:
        return _fail(f"cannot start the service: {exc}")

    host = config.get_host()
    port = config.get_port()
    print(f"knowledge-service: serving /v1 on http://{host}:{port}", file=sys.stderr)
    uvicorn.run(app, host=host, port=port)
    return 0


def _cmd_refresh(args: argparse.Namespace) -> int:
    """Refresh one scope from its repository path."""
    scope = args.scope.strip()
    if not scope:
        return _fail("scope must not be empty")
    repo_path = Path(args.repo_path).expanduser()
    if not repo_path.is_dir():
        return _fail(f"repo_path is not an existing directory: {repo_path}")

    try:
        result = knowledge_maintenance.refresh_scope(scope, str(repo_path))
    except (knowledge_maintenance.indexer.RepoExclusionError, OSError) as exc:
        return _fail(str(exc))
    except ProviderNotReady as exc:
        return _fail(str(exc))
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 1

    documents = result.get("documents")
    suffix = f" {documents} document(s)" if documents is not None else ""
    print(f"{scope}\t{result['status']}{suffix}\t{result['manifest']}")
    return 0


def _registry_rows() -> list[tuple[str, str]]:
    """Return ``(scope, repository_path)`` pairs from the index registry."""
    conn = None
    try:
        conn = db.connect()
        rows = conn.execute(
            "SELECT scope, location FROM knowledge_indexes ORDER BY scope"
        ).fetchall()
    finally:
        if conn is not None:
            conn.close()
    return [(row["scope"], row["location"]) for row in rows]


def _cmd_refresh_all(args: argparse.Namespace) -> int:
    """Refresh every registry scope whose recorded repository path exists."""
    pairs = _registry_rows()
    failures = 0
    considered = 0
    for scope, location in pairs:
        if knowledge_maintenance.is_learning_location(location):
            print(
                f"knowledge-service: skip {scope}: learning-managed scope "
                "(rebuilt from its manifest by 'learning rebuild')",
                file=sys.stderr,
            )
            continue
        repo_path = Path(location).expanduser() if location else None
        if repo_path is None or not repo_path.is_dir():
            print(
                f"knowledge-service: skip {scope}: recorded repository path "
                f"'{location}' does not exist",
                file=sys.stderr,
            )
            continue
        considered += 1
        if args.dry_run:
            print(f"{scope}\t{repo_path}")
            continue
        try:
            result = knowledge_maintenance.refresh_scope(scope, str(repo_path))
        except (knowledge_maintenance.indexer.RepoExclusionError, OSError) as exc:
            failures += 1
            print(
                f"knowledge-service: {scope} failed: {exc}", file=sys.stderr
            )
            continue
        except ProviderNotReady as exc:
            failures += 1
            print(
                f"knowledge-service: {scope} failed: {exc}", file=sys.stderr
            )
            continue
        except SystemExit as exc:
            failures += 1
            print(
                f"knowledge-service: {scope} failed: "
                f"{exc.code if exc.code else 'operation failed'}",
                file=sys.stderr,
            )
            continue
        documents = result.get("documents")
        suffix = documents if documents is not None else ""
        print(f"{scope}\t{result['status']}\t{suffix}")

    if args.dry_run:
        return 0
    if failures:
        return _fail(f"{failures} of {considered} scope(s) failed to refresh")
    return 0


def _cmd_scopes(_args: argparse.Namespace) -> int:
    """Print the index registry."""
    conn = None
    try:
        conn = db.connect()
        rows = conn.execute(
            "SELECT scope, provider, status, document_count, updated_at"
            " FROM knowledge_indexes ORDER BY scope"
        ).fetchall()
    finally:
        if conn is not None:
            conn.close()
    for row in rows:
        print(
            f"{row['scope']}\t{row['provider']}\t{row['status']}\t"
            f"{row['document_count']}\t{row['updated_at']}"
        )
    return 0


def _grant_key(args: argparse.Namespace) -> tuple[str, str, str | None]:
    scope = args.scope.strip()
    role = args.agent_role.strip()
    flow = args.flow.strip() if args.flow and args.flow.strip() else None
    if not scope or not role:
        raise ValueError("scope and agent_role must not be empty")
    return scope, role, flow


def _cmd_grant(args: argparse.Namespace) -> int:
    """Record one explicit grant for an internal scope."""
    try:
        scope, role, flow = _grant_key(args)
    except ValueError as exc:
        return _fail(str(exc))

    conn = None
    try:
        conn = db.connect()
        if flow is None:
            existing = conn.execute(
                "SELECT 1 FROM knowledge_scope_grants"
                " WHERE scope = ? AND agent_role = ? AND flow_key IS NULL"
                " LIMIT 1",
                (scope, role),
            ).fetchone()
        else:
            existing = conn.execute(
                "SELECT 1 FROM knowledge_scope_grants"
                " WHERE scope = ? AND agent_role = ? AND flow_key = ?"
                " LIMIT 1",
                (scope, role, flow),
            ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO knowledge_scope_grants (scope, agent_role, flow_key)"
                " VALUES (?, ?, ?)",
                (scope, role, flow),
            )
            conn.commit()
            printed_flow = flow if flow is not None else "(any flow)"
            print(f"granted {scope} to {role} [{printed_flow}]")
        else:
            print(f"grant already present for {scope} / {role}")
        return 0
    except sqlite3.Error as exc:
        return _fail(f"cannot record grant: {exc}")
    finally:
        if conn is not None:
            conn.close()


def _cmd_revoke(args: argparse.Namespace) -> int:
    """Remove the matching grant rows for an internal scope."""
    try:
        scope, role, flow = _grant_key(args)
    except ValueError as exc:
        return _fail(str(exc))

    conn = None
    try:
        conn = db.connect()
        if flow is None:
            cur = conn.execute(
                "DELETE FROM knowledge_scope_grants"
                " WHERE scope = ? AND agent_role = ? AND flow_key IS NULL",
                (scope, role),
            )
        else:
            cur = conn.execute(
                "DELETE FROM knowledge_scope_grants"
                " WHERE scope = ? AND agent_role = ? AND flow_key = ?",
                (scope, role, flow),
            )
        conn.commit()
        print(f"removed {cur.rowcount} grant row(s) for {scope} / {role}")
        return 0
    except sqlite3.Error as exc:
        return _fail(f"cannot remove grant: {exc}")
    finally:
        if conn is not None:
            conn.close()


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def _grant_present(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    """Whether this exact ``(scope, agent_role, flow_key)`` triple is stored."""
    if "flow_key" not in row.keys():
        return False
    if row["flow_key"] is None:
        found = conn.execute(
            "SELECT 1 FROM knowledge_scope_grants"
            " WHERE scope = ? AND agent_role = ? AND flow_key IS NULL"
            " LIMIT 1",
            (row["scope"], row["agent_role"]),
        ).fetchone()
    else:
        found = conn.execute(
            "SELECT 1 FROM knowledge_scope_grants"
            " WHERE scope = ? AND agent_role = ? AND flow_key = ?"
            " LIMIT 1",
            (row["scope"], row["agent_role"], row["flow_key"]),
        ).fetchone()
    return found is not None


_MODEL_FILES = (
    "onnx/model.onnx",
    "tokenizer.json",
    "config.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
)


def _cmd_download_model(args: argparse.Namespace) -> int:
    """Fetch only the files the portable provider reads.

    ``onnx/model.onnx``, ``tokenizer.json`` and the three small tokenizer
    files — nothing else from the repository. The directory and each file's
    size are printed so an operator can see what landed; without a network the
    command prints one clean line and exits 1.
    """
    model_id = (args.model_id or config.get_portable_model_id()).strip()
    if args.model_dir:
        model_dir = Path(args.model_dir).expanduser()
    else:
        model_dir = Path(config.get_portable_model_dir())

    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        return _fail(f"cannot download the model: {exc}")

    try:
        snapshot_download(
            model_id, allow_patterns=list(_MODEL_FILES), local_dir=str(model_dir)
        )
    except Exception as exc:  # offline, missing repo, broken cache: one clean line
        return _fail(f"cannot download {model_id} into {model_dir}: {exc}")

    print(model_dir)
    for name in _MODEL_FILES:
        path = model_dir / name
        if path.is_file():
            print(f"{name}\t{path.stat().st_size} bytes")
        else:
            print(f"{name}\tmissing")
    return 0


def _cmd_import_registry(args: argparse.Namespace) -> int:
    """Copy the four knowledge tables once from another database.

    Idempotent: rows are inserted with their original ids through ``INSERT
    OR IGNORE``, so re-running the import changes nothing. Grants are also
    compared by their exact ``(scope, agent_role, flow_key)`` triple, because
    a ``NULL`` flow key is not deduplicated by the table's ``UNIQUE`` index.
    """
    source = Path(args.source).expanduser()
    if not source.is_file():
        return _fail(f"--from database does not exist: {source}")

    src = None
    tgt = None
    try:
        src = db.connect_read_only(str(source))
        tgt = db.connect()
    except sqlite3.Error as exc:
        if src is not None:
            src.close()
        if tgt is not None:
            tgt.close()
        return _fail(f"cannot open databases: {exc}")

    try:
        totals: list[str] = []
        for table in db.REGISTRY_TABLES:
            source_columns = _table_columns(src, table)
            if not source_columns:
                totals.append(f"{table}: absent in source, skipped")
                continue
            target_columns = set(_table_columns(tgt, table))
            columns = [name for name in source_columns if name in target_columns]
            if not columns:
                totals.append(f"{table}: no shared columns, skipped")
                continue

            column_list = ", ".join(columns)
            placeholders = ", ".join("?" for _ in columns)
            statement = (
                f"INSERT OR IGNORE INTO {table} ({column_list})"
                f" VALUES ({placeholders})"
            )
            before = tgt.total_changes
            for row in src.execute(f"SELECT * FROM {table}").fetchall():
                values = tuple(row[name] for name in columns)
                if table == "knowledge_scope_grants":
                    # The unique triple can hold a NULL ``flow_key``, which
                    # SQLite treats as distinct in a UNIQUE constraint, so the
                    # exact triple is compared here instead of relying on
                    # ``INSERT OR IGNORE`` alone.
                    if _grant_present(tgt, row):
                        continue
                tgt.execute(statement, values)
            totals.append(f"{table}: {tgt.total_changes - before} row(s) inserted")
        tgt.commit()
        for line in totals:
            print(line)
        return 0
    except sqlite3.Error as exc:
        return _fail(f"registry import failed: {exc}")
    finally:
        src.close()
        tgt.close()


def _cmd_learning(args: argparse.Namespace) -> int:
    """Run one ``learning`` subcommand; clean errors, exit 1 on refusal.

    Admission and retraction append one ledger line under the learning
    directory and rebuild the learning manifests and scopes; every action is
    auditable there.
    """
    command = args.learning_command
    try:
        if command == "validate":
            return knowledge_learning.validate_file(args.yaml_path)
        if command == "admit":
            return knowledge_learning.admit(args.yaml_path)
        if command == "retract":
            return knowledge_learning.retract(args.ref)
        if command == "list":
            return knowledge_learning.list_artifacts()
        if command == "rebuild":
            return knowledge_learning.rebuild()
    except ProviderNotReady as exc:
        return _fail(str(exc))
    except OSError as exc:
        return _fail(str(exc))
    return _fail(f"unknown learning command: {command!r}")


# ── parser ──────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="knowledge_service.cli",
        description="Standalone knowledge service command line.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="run the HTTP service")
    serve.set_defaults(handler=_cmd_serve)

    refresh = subparsers.add_parser(
        "refresh", help="refresh one scope from its repository"
    )
    refresh.add_argument("scope", help="scope slug, e.g. flowrunner")
    refresh.add_argument("repo_path", help="repository directory to scan")
    refresh.set_defaults(handler=_cmd_refresh)

    refresh_all = subparsers.add_parser(
        "refresh-all", help="refresh every registered scope"
    )
    refresh_all.add_argument(
        "--from-registry",
        action="store_true",
        help="read scopes and repository paths from knowledge_indexes",
    )
    refresh_all.add_argument(
        "--dry-run", action="store_true", help="list what would refresh"
    )
    refresh_all.set_defaults(handler=_cmd_refresh_all)

    scopes = subparsers.add_parser("scopes", help="list the index registry")
    scopes.set_defaults(handler=_cmd_scopes)

    learning_parser = subparsers.add_parser(
        "learning", help="manage validated learning artifacts"
    )
    learning_sub = learning_parser.add_subparsers(
        dest="learning_command", required=True
    )
    learning_validate = learning_sub.add_parser(
        "validate", help="list the schema violations of one artifact YAML"
    )
    learning_validate.add_argument("yaml_path")
    learning_validate.set_defaults(handler=_cmd_learning)
    learning_admit = learning_sub.add_parser(
        "admit", help="admit one validated artifact into experience"
    )
    learning_admit.add_argument("yaml_path")
    learning_admit.set_defaults(handler=_cmd_learning)
    learning_retract = learning_sub.add_parser(
        "retract", help="move one admitted artifact to history"
    )
    learning_retract.add_argument("ref", help="family/run, e.g. 2000/029")
    learning_retract.set_defaults(handler=_cmd_learning)
    learning_list = learning_sub.add_parser(
        "list", help="list admitted artifacts with topic, level and confidence"
    )
    learning_list.set_defaults(handler=_cmd_learning)
    learning_rebuild = learning_sub.add_parser(
        "rebuild", help="rewrite the learning manifests and rebuild the scopes"
    )
    learning_rebuild.set_defaults(handler=_cmd_learning)

    grant = subparsers.add_parser(
        "grant", help="grant an agent role access to an internal scope"
    )
    grant.add_argument("scope")
    grant.add_argument("agent_role")
    grant.add_argument("--flow", default=None, help="restrict the grant to one flow")
    grant.set_defaults(handler=_cmd_grant)

    revoke = subparsers.add_parser("revoke", help="remove a grant")
    revoke.add_argument("scope")
    revoke.add_argument("agent_role")
    revoke.add_argument("--flow", default=None, help="revoke only this flow's grant")
    revoke.set_defaults(handler=_cmd_revoke)

    import_registry = subparsers.add_parser(
        "import-registry", help="copy the knowledge tables once from another database"
    )
    import_registry.add_argument("--from", dest="source", required=True)
    import_registry.set_defaults(handler=_cmd_import_registry)

    download_model = subparsers.add_parser(
        "download-model", help="fetch the portable provider's ONNX model files"
    )
    download_model.add_argument(
        "--model-id", default=None, help="hugging face model id (default from ini)"
    )
    download_model.add_argument(
        "--model-dir", default=None, help="directory to place the files in"
    )
    download_model.set_defaults(handler=_cmd_download_model)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Run one CLI command and return its exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except SystemExit as exc:  # argparse and _fail already printed the message
        if exc.code is None:
            return 0
        return exc.code if isinstance(exc.code, int) else 1
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        return _fail(str(exc) or exc.__class__.__name__)


if __name__ == "__main__":
    raise SystemExit(main())
