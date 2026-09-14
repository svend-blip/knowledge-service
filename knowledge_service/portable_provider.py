"""CPU-only portable knowledge provider.

The ``leann`` provider needs its compiled HNSW backend and a CUDA GPU, so a
Windows FlowApp or a machine without a GPU has nothing to run. This module is
the second provider behind the same
:class:`knowledge_service.provider.KnowledgeProvider` interface: passages and
metadata in one SQLite file per scope, embeddings from a small multilingual
ONNX model on the CPU, cosine similarity over numpy. Slower and simpler, with
the same manifests, the same scopes, and the same result shape.

Module scope stays inside the standard library so importing this module works
on any Python install; ``onnxruntime``, ``tokenizers`` and ``numpy`` are
imported where they are actually needed. Everything the provider needs is pure
Python plus those three, so it runs on a machine without a GPU just as well as
on one with it.

The store is ``<index_dir>/<scope>.portable.db``, deliberately *not* reusing
the ``<scope>.leann`` naming of the LEANN store, so both providers can coexist
side by side in one index directory. Each row is one passage:

``passages(id TEXT PRIMARY KEY, scope TEXT, path TEXT, content TEXT,
embedding BLOB, dim INTEGER, model TEXT)``

``id`` is the stable ``<scope>:<path>`` pair used by the LEANN provider, so one
manifest maps onto either store without translation. The embedding is a
little-endian float32 blob of ``dim`` values written by whichever embedder is
active, and ``model`` records the model id that produced it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from knowledge_service import config
from knowledge_service.provider import KnowledgeProvider, ProviderNotReady

__all__ = ["PortableProvider", "OnnxEmbedder"]

# Embedding call shape, fixed by the scope: 32 passages per ONNX run, at most
# 256 tokens per passage.
BATCH_SIZE = 32
MAX_TOKENS = 256

# LEANN's own default result count, kept identical so both providers answer an
# unbounded search the same way.
DEFAULT_TOP_K = 5

_MODEL_FILES = ("onnx/model.onnx", "tokenizer.json")

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS passages (
    id TEXT PRIMARY KEY,
    scope TEXT,
    path TEXT,
    content TEXT,
    embedding BLOB,
    dim INTEGER,
    model TEXT
)
"""

# Columns a caller may filter on; everything else is metadata in the manifest.
_FILTER_COLUMNS = ("scope", "path", "model")


