"""Windows-readiness proofs for the portable provider path.

Seven named proofs, runnable wherever Python and the portable requirements
run (hermetic: temporary directories only, fake embedders, no network):

1. the package imports and builds its app without ``leann``,
   ``leann_backend_hnsw`` or ``torch`` being importable;
2. the portable modules build paths with ``pathlib`` and avoid POSIX-only
   calls (``chmod``, ``fcntl``, ``os.geteuid``, ``signal``);
3. building a three-document manifest and searching it round-trips through
   the real SQLite store with a fake embedder;
4. the parity script reports an unavailable provider in its summary instead
   of raising;
5. the parity run-scoped INI inherits every operator key except the three
   it overrides;
6. each parity row carries that query's own latency, and the summary the
   mean across queries;
7. a provider whose build raises after a passing preflight is reported as
   ``failed``, the other provider still completes, and the exit code is 0;
8. each provider builds into its own sub-directory under the index dir, so
   neither build can shadow the other's manifest.
"""

from __future__ import annotations

import configparser
import importlib.util
import json
import subprocess
import sys
from functools import partial
from pathlib import Path

import pytest

from knowledge_service import search
from knowledge_service import provider as provider_module
from knowledge_service.portable_provider import PortableProvider

ROOT = Path(__file__).resolve().parent.parent

PARITY_SCRIPT = """
import os
import sys
from pathlib import Path

root = Path(os.environ["KNOWLEDGE_SERVICE_REPO_ROOT"])
sys.path.insert(0, str(root))

BLOCKED = {"leann", "leann_backend_hnsw", "torch"}


class Blocker:
    def find_spec(self, name, path=None, target=None):
        if name in BLOCKED:
            raise ImportError(f"{name} is blocked for this probe")
        return None


sys.meta_path.insert(0, Blocker())

import knowledge_service  # noqa: E402
from knowledge_service.app import create_app  # noqa: E402
from knowledge_service.cli import main  # noqa: E402
from knowledge_service.portable_provider import PortableProvider  # noqa: E402
from knowledge_service.learning import list_pending_drafts  # noqa: E402
from knowledge_service.maintenance import refresh_scope  # noqa: E402

app = create_app()
assert app is not None, "create_app() returned None with provider = portable"
print("imports-ok")
"""


class ReadinessEmbedder:
    """Deterministic embedder: one axis per keyword, equal-length lists."""

    dim = 3

    def embed(self, texts):
        vectors = []
        for text in texts:
            if "apple" in text:
                vectors.append([1.0, 0.0, 0.0])
            elif "banana" in text:
                vectors.append([0.0, 1.0, 0.0])
            else:
                vectors.append([0.0, 0.0, 1.0])
        return vectors


