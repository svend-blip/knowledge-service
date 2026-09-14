"""Provider parity instrument: one scope, both providers, comparable answers.

Builds a scope twice into one temporary index directory — once with the
``leann`` provider, once with the ``portable`` provider — through the
package's own maintenance path (:func:`knowledge_service.maintenance.refresh_scope`,
which itself resolves the provider through
``knowledge_service.search.resolve_provider``), then runs every query line of
a file against both stores and reports one row per query:

``query | leann_top | portable_top | jaccard@k | leann_ms | portable_ms``

plus a summary line with the mean Jaccard score, the mean answer latency per
provider, the build seconds and the store size per provider. ``--json``
prints the same data as one JSON document.

A provider whose preflight fails (no GPU, no model files) is reported in the
summary as ``unavailable: <detail>`` and its columns stay empty; a provider
whose build or search raises after a passing preflight is reported as
``failed: <exception class>: <first line>``, also with empty columns, and
the other provider's rows and summary still print. The script never raises
for either; it exits 0 when at least one provider completed and 2 when none
did. Everything is written under ``--index-dir`` (a temp directory by
default): manifests, both stores and the temp registry database. The shared
index directory and the real registry are never touched.

Usage::

    python scripts/provider_parity.py --scope dpmtf-webui \
        --repo /path/to/repo --queries queries.txt [--index-dir tmp] \
        [--top-k 5] [--json]
"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import sys
import tempfile
import time
from pathlib import Path

# Running as `python scripts/provider_parity.py` puts only scripts/ on the
# import path; make the package beside it importable without installation.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

sys.dont_write_bytecode = True

from knowledge_service import config, maintenance, search  # noqa: E402
from knowledge_service.provider import ProviderNotReady  # noqa: E402

__all__ = ["jaccard", "parity_report", "main"]

_PROVIDER_KEYS = ("leann", "portable")


def jaccard(first: list[str], second: list[str]) -> float:
    """Jaccard similarity of two result-path lists (sets, order-insensitive).

    Two empty lists agree perfectly and score ``1.0``; otherwise it is the
    size of the intersection over the size of the union.
    """
    left = set(first)
    right = set(second)
    union = left | right
    if not union:
        return 1.0
    return len(left & right) / len(union)


def _write_ini(index_dir: Path, provider_key: str) -> str:
    """Write one run-scoped INI into ``index_dir`` and point the env at it.

    The run starts from the operator's configured INI — the one ``config``
    resolves from ``KNOWLEDGE_SERVICE_INI`` or its default location, or the
    packaged defaults when no file exists — and overrides exactly three
    keys: ``provider``, the ``index_dir`` (always ``index_dir``) and the
    ``db_path`` (a temp registry beside it). Every other operator setting —
    ``min_free_vram_mib``, ``leann_use_daemon``, ``top_k``, the token and
    character budgets, the ``[portable]`` section — carries over unchanged,
    so a preflight on a host with a resident model sees the operator's own
    limits instead of a from-scratch file.
    """
    parser = configparser.ConfigParser()
    base = config.ini_path()
    if base:
        parser.read(base, encoding="utf-8")
    registry = index_dir / "parity-registry.db"
    parser.read_dict(
        {
            "knowledge": {"provider": provider_key, "index_dir": str(index_dir)},
            "service": {"db_path": str(registry)},
        }
    )
    ini_path = index_dir / f"parity-{provider_key}.ini"
    with ini_path.open("w", encoding="utf-8") as handle:
        parser.write(handle)
    os.environ[config.ENV_INI_PATH] = str(ini_path)
    config.reload()
    return str(ini_path)


def _store_size_bytes(index_dir: Path, scope: str) -> int:
    """Total bytes of the store files this scope owns under ``index_dir``."""
    total = 0
    for candidate in maintenance._scope_store_files(index_dir, scope):
        try:
            if candidate.is_dir():
                total += sum(
                    file.stat().st_size
                    for file in candidate.rglob("*")
                    if file.is_file()
                )
            elif candidate.is_file():
                total += candidate.stat().st_size
        except OSError:
            continue
    return total


def _failure_text(exc: Exception) -> str:
    """One-line failure summary: exception class plus the message's first line."""
    lines = str(exc).splitlines()
    detail = lines[0] if lines else ""
    return f"{type(exc).__name__}: {detail}"


