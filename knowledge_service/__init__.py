"""Standalone knowledge service.

The knowledge layer as its own provider-neutral HTTP service: one package,
one config file, one SQLite store, and five ``/v1`` routes so every harness
(DeepSeek Harness, mcp-light, simple-harness, FlowRunner, exported FlowApps
and DPMtF itself) consumes the same contract.

Modules: ``provider`` (the interface), ``leann_provider`` (the LEANN-backed
implementation), ``search`` (provider resolution), ``indexer`` and
``maintenance`` (manifest building and change detection), ``scopes`` (the
shared path-to-scope rule), ``scope_guard`` (internal-scope grants),
``retrieval_log`` (the append-only audit row), ``config`` (``knowledge.ini``),
``db`` (this service's SQLite store), ``app`` (the FastAPI routes) and
``cli`` (the command-line entry point).
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "1.0.0"