def _parity_module():
    """Load scripts/provider_parity.py from this repository."""
    spec = importlib.util.spec_from_file_location(
        "provider_parity", ROOT / "scripts" / "provider_parity.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_package_imports_without_leann_torch_or_gpu(tmp_path, monkeypatch):
    """The portable path must not lean on leann, its backends or torch."""
    ini = tmp_path / "portable.ini"
    ini.write_text(
        "[knowledge]\n"
        "enabled = true\n"
        "provider = portable\n"
        f"index_dir = {tmp_path / 'index'}\n"
        "\n"
        "[service]\n"
        f"db_path = {tmp_path / 'knowledge.db'}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("KNOWLEDGE_SERVICE_INI", str(ini))
    monkeypatch.setenv("KNOWLEDGE_SERVICE_REPO_ROOT", str(ROOT))

    script = tmp_path / "probe.py"
    script.write_text(PARITY_SCRIPT, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert "imports-ok" in result.stdout


def test_portable_provider_paths_are_pathlib_and_no_posix_only_calls():
    """Portable-path modules stay pathlib-based and free of POSIX-only calls."""
    for name in ("portable_provider.py", "config.py"):
        text = (ROOT / "knowledge_service" / name).read_text(encoding="utf-8")
        assert "from pathlib import Path" in text, name
        assert "os.path.join" not in text, name

    for name in ("portable_provider.py", "learning.py"):
        text = (ROOT / "knowledge_service" / name).read_text(encoding="utf-8")
        for token in ("chmod", "fcntl", "os.geteuid", "signal"):
            assert token not in text, f"{name} references {token!r}"


def test_portable_build_and_search_round_trip_with_a_fake_embedder(tmp_path):
    """Three documents build, store and search back with the right top-1."""
    manifest = tmp_path / "manifest.jsonl"
    records = [
        {"scope": "alpha", "path": "a-apple.md", "content": "apple pie recipe"},
        {"scope": "alpha", "path": "m-banana.md", "content": "banana bread loaf"},
        {"scope": "alpha", "path": "z-apple.md", "content": "apple cider hours"},
    ]
    manifest.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )

    store = tmp_path / "store" / "alpha.portable.db"
    provider = PortableProvider(index_path=store, embedder=ReadinessEmbedder())
    provider.index(str(manifest))
    assert store.is_file()

    results = provider.search("apple", scope="alpha", top_k=2)

    assert [result["path"] for result in results] == ["a-apple.md", "z-apple.md"]
    assert results[0]["scope"] == "alpha"
    assert results[0]["score"] == pytest.approx(1.0)


def test_parity_script_reports_an_unavailable_provider_instead_of_raising(
    tmp_path, monkeypatch, capsys
):
    """A failing preflight becomes `unavailable: <detail>`, not a traceback."""

    class StubLeann:
        def __init__(self, **kwargs):
            pass

        def preflight(self):
            # Resolve at raise time: earlier suites reload the package, and
            # the except clause compares against the re-imported class.
            raise provider_module.ProviderNotReady("no GPU")

        def index(self, source):
            return None

        def update(self, source):
            return None

        def remove(self, source):
            return None

        def search(self, query, *, scope=None, filters=None, top_k=None,
                   token_budget=None):
            return []

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "notes.md").write_text("# Notes\nalpha bravo bridge\n", encoding="utf-8")
    queries = tmp_path / "queries.txt"
    queries.write_text("alpha\nbravo\n", encoding="utf-8")

    index_dir = tmp_path / "index"
    monkeypatch.setattr(
        search,
        "resolve_provider",
        lambda name, scope=None: StubLeann
        if name == "leann"
        else partial(
            PortableProvider,
            index_path=str(index_dir / "portable" / f"{scope}.portable.db"),
            embedder=ReadinessEmbedder(),
        ),
    )

    parity = _parity_module()
    report = parity.parity_report(
        "alpha", str(repo), ["alpha", "bravo"], index_dir=index_dir, top_k=2
    )

    summary = report["summary"]
    assert summary["providers"]["leann"]["unavailable"] == "no GPU"
    assert summary["providers"]["portable"]["store_bytes"] > 0
    row = report["rows"][0]
    assert row["leann_top"] == []
    assert row["portable_top"] == ["notes.md"]

    second_dir = tmp_path / "index-2"
    exit_code = parity.main(
        [
            "--scope", "alpha",
            "--repo", str(repo),
            "--queries", str(queries),
            "--index-dir", str(second_dir),
            "--top-k", "2",
        ]
    )
    printed = capsys.readouterr().out
    assert exit_code == 0
    assert "unavailable: no GPU" in printed
    assert "summary queries=2" in printed


def test_parity_ini_inherits_the_operator_settings(tmp_path, monkeypatch):
    """The run-scoped INI carries the operator's keys, plus three overrides."""
    base = tmp_path / "knowledge.ini"
    base.write_text(
        "[knowledge]\n"
        "enabled = true\n"
        "provider = leann\n"
        "min_free_vram_mib = 1234\n"
        "leann_use_daemon = false\n"
        "\n"
        "[service]\n"
        "token = operator-token\n"
        "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("KNOWLEDGE_SERVICE_INI", str(base))

    parity = _parity_module()
    index_dir = tmp_path / "index"
    index_dir.mkdir()
    written = parity._write_ini(index_dir, "portable")

    parser = configparser.ConfigParser()
    parser.read(written, encoding="utf-8")
    assert parser.get("knowledge", "min_free_vram_mib") == "1234"
    assert parser.get("knowledge", "leann_use_daemon") == "false"
    assert parser.get("knowledge", "provider") == "portable"
    assert parser.get("knowledge", "index_dir") == str(index_dir)
    assert parser.get("service", "db_path") == str(index_dir / "parity-registry.db")


def test_parity_rows_carry_per_query_latencies(tmp_path, monkeypatch):
    """Each row is that query's own measurement; the summary is their mean."""

    class FixedClock:
        def __init__(self, values):
            self._values = iter(values)

        def perf_counter(self):
            return next(self._values)

    class TimedProvider:
        def __init__(self, **kwargs):
            pass

        def preflight(self):
            return None

        def index(self, source):
            return None

        def update(self, source):
            return None

        def remove(self, source):
            return None

        def search(self, query, *, scope=None, filters=None, top_k=None,
                   token_budget=None):
            return [{"path": "notes.md", "score": 1.0}]

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "notes.md").write_text("# Notes\nalpha bravo bridge\n", encoding="utf-8")

    monkeypatch.setattr(search, "resolve_provider", lambda name, scope=None: TimedProvider)
    parity = _parity_module()
    # Per provider run: build start/end, then two query measurements. The
    # first query takes 1 s of clock, the second 2 s, on both providers.
    monkeypatch.setattr(
        parity, "time", FixedClock([1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12, 14])
    )

    report = parity.parity_report(
        "alpha", str(repo), ["alpha", "bravo"], index_dir=tmp_path / "index", top_k=2
    )

    rows = report["rows"]
    assert rows[0]["leann_ms"] == pytest.approx(1000.0)
    assert rows[1]["leann_ms"] == pytest.approx(2000.0)
    assert rows[0]["leann_ms"] != rows[1]["leann_ms"]
    assert rows[0]["portable_ms"] == pytest.approx(1000.0)
    assert rows[1]["portable_ms"] == pytest.approx(2000.0)

    providers = report["summary"]["providers"]
    assert providers["leann"]["mean_ms"] == pytest.approx(1500.0)
    assert providers["portable"]["mean_ms"] == pytest.approx(1500.0)


def test_parity_reports_a_provider_that_fails_during_build(
    tmp_path, monkeypatch, capsys
):
    """A raising build becomes `failed: <class>: <line>`; the other side prints."""

    class BuildFailLeann:
        def __init__(self, **kwargs):
            pass

        def preflight(self):
            return None

        def index(self, source):
            raise RuntimeError("boom")

        def update(self, source):
            raise RuntimeError("boom")

        def remove(self, source):
            return None

        def search(self, query, *, scope=None, filters=None, top_k=None,
                   token_budget=None):
            return []

    class OkPortable:
        def __init__(self, **kwargs):
            pass

        def preflight(self):
            return None

        def index(self, source):
            return None

        def update(self, source):
            return None

        def remove(self, source):
            return None

        def search(self, query, *, scope=None, filters=None, top_k=None,
                   token_budget=None):
            return [{"path": "notes.md", "score": 1.0}]

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "notes.md").write_text("# Notes\nalpha bravo bridge\n", encoding="utf-8")
    queries = tmp_path / "queries.txt"
    queries.write_text("alpha\nbravo\n", encoding="utf-8")

    monkeypatch.setattr(
        search,
        "resolve_provider",
        lambda name, scope=None: BuildFailLeann
        if name == "leann"
        else OkPortable,
    )

    parity = _parity_module()
    exit_code = parity.main(
        [
            "--scope", "alpha",
            "--repo", str(repo),
            "--queries", str(queries),
            "--index-dir", str(tmp_path / "index"),
            "--top-k", "2",
            "--json",
        ]
    )
    printed = capsys.readouterr().out

    assert exit_code == 0
    report = json.loads(printed)
    providers = report["summary"]["providers"]
    assert providers["leann"]["failed"] == "RuntimeError: boom"
    assert "mean_ms" in providers["portable"]
    rows = report["rows"]
    assert len(rows) == 2
    assert all(row["portable_top"] == ["notes.md"] for row in rows)
    assert all(row["leann_top"] == [] for row in rows)