class OnnxEmbedder:
    """Mean-pooled ONNX embedder for CPU.

    Loads ``<model_dir>/onnx/model.onnx`` with ``onnxruntime`` and
    ``<model_dir>/tokenizer.json`` with ``tokenizers``, encodes each text to at
    most :data:`MAX_TOKENS` tokens, runs batches of :data:`BATCH_SIZE`, means
    the last hidden state over the attention mask, and returns L2-normalised
    float32 vectors. Input names are read from the session, so a model that
    also wants ``token_type_ids`` is fed what it asks for. A pre-built
    ``session``/``tokenizer`` pair can be handed in, which lets a test drive
    the pooling without any model file on disk.
    """

    def __init__(
        self,
        model_dir: str | Path,
        model_id: str = "",
        session: Any = None,
        tokenizer: Any = None,
    ) -> None:
        self.model_dir = Path(model_dir).expanduser()
        self.model_id = model_id or config.get_portable_model_id()
        # A pre-built session/tokenizer pair lets a test drive the pooling with
        # no model files on disk; both are loaded lazily otherwise.
        self._session: Any = session
        self._tokenizer: Any = tokenizer
        self._dim: int | None = None

    # ── lazy session ────────────────────────────────────────────────────

    def _ensure_session(self) -> None:
        """Open the ONNX session and tokenizer unless both are already set."""
        if self._session is not None and self._tokenizer is not None:
            return None
        from onnxruntime import InferenceSession  # lazy: CPU-only dependency
        from tokenizers import Tokenizer

        model_path = self.model_dir / "onnx" / "model.onnx"
        tokenizer_path = self.model_dir / "tokenizer.json"
        if self._session is None:
            self._session = InferenceSession(str(model_path))
        if self._tokenizer is None:
            self._tokenizer = Tokenizer.from_file(str(tokenizer_path))
        return None

    # ── embedding ───────────────────────────────────────────────────────

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one L2-normalised float32 vector per text."""
        import numpy as np  # lazy: available wherever this provider runs

        self._ensure_session()
        vectors: list[list[float]] = []
        for start in range(0, len(texts), BATCH_SIZE):
            batch = texts[start : start + BATCH_SIZE]
            vectors.extend(self._embed_batch(np, batch))
        if vectors and self._dim is None:
            self._dim = len(vectors[0])
        return vectors

    def _embed_batch(self, np: Any, texts: list[str]) -> list[list[float]]:
        """Embed one batch of at most ``BATCH_SIZE`` texts."""
        encoded = []
        for text in texts:
            ids = list(self._tokenizer.encode(text, add_special_tokens=True).ids)
            encoded.append(ids[:MAX_TOKENS] or ids[:1])
        width = max(len(ids) for ids in encoded)

        input_ids = np.zeros((len(encoded), width), dtype=np.int64)
        attention_mask = np.zeros((len(encoded), width), dtype=np.int64)
        for row, ids in enumerate(encoded):
            input_ids[row, : len(ids)] = ids
            attention_mask[row, : len(ids)] = 1

        feeds: dict[str, Any] = {}
        for spec in self._session.get_inputs():
            if spec.name == "input_ids":
                feeds[spec.name] = input_ids
            elif spec.name == "attention_mask":
                feeds[spec.name] = attention_mask
            elif spec.name.endswith("token_type_ids"):
                feeds[spec.name] = np.zeros_like(input_ids)
        hidden = np.asarray(self._session.run(None, feeds)[0], dtype=np.float32)

        mask = attention_mask.astype(np.float32)[..., None]
        sums = (hidden * mask).sum(axis=1)
        # ``mask`` already carries the trailing hidden axis, so this sum is
        # (batch, 1) and divides (batch, hidden) directly. Reshaping it again
        # would broadcast the pooled result to three dimensions.
        counts = np.clip(mask.sum(axis=1), 1e-9, None)
        pooled = sums / counts
        assert pooled.ndim == 2, pooled.shape
        assert pooled.shape[0] == len(texts), (pooled.shape, len(texts))
        norms = np.linalg.norm(pooled, axis=1, keepdims=True)
        pooled = pooled / np.clip(norms, 1e-9, None)
        return [vector.astype(np.float32).tolist() for vector in pooled]

    @property
    def dim(self) -> int:
        """Vector width of this model, measured once on first use."""
        if self._dim is None:
            self.embed(["dimension probe"])
        assert self._dim is not None
        return self._dim


class PortableProvider(KnowledgeProvider):
    """Provider that runs anywhere Python and ``onnxruntime`` run.

    The provider is manifest-driven exactly like
    :class:`~knowledge_service.leann_provider.LeannProvider`: ``index`` reads a
    JSONL manifest written by :mod:`knowledge_service.indexer` (one JSON object
    per line with ``path`` and ``content``, plus an optional ``scope``), embeds
    each passage on the CPU and stores it with its metadata. Unlike the LEANN
    backends, ``update`` and ``remove`` are real operations: the store is plain
    SQLite, so replacing a passage by its stable id and deleting ids both work
    natively.

    ``embedder`` is injectable so tests can hand in a fake: any object with an
    ``embed(texts)`` method returning equal-length lists is enough, and its
    ``dim`` is read when available. With an injected embedder the provider does
    not require ``onnxruntime`` at all; without one it builds an
    :class:`OnnxEmbedder` for ``model_dir`` on first use.
    """

    def __init__(
        self,
        index_path: str | Path | None = None,
        *,
        model_dir: str | Path | None = None,
        embedder: Any = None,
    ) -> None:
        store = index_path if index_path is not None else _default_store_path()
        self._index_path = str(Path(store).expanduser())
        self._model_dir = str(
            Path(model_dir or config.get_portable_model_dir()).expanduser()
        )
        # An explicitly given model directory is checked by ``preflight`` even
        # when an embedder is injected; an inferred one only matters when this
        # provider has to build the embedder itself.
        self._check_model_files = model_dir is not None or embedder is None
        self._embedder = embedder
        self._own_embedder: Any = None

    # ── readiness ───────────────────────────────────────────────────────

    def readiness_detail(self) -> str:
        """Short health-report line about the model directory."""
        return f"model present: {'yes' if self._missing_model_files() == [] else 'no'}"

    def preflight(self) -> None:
        """Require a usable embedder and the model files it reads."""
        reasons: list[str] = []
        if self._embedder is None:
            try:
                import onnxruntime  # noqa: F401 - presence check only
            except ImportError as exc:
                reasons.append(f"onnxruntime is not installed ({exc})")
        elif not callable(getattr(self._embedder, "embed", None)):
            reasons.append("the injected embedder has no embed() method")
        if self._check_model_files:
            missing = self._missing_model_files()
            if missing:
                reasons.append(
                    f"model files missing under {self._model_dir}: "
                    f"{', '.join(missing)}"
                )
        if reasons:
            raise ProviderNotReady(
                "knowledge provider not ready: " + "; ".join(reasons)
            )
        return None

    def _missing_model_files(self) -> list[str]:
        """Names of the model files this provider needs but cannot find."""
        directory = Path(self._model_dir)
        return [name for name in _MODEL_FILES if not (directory / name).is_file()]

    # ── KnowledgeProvider interface ─────────────────────────────────────

    def index(self, source: str) -> None:
        """Index every manifest record into the portable store.

        Each record becomes one row keyed by ``<scope>:<path>``, so a repeated
        ``index`` of the same manifest is an update rather than a duplicate.
        """
        records = self._read_manifest(source)
        model_id = config.get_portable_model_id()
        embedder = self._get_embedder()
        conn = self._connect()
        try:
            for start in range(0, len(records), BATCH_SIZE):
                batch = records[start : start + BATCH_SIZE]
                vectors = embedder.embed([record["content"] for record in batch])
                for record, vector in zip(batch, vectors):
                    self._write_row(conn, record, vector, model_id)
            conn.commit()
        finally:
            conn.close()
        return None

    def update(self, source: str) -> None:
        """Replace the manifest's passages and drop passages it no longer lists.

        The store is plain SQLite, so replacement is native: the same stable id
        is written again with ``INSERT OR REPLACE``. Rows of the manifest's own
        scopes that the manifest no longer mentions are deleted, which is what
        a deleted document needs.
        """
        records = self._read_manifest(source)
        model_id = config.get_portable_model_id()
        embedder = self._get_embedder()
        conn = self._connect()
        try:
            for start in range(0, len(records), BATCH_SIZE):
                batch = records[start : start + BATCH_SIZE]
                vectors = embedder.embed([record["content"] for record in batch])
                for record, vector in zip(batch, vectors):
                    self._write_row(conn, record, vector, model_id)
            self._prune(conn, records)
            conn.commit()
        finally:
            conn.close()
        return None

    def remove(self, source: str) -> None:
        """Delete the manifest's passages from the portable store."""
        records = self._read_manifest(source)
        conn = self._connect()
        try:
            for record in records:
                conn.execute(
                    "DELETE FROM passages WHERE id = ?", (self._passage_id(record),)
                )
            conn.commit()
        finally:
            conn.close()
        return None

    def search(
        self,
        query: str,
        *,
        scope: str | None = None,
        filters: dict[str, Any] | None = None,
        top_k: int | None = None,
        token_budget: int | None = None,
    ) -> list:
        """Cosine search over the stored embeddings of one scope.

        Every stored embedding whose ``scope`` matches is compared with the
        query vector; ``filters`` entries narrow the result set by equality on
        ``scope``, ``path`` or ``model`` (a LEANN-style mapping such as
        ``{"==": value}`` is understood as the same equality). Results carry
        ``path``, ``content``, ``score`` and ``scope``, are ordered by score,
        bounded by ``top_k`` (LEANN's default of 5 when unset) and finally cut
        to ``token_budget`` with the same rule the LEANN provider applies: the
        last included snippet is truncated to fit and nothing is padded.
        """
        query_vector = self._get_embedder().embed([query])[0]
        rows = self._candidate_rows(scope)

        scored: list[dict[str, Any]] = []
        for row in rows:
            if not self._matches_filters(row, filters):
                continue
            vector = _decode_vector(row["embedding"])
            if len(vector) != len(query_vector):
                continue
            scored.append(
                {
                    "path": row["path"],
                    "content": row["content"],
                    "score": float(_cosine(query_vector, vector)),
                    "scope": row["scope"],
                }
            )
        scored.sort(key=lambda item: (-item["score"], str(item["path"])))
        limit = DEFAULT_TOP_K if top_k is None else top_k
        results = scored[:limit]
        return self._apply_token_budget(results, token_budget)

    # ── internal helpers ────────────────────────────────────────────────

    def _get_embedder(self) -> Any:
        """Return the injected embedder, or build the ONNX one on first use."""
        if self._embedder is not None:
            return self._embedder
        if self._own_embedder is None:
            self.preflight()
            self._own_embedder = OnnxEmbedder(
                self._model_dir, config.get_portable_model_id()
            )
        return self._own_embedder

    def _connect(self) -> sqlite3.Connection:
        """Open the store, creating its directory and schema when missing."""
        path = Path(self._index_path)
        if path.parent and not path.parent.exists():
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute(_SCHEMA_SQL)
        return conn

    @staticmethod
    def _write_row(
        conn: sqlite3.Connection, record: dict[str, Any], vector: list[float], model_id: str
    ) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO passages"
            " (id, scope, path, content, embedding, dim, model)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                PortableProvider._passage_id(record),
                record.get("scope", ""),
                record["path"],
                record["content"],
                _encode_vector(vector),
                len(vector),
                model_id,
            ),
        )

    @staticmethod
    def _prune(conn: sqlite3.Connection, records: list[dict[str, Any]]) -> None:
        """Delete rows of the manifest's scopes that the manifest no longer lists."""
        scopes = {record.get("scope", "") for record in records}
        kept = {PortableProvider._passage_id(record) for record in records}
        for scope in sorted(scopes):
            rows = conn.execute(
                "SELECT id FROM passages WHERE scope = ?", (scope,)
            ).fetchall()
            for row in rows:
                if row["id"] not in kept:
                    conn.execute("DELETE FROM passages WHERE id = ?", (row["id"],))

    @staticmethod
    def _read_manifest(source: str) -> list[dict[str, Any]]:
        """Read a JSONL manifest with the LEANN provider's own reader."""
        from knowledge_service.leann_provider import LeannProvider

        return LeannProvider._read_manifest(source)

    @staticmethod
    def _passage_id(record: dict[str, Any]) -> str:
        """Return the stable ``<scope>:<path>`` id of a manifest record."""
        return f"{record.get('scope', '')}:{record['path']}"

    def _candidate_rows(self, scope: str | None) -> list[sqlite3.Row]:
        """Return stored rows, restricted to one scope when given."""
        conn = self._connect()
        try:
            if scope is None:
                return conn.execute(
                    "SELECT id, scope, path, content, embedding, dim, model"
                    " FROM passages"
                ).fetchall()
            return conn.execute(
                "SELECT id, scope, path, content, embedding, dim, model"
                " FROM passages WHERE scope = ?",
                (scope,),
            ).fetchall()
        finally:
            conn.close()

    @staticmethod
    def _matches_filters(row: sqlite3.Row, filters: dict[str, Any] | None) -> bool:
        """Whether a stored row satisfies every equality filter."""
        for field, expected in (filters or {}).items():
            if field not in _FILTER_COLUMNS:
                continue
            value = row[field] if field in row.keys() else None
            if isinstance(expected, dict):
                wanted = expected.get("==", next(iter(expected.values()), None))
                if value != wanted:
                    return False
            elif value != expected:
                return False
        return True

    @staticmethod
    def _apply_token_budget(
        results: list[dict[str, Any]], token_budget: int | None
    ) -> list[dict[str, Any]]:
        """Bound the total whitespace-split tokens of returned content fields."""
        if token_budget is None:
            return results
        budgeted: list[dict[str, Any]] = []
        used = 0
        for result in results:
            tokens = result["content"].split()
            if not tokens:
                budgeted.append(result)
                continue
            remaining = token_budget - used
            if remaining <= 0:
                break
            if len(tokens) <= remaining:
                budgeted.append(result)
                used += len(tokens)
            else:
                truncated = dict(result)
                truncated["content"] = " ".join(tokens[:remaining])
                budgeted.append(truncated)
                used += remaining
                break
        return budgeted


def _default_store_path() -> str:
    """Return ``<index_dir>/<configured scope>.portable.db``."""
    scope = config.get_scope()
    return str(Path(config.get_index_dir()) / f"{scope}.portable.db")


def _encode_vector(vector: list[float]) -> bytes:
    """Pack one embedding as little-endian float32."""
    import struct

    return struct.pack(f"<{len(vector)}f", *vector)


def _decode_vector(blob: Any) -> list[float]:
    """Unpack a stored embedding into a plain list of floats."""
    import struct

    if blob is None:
        return []
    if isinstance(blob, memoryview):
        blob = blob.tobytes()
    count = len(blob) // 4
    return list(struct.unpack(f"<{count}f", blob))


def _cosine(first: list[float], second: list[float]) -> float:
    """Cosine similarity of two equal-length vectors."""
    import numpy as np

    a = np.asarray(first, dtype=np.float32)
    b = np.asarray(second, dtype=np.float32)
    denominator = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denominator <= 0.0:
        return 0.0
    return float(np.dot(a, b) / denominator)