def _run_provider(
    provider_key: str,
    scope: str,
    repo_path: str,
    index_dir: Path,
    queries: list[str],
    top_k: int,
) -> dict:
    """Build the scope once and time every query against it."""
    outcome: dict = {
        "top": {query: [] for query in queries},
        "latency_ms": {},
        "build_seconds": 0.0,
        "store_bytes": 0,
        "unavailable": None,
        "failed": None,
    }
    _write_ini(index_dir, provider_key)

    started = time.perf_counter()
    try:
        maintenance.refresh_scope(scope, repo_path)
    except ProviderNotReady as exc:
        outcome["unavailable"] = str(exc)
        return outcome
    except Exception as exc:
        outcome["failed"] = _failure_text(exc)
        return outcome
    outcome["build_seconds"] = time.perf_counter() - started

    try:
        provider_factory = search.resolve_provider(provider_key, scope=scope)
        provider = provider_factory()
        provider.preflight()
        for query in queries:
            started = time.perf_counter()
            results = provider.search(query, scope=scope, top_k=top_k)
            outcome["latency_ms"][query] = (time.perf_counter() - started) * 1000.0
            outcome["top"][query] = [
                str(result.get("path", "")) for result in results
            ]
    except ProviderNotReady as exc:
        outcome["unavailable"] = str(exc)
        outcome["top"] = {query: [] for query in queries}
        outcome["latency_ms"] = {}
    except Exception as exc:
        outcome["failed"] = _failure_text(exc)
        outcome["top"] = {query: [] for query in queries}
        outcome["latency_ms"] = {}

    outcome["store_bytes"] = _store_size_bytes(index_dir, scope)
    return outcome


def parity_report(
    scope: str,
    repo_path: str,
    queries: list[str],
    *,
    index_dir: str | Path | None = None,
    top_k: int = 5,
) -> dict:
    """Build ``scope`` with both providers and compare their answers.

    Returns ``{"scope", "repo", "top_k", "rows", "summary"}``. Each row is
    ``{"query", "leann_top", "portable_top", "jaccard", "leann_ms",
    "portable_ms"}``; the summary holds the mean Jaccard, the mean latency,
    the build seconds and the store size per provider, or the provider's
    ``unavailable`` / ``failed`` detail when its preflight failed or its
    build or search raised. Never raises for either.
    """
    if index_dir is None:
        index_dir = Path(tempfile.mkdtemp(prefix="knowledge-parity-"))
    index_dir = Path(index_dir)
    index_dir.mkdir(parents=True, exist_ok=True)

    previous_ini = config.ini_path() or None
    outcomes: dict[str, dict] = {}
    try:
        for provider_key in _PROVIDER_KEYS:
            outcomes[provider_key] = _run_provider(
                provider_key, scope, str(repo_path), index_dir, queries, top_k
            )
    finally:
        if previous_ini is None:
            os.environ.pop(config.ENV_INI_PATH, None)
        else:
            os.environ[config.ENV_INI_PATH] = previous_ini
        config.reload()

    rows: list[dict] = []
    for query in queries:
        leann_top = outcomes["leann"]["top"][query]
        portable_top = outcomes["portable"]["top"][query]
        available = [
            key
            for key in _PROVIDER_KEYS
            if outcomes[key]["unavailable"] is None
            and outcomes[key]["failed"] is None
        ]
        score = (
            jaccard(leann_top, portable_top)
            if len(available) == len(_PROVIDER_KEYS)
            else None
        )
        rows.append(
            {
                "query": query,
                "leann_top": leann_top,
                "portable_top": portable_top,
                "jaccard": score,
                "leann_ms": outcomes["leann"]["latency_ms"].get(query),
                "portable_ms": outcomes["portable"]["latency_ms"].get(query),
            }
        )

    scored = [row["jaccard"] for row in rows if row["jaccard"] is not None]
    providers: dict[str, dict] = {}
    for provider_key in _PROVIDER_KEYS:
        outcome = outcomes[provider_key]
        if outcome["unavailable"] is not None:
            providers[provider_key] = {"unavailable": outcome["unavailable"]}
        elif outcome["failed"] is not None:
            providers[provider_key] = {"failed": outcome["failed"]}
        else:
            providers[provider_key] = {
                "mean_ms": _mean(list(outcome["latency_ms"].values())),
                "build_seconds": round(outcome["build_seconds"], 3),
                "store_bytes": outcome["store_bytes"],
            }
    return {
        "scope": scope,
        "repo": str(repo_path),
        "top_k": top_k,
        "rows": rows,
        "summary": {
            "queries": len(rows),
            "jaccard_mean": round(_mean(scored), 4) if scored else None,
            "providers": providers,
        },
    }


