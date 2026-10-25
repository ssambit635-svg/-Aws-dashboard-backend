"""A tiny in-process TTL cache with observability.

Cloud control-plane APIs are slow and rate limited (Cost Explorer in
particular: ~1 request/second). Caching read paths is not an optimisation here,
it is what keeps the service inside provider quotas. The hit/miss counters are
exposed on ``/api/v1/system/info`` so the behaviour is visible in the UI.
"""

from __future__ import annotations

import functools
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

T = TypeVar("T")


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    evictions: int = 0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return round(self.hits / total, 4) if total else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "evictions": self.evictions,
            "hit_rate": self.hit_rate,
        }


@dataclass
class _Entry:
    value: Any
    expires_at: float


@dataclass
class TTLCache:
    """Thread-safe, size-bounded, TTL-based cache.

    Keys are derived from the wrapped call's arguments, which is enough for the
    read-only aggregation endpoints this service exposes.
    """

    ttl: float = 60.0
    max_entries: int = 512
    stats: CacheStats = field(default_factory=CacheStats)
    _store: dict[str, _Entry] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def get(self, key: str) -> Any | None:
        now = time.monotonic()
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self.stats.misses += 1
                return None
            if entry.expires_at <= now:
                del self._store[key]
                self.stats.evictions += 1
                self.stats.misses += 1
                return None
            self.stats.hits += 1
            return entry.value

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            if len(self._store) >= self.max_entries:
                # Cheap FIFO eviction; adequate for a bounded read cache.
                oldest = min(self._store, key=lambda k: self._store[k].expires_at)
                del self._store[oldest]
                self.stats.evictions += 1
            self._store[key] = _Entry(value=value, expires_at=time.monotonic() + self.ttl)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


def cached(cache: TTLCache) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Memoise a function's result in ``cache``, keyed by its arguments."""

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            key = f"{func.__module__}.{func.__qualname__}:{args!r}:{sorted(kwargs.items())!r}"
            hit = cache.get(key)
            if hit is not None:
                return hit
            value = func(*args, **kwargs)
            cache.set(key, value)
            return value

        return wrapper

    return decorator
