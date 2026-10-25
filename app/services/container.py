"""Dependency container.

One place where the settings, the chosen provider and the cache are wired
together. FastAPI ``Depends`` resolves through here, which keeps routers thin
and makes tests trivial: override ``get_container`` and the whole app talks to a
different provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from app.core.cache import TTLCache
from app.core.config import Settings, get_settings
from app.models.common import ProviderInfo
from app.providers.base import CloudProvider
from app.providers.registry import ProviderBundle, build_provider


@dataclass
class Container:
    settings: Settings
    bundle: ProviderBundle
    cache: TTLCache

    @property
    def provider(self) -> CloudProvider:
        return self.bundle.provider

    @property
    def info(self) -> ProviderInfo:
        return self.bundle.info


@lru_cache(maxsize=1)
def get_container() -> Container:
    settings = get_settings()
    bundle = build_provider(settings)
    cache = TTLCache(ttl=float(settings.cache_ttl_seconds), max_entries=256)
    return Container(settings=settings, bundle=bundle, cache=cache)


def reset_container() -> None:
    """Drop the cached container (used by tests and after a mode change)."""
    get_container.cache_clear()
