"""Provider-neutral knowledge retrieval interface.

The knowledge layer is disabled by default and the default provider is
``none`` (a no-op). Execution code must depend only on this interface so a
future retrieval backend can be added behind it without redesigning flows.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class ProviderNotReady(Exception):
    """Raised when a knowledge provider cannot run safely right now."""


class KnowledgeProvider(ABC):
    """Abstract interface for a knowledge retrieval backend."""

    def preflight(self) -> None:
        """Check provider readiness; default providers are always ready."""
        return None

    @abstractmethod
    def index(self, source: str) -> None:
        """Index the given source into the knowledge store."""

    @abstractmethod
    def update(self, source: str) -> None:
        """Update previously indexed knowledge for the given source."""

    @abstractmethod
    def remove(self, source: str) -> None:
        """Remove the given source from the knowledge store."""

    @abstractmethod
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

        The no-op provider returns an empty list.
        """


class NoneProvider(KnowledgeProvider):
    """No-op knowledge provider used when retrieval is disabled."""

    def index(self, source: str) -> None:
        return None

    def update(self, source: str) -> None:
        return None

    def remove(self, source: str) -> None:
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
        return []