def _mean(values: list[float]) -> float | None:
    """Arithmetic mean of a list, ``None`` when it is empty."""
    if not values:
        return None
    return sum(values) / len(values)


def _format_ms(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}"


def _format_jaccard(value: float | None) -> str:
    return "-" if value is None else f"{value:.3f}"


def _provider_column(summary: dict, provider_key: str) -> str:
    provider = summary["providers"][provider_key]
    if "unavailable" in provider:
        return f"{provider_key} unavailable: {provider['unavailable']}"
    if "failed" in provider:
        return f"{provider_key} failed: {provider['failed']}"
    return (
        f"{provider_key} mean_ms={_format_ms(provider['mean_ms'])} "
        f"build_s={provider['build_seconds']:.2f} "
        f"store_bytes={provider['store_bytes']}"
    )


def main(argv: list[str] | None = None) -> int:
    """Entry point: parse arguments, run the parity report, print it."""
    parser = argparse.ArgumentParser(
        description=(
            "Build one scope with the leann and the portable provider and "
            "compare their search answers."
        )
    )
    parser.add_argument("--scope", required=True, help="scope name to build")
    parser.add_argument("--repo", required=True, help="repository path to index")
    parser.add_argument(
        "--queries", required=True, help="file with one query per line"
    )
    parser.add_argument(
        "--index-dir",
        help="temporary index directory (default: a fresh mkdtemp)",
    )
    parser.add_argument(
        "--top-k", type=int, default=5, help="results per query (default 5)"
    )
    parser.add_argument(
        "--json", action="store_true", help="print one JSON document instead"
    )
    args = parser.parse_args(argv)

    queries_path = Path(args.queries).expanduser()
    if not queries_path.is_file():
        print(f"provider_parity: error: no such file: {queries_path}", file=sys.stderr)
        return 1
    queries = [
        line.strip()
        for line in queries_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if args.top_k < 1:
        print("provider_parity: error: --top-k must be >= 1", file=sys.stderr)
        return 1

    report = parity_report(
        args.scope, args.repo, queries, index_dir=args.index_dir, top_k=args.top_k
    )

    completed = sum(
        1
        for provider in report["summary"]["providers"].values()
        if "unavailable" not in provider and "failed" not in provider
    )

    if args.json:
        print(json.dumps(report, ensure_ascii=False))
        return 0 if completed else 2

    header = ["query", "leann_top", "portable_top", "jaccard@k", "leann_ms", "portable_ms"]
    lines = [" | ".join(header)]
    for row in report["rows"]:
        lines.append(
            " | ".join(
                [
                    row["query"],
                    ", ".join(row["leann_top"]),
                    ", ".join(row["portable_top"]),
                    _format_jaccard(row["jaccard"]),
                    _format_ms(row["leann_ms"]),
                    _format_ms(row["portable_ms"]),
                ]
            )
        )
    summary = report["summary"]
    mean_jaccard = (
        f"{summary['jaccard_mean']:.3f}"
        if summary["jaccard_mean"] is not None
        else "-"
    )
    lines.append(
        f"summary queries={summary['queries']} jaccard_mean={mean_jaccard}"
        f" | {_provider_column(summary, 'leann')}"
        f" | {_provider_column(summary, 'portable')}"
    )
    print("\n".join(lines))
    return 0 if completed else 2


if __name__ == "__main__":
    raise SystemExit(main())
