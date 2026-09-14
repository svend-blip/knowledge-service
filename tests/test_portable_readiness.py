"""Windows-readiness proofs for the portable provider path.

Four named proofs, runnable wherever Python and the portable requirements
run (hermetic: temporary directories only, fake embedders, no network):

1. the package imports and builds its app without ``leann``,
   ``leann_backend_hnsw`` or ``torch`` being importable;
2. the portable modules build paths with ``pathlib`` and avoid POSIX-only
   calls (``chmod``, ``fcntl``, ``os.geteuid``, ``signal``);
3. building a three-document manifest and searching it round-trips through
   the real SQLite store with a fake embedder;
4. the parity script reports an unavailable provider in its summary instead
   of raising.
"""

from __future__ import annotations

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
            index_path=str(index_dir / f"{scope}.portable.db"),
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