def test_parity_builds_each_provider_in_its_own_directory(tmp_path, monkeypatch):
    """Each provider gets its own sub-directory; no shared manifest, no noop."""

    seen: dict = {}

    class BuildFailLeann:
        def __init__(self, **kwargs):
            pass

        def preflight(self):
            return None

        def index(self, source):
            # Reached only after the indexer wrote the manifest; record it
            # and die, so only the portable sub-directory can still build.
            seen["leann_manifest"] = str(source)
            raise RuntimeError("boom")

        def update(self, source):
            raise RuntimeError("boom")

        def remove(self, source):
            return None

        def search(self, query, *, scope=None, filters=None, top_k=None,
                   token_budget=None):
            return []

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "notes.md").write_text("# Notes\nalpha bravo bridge\n", encoding="utf-8")

    index_dir = tmp_path / "index"
    monkeypatch.setattr(
        search,
        "resolve_provider",
        lambda name, scope=None: BuildFailLeann
        if name == "leann"
        else partial(
            PortableProvider,
            index_path=str(index_dir / "portable" / f"{scope}.portable.db"),
            embedder=ReadinessEmbedder(),
        ),
    )

    parity = _parity_module()
    report = parity.parity_report(
        "alpha", str(repo), ["alpha"], index_dir=index_dir, top_k=2
    )

    # The portable side built its own store in its own sub-directory and
    # answered, even though the LEANN attempt died mid-build.
    rows = report["rows"]
    assert rows[0]["portable_top"] == ["notes.md"]
    assert rows[0]["leann_top"] == []
    assert report["summary"]["providers"]["leann"]["failed"] == "RuntimeError: boom"
    assert report["summary"]["providers"]["portable"]["store_bytes"] > 0
    assert (index_dir / "portable" / "alpha.portable.db").is_file()

    # Each provider's manifest and run-scoped INI sit in its own directory.
    leann_manifest = Path(seen["leann_manifest"])
    assert leann_manifest.is_file()
    assert leann_manifest.parent == index_dir / "leann"
    assert (index_dir / "portable" / "alpha.jsonl").is_file()

    leann_ini = configparser.ConfigParser()
    leann_ini.read(index_dir / "leann" / "parity-leann.ini", encoding="utf-8")
    portable_ini = configparser.ConfigParser()
    portable_ini.read(index_dir / "portable" / "parity-portable.ini", encoding="utf-8")
    assert leann_ini.get("knowledge", "index_dir") == str(index_dir / "leann")
    assert portable_ini.get("knowledge", "index_dir") == str(index_dir / "portable")
    assert leann_ini.get("service", "db_path") != portable_ini.get(
        "service", "db_path"
    )

