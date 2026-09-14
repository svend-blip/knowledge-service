"""Keep every test inside its own temporary directories.

The service owns two pieces of operator storage: its SQLite database
(default ``~/.local/share/knowledge-service/knowledge.db``) and the shared
knowledge index directory (default ``~/.local/share/dpmtf/knowledge_index``).
Both are resolved from ``knowledge.ini``, so a test that leaves the environment
alone ends up writing registry rows into the operator's files — visible later
as unexpected ``alpha``/``beta`` rows in the live database.

This conftest removes that possibility for every test:

* the autouse :func:`isolated_service_paths` fixture points
  ``KNOWLEDGE_SERVICE_INI`` at a temporary INI whose ``db_path`` and
  ``index_dir`` are inside pytest's ``tmp_path``, before the test body runs;
* at teardown it compares a size-and-mtime snapshot of the two operator
  locations and fails with the offending path when anything changed.

The helpers are module-level (not nested in the fixture) so a test can exercise
the comparison logic directly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ENV_INI_PATH = "KNOWLEDGE_SERVICE_INI"

# Operator-owned locations the suite must never write to. Derived from the home
# directory instead of a literal path so the guard works on any host.
OPERATOR_DIRS: tuple = (
    Path.home() / ".local/share/knowledge-service",
    Path.home() / ".local/share/dpmtf/knowledge_index",
)


def snapshot_tree(directory: Path) -> dict:
    """Map each file under ``directory`` to its ``(size, mtime_ns)``."""
    snapshot: dict = {}
    if not directory.is_dir():
        return snapshot
    for path in sorted(directory.rglob("*")):
        try:
            if not path.is_file():
                continue
            stat = path.stat()
        except OSError:
            continue
        snapshot[str(path.relative_to(directory))] = (stat.st_size, stat.st_mtime_ns)
    return snapshot


def changed_entries(before: dict, after: dict) -> list:
    """Return relative names whose stamp differs, was added, or disappeared."""
    changed = {name for name, stamp in after.items() if before.get(name) != stamp}
    changed.update(name for name in before if name not in after)
    return sorted(changed)


def _guard_ini(directory: Path) -> str:
    """INI body that keeps both storage locations inside ``directory``."""
    return (
        "[knowledge]\n"
        "enabled = false\n"
        "provider = none\n"
        "scope = dpmtf-webui\n"
        f"index_dir = {directory / 'knowledge_index'}\n"
        "\n"
        "[service]\n"
        "host = 127.0.0.1\n"
        "port = 9140\n"
        f"db_path = {directory / 'knowledge.db'}\n"
        "token =\n"
        "\n"
    )


@pytest.fixture(autouse=True)
def isolated_service_paths(tmp_path, monkeypatch):
    """Isolate one test in ``tmp_path`` and check the operator files after it."""
    ini = tmp_path / "knowledge.ini"
    ini.write_text(_guard_ini(tmp_path), encoding="utf-8")
    monkeypatch.setenv(ENV_INI_PATH, str(ini))

    from knowledge_service import config

    config.reload()

    before = {directory: snapshot_tree(directory) for directory in OPERATOR_DIRS}
    yield ini
    for directory in OPERATOR_DIRS:
        diff = changed_entries(before[directory], snapshot_tree(directory))
        if diff:
            names = ", ".join(str(directory / name) for name in diff)
            raise AssertionError("test wrote outside its temporary directories: " + names)
    config.reload()
