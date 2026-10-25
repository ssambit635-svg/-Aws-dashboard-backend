"""Service-layer tests: caching, pagination and derived metrics."""

from __future__ import annotations

from datetime import UTC, timedelta
from decimal import Decimal

import pytest

from app.core.cache import TTLCache, cached
from app.core.config import Settings
from app.providers.mock import MockProvider
from app.services.cloud import CloudService, _human_bytes


@pytest.fixture
def service():
    settings = Settings(provider_mode="mock", monthly_budget=8000)
    return CloudService(MockProvider(), settings, TTLCache(ttl=60))


# ── caching ───────────────────────────────────────────────────────────────
def test_repeat_reads_hit_the_cache(service):
    _, first_cached = service.buckets()
    _, second_cached = service.buckets()
    assert first_cached is False
    assert second_cached is True


def test_cache_reduces_provider_calls(service, monkeypatch):
    calls = {"n": 0}
    original = service.provider.list_buckets

    def counted():
        calls["n"] += 1
        return original()

    monkeypatch.setattr(service.provider, "list_buckets", counted)
    for _ in range(5):
        service.buckets()
    assert calls["n"] == 1


def test_cache_stats_are_tracked(service):
    service.buckets()
    service.buckets()
    service.buckets()
    stats = service.cache.stats
    assert stats.hits == 2
    assert stats.misses == 1
    assert stats.hit_rate == pytest.approx(2 / 3, abs=0.01)


def test_ttl_expiry_evicts(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr("app.core.cache.time.monotonic", lambda: clock["t"])
    cache = TTLCache(ttl=10)
    cache.set("k", "v")
    assert cache.get("k") == "v"
    clock["t"] += 11
    assert cache.get("k") is None
    assert cache.stats.evictions == 1


def test_cache_bound_evicts_oldest(monkeypatch):
    monkeypatch.setattr("app.core.cache.time.monotonic", lambda: 1000.0)
    cache = TTLCache(ttl=60, max_entries=2)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.set("c", 3)
    assert len(cache._store) == 2
    assert cache.stats.evictions == 1


def test_cached_decorator_keys_on_arguments():
    calls = {"n": 0}
    cache = TTLCache(ttl=60)

    @cached(cache)
    def double(x):
        calls["n"] += 1
        return x * 2

    assert double(2) == 4
    assert double(2) == 4
    assert double(3) == 6
    assert calls["n"] == 2


# ── pagination ────────────────────────────────────────────────────────────
def test_pagination_math(service):
    items = list(range(25))
    page3, meta = service.paginate(items, page=3, page_size=10)
    assert page3 == [20, 21, 22, 23, 24]
    assert meta.total == 25
    assert meta.total_pages == 3
    assert meta.has_next is False


def test_pagination_beyond_end_returns_empty(service):
    page, meta = service.paginate(list(range(3)), page=9, page_size=10)
    assert page == []
    assert meta.has_next is False


# ── derived metrics ───────────────────────────────────────────────────────
def test_overview_is_internally_consistent(service):
    overview = service.overview()
    assert overview.instances == 8
    assert overview.instances_running == 6
    assert overview.buckets == 5
    assert overview.alarms_firing == 3
    assert overview.storage_bytes > 0
    assert overview.spend_month_to_date.amount > 0
    assert 0 <= overview.health_score <= 100


def test_health_score_penalises_firing_alarms(service):
    healthy = service._health_score(buckets=[], alarms=[], used_pct=0, instances=[])
    assert healthy == 100

    from app.models.resources import Alarm, AlarmState, Severity

    alarms = [
        Alarm(
            name=f"a{i}",
            state=AlarmState.ALARM,
            severity=Severity.critical,
            namespace="AWS/EC2",
            metric="CPUUtilization",
            threshold=80,
            current_value=95,
            updated_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        )
        for i in range(3)
    ]
    assert service._health_score(buckets=[], alarms=alarms, used_pct=0, instances=[]) == 76


def test_health_score_penalises_public_buckets(service):
    from datetime import datetime

    from app.models.common import Money
    from app.models.resources import Bucket, StorageClass

    public = Bucket(
        name="open-bucket",
        region="us-east-1",
        size_bytes=1,
        object_count=1,
        storage_class=StorageClass.STANDARD,
        created_at=datetime.now(UTC),
        encrypted=False,
        public_access_blocked=False,
        monthly_cost=Money.usd("0"),
    )
    score = service._health_score(buckets=[public], alarms=[], used_pct=0, instances=[])
    assert score == 100 - 3 - 5  # unencrypted + public


def test_budget_percentage(service):
    overview = service.overview()
    expected = float(overview.spend_month_to_date.amount / Decimal("8000") * 100)
    assert overview.budget_used_pct == pytest.approx(expected, abs=0.1)


def test_series_delta_uses_two_windows(service):
    assert service._series_delta([1.0] * 7) == 0.0
    assert service._series_delta([1.0] * 7 + [2.0] * 7) == 100.0
    assert service._series_delta([2.0] * 7 + [1.0] * 7) == -50.0


def test_current_period_starts_on_the_first(service):
    start, end = service.current_period()
    assert start.day == 1
    assert end >= start
    assert end - start <= timedelta(days=31)


@pytest.mark.parametrize(
    "value,expected",
    [
        (0, "0 B"),
        (512, "512 B"),
        (1024, "1.0 KB"),
        (1024**2, "1.0 MB"),
        (1024**4, "1.0 TB"),
        (int(9.2 * 1024**4), "9.2 TB"),
    ],
)
def test_human_bytes(value, expected):
    assert _human_bytes(value) == expected
