"""LEANN-backed knowledge provider.

This module is the only place in the package where LEANN-specific names may
appear. The ``leann`` dependency is imported lazily, inside the helper
``_import_leann``, so importing this module never puts ``leann`` into
``sys.modules`` and the service keeps importing and running when LEANN is
absent. ``config`` here is this package's own configuration module
(:mod:`knowledge_service.config`), never another project's.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from knowledge_service import config

from knowledge_service.provider import KnowledgeProvider, ProviderNotReady

__all__ = ["LeannProvider"]

# Cached handle to the optional ``leann`` package. ``None`` means "not yet
# imported". Kept at module level so repeated calls share one import.
_LEANN_MODULE: Any | None = None


def _import_leann() -> Any:
    """Import and cache the optional ``leann`` package on first use.

    This helper is the only place in the module that performs the lazy
    ``import leann``. If LEANN is not installed the caller receives a clear
    en-US ``ImportError``.
    """
    global _LEANN_MODULE
    if _LEANN_MODULE is not None:
        return _LEANN_MODULE
    try:
        import leann  # Deliberate lazy import; keep LEANN out of module scope.
    except ImportError as exc:
        raise ImportError(
            "LEANN is not installed; install the approved leann dependency "
            "before using LeannProvider"
        ) from exc
    _LEANN_MODULE = leann
    return _LEANN_MODULE


class LeannProvider(KnowledgeProvider):
    """Knowledge retrieval provider backed by the optional ``leann`` package.

    The provider is manifest-driven: ``index`` accepts the path of a JSONL
    manifest written by ``knowledge_service.indexer`` (one JSON object per
    line with at least ``path`` and ``content`` keys, plus an optional
    ``scope``). The manifest records are handed to LEANN's builder with
    stable passage ids derived from ``scope`` and ``path``, and the manifest
    ``path``/``scope`` values are stored in each passage's metadata so search
    results can carry usable source references.

    The LEANN index is stored next to the manifest as ``<manifest>.leann``
    unless ``index_path`` is supplied at construction. After ``index``, the
    provider remembers that index path and ``search`` queries it. Calling
    ``index`` with a different manifest switches the provider to that new
    index.

    The installed LEANN registers only the ``hnsw`` and ``diskann``
    backends, whose update operation only appends new passages and which
    expose no standalone remove. The ``ivf`` backend is the only one whose
    native update can remove and re-insert passages in place, so ``update``
    and ``remove`` delegate to it only when ``ivf`` is actually registered
    in ``leann.BACKEND_REGISTRY``. Otherwise those methods raise
    ``NotImplementedError`` instead of faking success.
    """

    def __init__(
        self,
        *,
        backend_name: str = "hnsw",
        embedding_model: str = "facebook/contriever",
        index_path: str | Path | None = None,
        backend_kwargs: dict[str, Any] | None = None,
        searcher_kwargs: dict[str, Any] | None = None,
    ) -> None:
        self._backend_name = backend_name
        self._embedding_model = embedding_model
        self._backend_kwargs = dict(backend_kwargs or {})
        self._searcher_kwargs = dict(searcher_kwargs or {})
        self._configured_index_path = (
            None
            if index_path is None
            else str(Path(index_path).expanduser().resolve())
        )
        # The active index searched by ``search``. Initialized from the
        # configured path (a caller may point at a prebuilt index) and
        # updated by ``index``.
        self._index_path = self._configured_index_path

    # ── KnowledgeProvider interface ────────────────────────────────────

    def preflight(self) -> None:
        """Refuse to run when the GPU is absent or too busy for LEANN."""
        import torch  # lazy: never at module scope
        if not torch.cuda.is_available():
            raise ProviderNotReady(
                "knowledge provider not ready: no CUDA device is available"
            )
        free_mib = torch.cuda.mem_get_info()[0] // (1024 * 1024)
        min_mib = config.get_min_free_vram_mib()
        if free_mib < min_mib:
            raise ProviderNotReady(
                "knowledge provider not ready: free GPU memory "
                f"{free_mib} MiB is below the configured minimum {min_mib} MiB"
            )
        return None

    def index(self, source: str) -> None:
        """Index the JSONL manifest at ``source`` into a LEANN store.

        The manifest is read record by record (never rescanned from a
        repository) and each record's ``content`` is handed to LEANN's
        builder with ``scope`` and ``path`` kept in the passage metadata.
        """
        self.preflight()
        records = self._read_manifest(source)
        leann = _import_leann()
        builder = self._new_builder(leann)
        for record in records:
            builder.add_text(record["content"], metadata=self._metadata_for(record))
        index_path = self._resolve_index_path(source)
        builder.build_index(index_path)
        self._index_path = index_path
        return None

    def update(self, source: str) -> None:
        """Update previously indexed knowledge for the manifest at ``source``.

        The installed LEANN backends (``hnsw``/``diskann``) cannot replace
        passages previously indexed from the same manifest: their native
        update only appends new passages and LEANN exposes no standalone
        remove. The ``ivf`` backend supports remove + re-insert, so this
        method delegates to it only when ``ivf`` is registered in
        ``leann.BACKEND_REGISTRY``; otherwise it raises
        ``NotImplementedError`` instead of faking success.
        """
        if self._backend_name != "ivf":
            raise NotImplementedError(
                "LEANN update is not supported for backend "
                f"{self._backend_name!r}: the installed LEANN backends "
                "(hnsw/diskann) cannot replace passages previously indexed "
                "from the same manifest"
            )
        leann = _import_leann()
        if "ivf" not in getattr(leann, "BACKEND_REGISTRY", {}):
            raise NotImplementedError(
                "LEANN update is not supported: the installed LEANN does not "
                "register the ivf backend required for remove + re-insert"
            )
        records = self._read_manifest(source)
        builder = self._new_builder(leann)
        for record in records:
            builder.add_text(record["content"], metadata=self._metadata_for(record))
        index_path = self._resolve_index_path(source)
        builder.update_index(
            index_path, remove_passage_ids=self._passage_ids(records)
        )
        self._index_path = index_path
        return None

    def remove(self, source: str) -> None:
        """Remove the manifest's passages from the LEANN store.

        The installed LEANN backends (``hnsw``/``diskann``) expose no
        standalone remove operation. The only native removal path is
        ``LeannBuilder.update_index`` with ``remove_passage_ids``, which
        requires the ``ivf`` backend. This method delegates to it only when
        ``ivf`` is registered in ``leann.BACKEND_REGISTRY``; otherwise it
        raises ``NotImplementedError`` instead of faking success.
        """
        if self._backend_name != "ivf":
            raise NotImplementedError(
                "LEANN remove is not supported for backend "
                f"{self._backend_name!r}: the installed LEANN backends "
                "(hnsw/diskann) expose no standalone remove operation"
            )
        leann = _import_leann()
        if "ivf" not in getattr(leann, "BACKEND_REGISTRY", {}):
            raise NotImplementedError(
                "LEANN remove is not supported: the installed LEANN does not "
                "register the ivf backend required for passage removal"
            )
        records = self._read_manifest(source)
        builder = self._new_builder(leann)
        builder.update_index(
            self._resolve_index_path(source),
            remove_passage_ids=self._passage_ids(records),
        )
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
        """Return knowledge results relevant to ``query``.

        Each result is a ``dict`` with at least ``path`` (the manifest path
        value) and ``content`` (the passage text), plus ``score`` and
        ``scope`` when available.

        ``top_k`` bounds the number of returned results; when it is ``None``
        LEANN's own default of 5 applies. ``token_budget`` bounds the total
        whitespace-split token count of the returned ``content`` fields: the
        last included snippet is truncated to fit the remaining budget and no
        padding is ever added.

        ``scope`` is applied as an equality filter on the ``scope`` metadata
        field (an explicit ``scope`` argument wins over a ``scope`` entry in
        ``filters``). ``filters`` entries with scalar values are applied as
        equality matches; entries whose value is already a mapping are passed
        through to LEANN's ``metadata_filters`` operator syntax unchanged.
        """
        self.preflight()
        if self._index_path is None:
            raise RuntimeError(
                "no LEANN index is available: call index() before search() "
                "or construct LeannProvider with an index_path"
            )
        leann = _import_leann()
        searcher = leann.LeannSearcher(self._index_path, **self._searcher_kwargs)
        native_top_k = 5 if top_k is None else top_k
        metadata_filters = self._build_metadata_filters(scope, filters)
        try:
            hits = searcher.search(
                query,
                top_k=native_top_k,
                metadata_filters=metadata_filters,
            )
        finally:
            try:
                searcher.cleanup()
            except Exception as exc:  # defensive: never mask search results
                print(
                    "knowledge_service.leann_provider: warning: searcher cleanup "
                    f"failed: {exc}",
                    file=sys.stderr,
                )
        results = [self._map_hit(hit) for hit in hits]
        if top_k is not None:
            results = results[:top_k]
        return self._apply_token_budget(results, token_budget)

    # ── Internal helpers ───────────────────────────────────────────────

    def _new_builder(self, leann: Any) -> Any:
        """Build a fresh LEANN builder using the configured backend."""
        return leann.LeannBuilder(
            self._backend_name,
            embedding_model=self._embedding_model,
            **self._backend_kwargs,
        )

    def _resolve_index_path(self, source: str) -> str:
        """Return the LEANN index path for a manifest source."""
        if self._configured_index_path is not None:
            return self._configured_index_path
        return str(Path(source).expanduser().resolve()) + ".leann"

    @staticmethod
    def _read_manifest(source: str) -> list[dict[str, Any]]:
        """Read and validate a JSONL manifest written by the indexer."""
        manifest_path = Path(source).expanduser()
        records: list[dict[str, Any]] = []
        with manifest_path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    record = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"invalid JSON on line {line_number} of "
                        f"{manifest_path}: {exc}"
                    ) from exc
                if not isinstance(record, dict):
                    raise ValueError(
                        f"manifest record on line {line_number} of "
                        f"{manifest_path} is not a JSON object"
                    )
                missing = {"path", "content"} - record.keys()
                if missing:
                    raise ValueError(
                        f"manifest record on line {line_number} of "
                        f"{manifest_path} is missing required keys: "
                        f"{', '.join(sorted(missing))}"
                    )
                records.append(record)
        return records

    @staticmethod
    def _passage_id(record: dict[str, Any]) -> str:
        """Return the stable passage id for a manifest record."""
        return f"{record.get('scope', '')}:{record['path']}"

    def _metadata_for(self, record: dict[str, Any]) -> dict[str, Any]:
        """Map a manifest record onto LEANN passage metadata.

        Extra keys under the record's own ``metadata`` mapping (the learning
        manifests carry ``evidence_level``, ``repository``, ``family``,
        ``run``, ``confidence`` and — for ecosystem — ``origin`` there) are
        kept so metadata filters can match on them; the identity fields
        ``id``, ``scope`` and ``path`` always come from the record itself.
        """
        metadata = dict(record.get("metadata") or {})
        metadata["id"] = self._passage_id(record)
        metadata["scope"] = record.get("scope", "")
        metadata["path"] = record["path"]
        return metadata

    def _passage_ids(self, records: list[dict[str, Any]]) -> list[str]:
        """Return the stable passage ids for all manifest records."""
        return [self._passage_id(record) for record in records]

    @staticmethod
    def _build_metadata_filters(
        scope: str | None, filters: dict[str, Any] | None
    ) -> dict[str, dict[str, Any]] | None:
        """Translate the provider-neutral filters into LEANN operator syntax."""
        metadata_filters: dict[str, dict[str, Any]] = {}
        for field, value in (filters or {}).items():
            if isinstance(value, dict):
                metadata_filters[field] = dict(value)
            else:
                metadata_filters[field] = {"==": value}
        if scope is not None:
            metadata_filters["scope"] = {"==": scope}
        return metadata_filters or None

    @staticmethod
    def _map_hit(hit: Any) -> dict[str, Any]:
        """Map a LEANN search hit onto the provider-neutral result shape."""
        metadata = getattr(hit, "metadata", None) or {}
        path = metadata.get("path") or getattr(hit, "id", None)
        return {
            "path": path,
            "content": getattr(hit, "text", ""),
            "score": float(getattr(hit, "score", 0.0)),
            "scope": metadata.get("scope"),
        }

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
