"""Provider resolution for the knowledge search API.

``knowledge_service.app`` must stay free of any concrete provider name, so
the configured-provider-key to provider-class mapping lives here.
``"none"`` maps to the no-op ``NoneProvider`` and ``"leann"`` maps to the
LEANN-backed provider. The LEANN-backed class is imported lazily inside its
loader so importing this service layer never imports the optional LEANN
dependency. Unknown provider keys resolve to ``NoneProvider`` and never
raise, matching the disabled-by-default contract: a misconfigured provider
key must not take the API down.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Callable

from knowledge_service import config
from knowledge_service.provider import KnowledgeProvider, NoneProvider

__all__ = ["PROVIDER_LOADERS", "resolve_provider"]


def _load_leann_provider(scope: str | None = None) -> Callable[[], KnowledgeProvider]:
    """Return a LeannProvider factory bound to the requested scope's index path.

    A given ``scope`` wins; when no scope is given the configured knowledge
    scope is the fallback, so the loader never silently binds a foreign
    scope's store to the configured one.
    """
    from knowledge_service.leann_provider import LeannProvider

    index_path = (
        Path(config.get_index_dir())
        / f"{scope or config.get_scope()}.leann"
    )
    return functools.partial(
        LeannProvider,
        index_path=str(index_path),
        searcher_kwargs={"use_daemon": config.get_leann_use_daemon()},
    )


# Maps a configured provider key to a zero-argument loader returning a
# provider class or a bound factory (the leann loader binds index_path).
# Loaders (rather than already-imported classes) keep the LEANN import lazy:
# importing this module must not import leann_provider.
PROVIDER_LOADERS: dict[str, Callable[[], KnowledgeProvider]] = {
    "none": lambda: NoneProvider,
    "leann": _load_leann_provider,
}


def resolve_provider(name: str, scope: str | None = None) -> Callable[[], KnowledgeProvider]:
    """Return the provider class for a configured provider key.

    ``"none"`` resolves to the no-op provider, and any unknown key also
    resolves to it, so a misconfigured provider key can never raise. Only
    ``"leann"`` receives ``scope``; the no-op provider, any unknown key, and
    any other registered loader ignore it. Registered non-``none`` loaders
    are still honoured so callers that temporarily extend
    ``PROVIDER_LOADERS`` (the tests' stub provider) keep resolving through
    this function.
    """
    if name == "leann":
        return _load_leann_provider(scope)
    if name == "none":
        return NoneProvider
    loader = PROVIDER_LOADERS.get(name)
    if loader is not None:
        return loader()
    return NoneProvider
