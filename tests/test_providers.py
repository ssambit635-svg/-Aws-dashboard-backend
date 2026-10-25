"""Provider-level tests: determinism, realism, and registry selection logic."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.core.config import Settings
from app.core.errors import ProviderUnavailableError
from app.providers import registry
from app.providers.mock import GB, MockProvider


@pytest.fixture
def provider():
    return MockProvider(region="eu-west-1")


# ── determinism ───────────────────────────────────────────────────────────
def test_bucket_costs_are_stable_across_calls(provider):
    first = [(b.name, b.monthly_cost.amount) for b in provider.list_buckets()]
    second = [(b.name, b.monthly_cost.amount) for b in provider.list_buckets()]
    assert first == second


def test_instance_costs_are_stable_across_calls(provider):
    first = [(i.id, i.monthly_cost.amount) for i in provider.list_instances()]
    second = [(i.id, i.monthly_cost.amount) for i in provider.list_instances()]
    assert first == second


def test_service_costs_are_stable_for_the_same_period(provider):
    start = date(2026, 10, 1)
    end = date(2026, 10, 25)
    first = provider.service_costs(start, end)
    second = provider.service_costs(start, end)
    assert [(r.service, r.cost.amount) for r in first] == [
        (r.service, r.cost.amount) for r in second
    ]


def test_trend_is_stable(provider):
    first = provider.cost_trend(30)
    second = provider.cost_trend(30)
    assert [p.total.amount for p in first] == [p.total.amount for p in second]


def test_different_days_produce_different_data(provider):
    """Determinism must not collapse into a flat line."""
    points = provider.cost_trend(30)
    totals = {p.total.amount for p in points}
    assert len(totals) > 5


def test_weekends_are_cheaper_than_weekdays(provider):
    """A cost model with no weekday signal is a weak fixture."""
    points = provider.cost_trend(60)
    weekday = [
        float(p.total.amount) for p in points if date.fromisoformat(str(p.date)).weekday() < 5
    ]
    weekend = [
        float(p.total.amount) for p in points if date.fromisoformat(str(p.date)).weekday() >= 5
    ]
    assert sum(weekday) / len(weekday) > sum(weekend) / len(weekend)


# ── realism ───────────────────────────────────────────────────────────────
def test_bucket_ids_are_unique_and_costs_positive(provider):
    buckets = provider.list_buckets()
    assert len({b.name for b in buckets}) == len(buckets)
    assert all(b.monthly_cost.amount >= 0 for b in buckets)


def test_bucket_cost_matches_size_and_storage_class(provider):
    for bucket in provider.list_buckets():
        expected = (Decimal(bucket.size_bytes) / Decimal(GB)) * Decimal(
            {
                "STANDARD": "0.023",
                "STANDARD_IA": "0.0125",
                "INTELLIGENT_TIERING": "0.0195",
                "GLACIER": "0.004",
                "DEEP_ARCHIVE": "0.00099",
            }[bucket.storage_class.value]
        )
        assert abs(bucket.monthly_cost.amount - expected) <= Decimal("0.02")


def test_instance_ids_are_unique(provider):
    ids = [i.id for i in provider.list_instances()]
    assert len(set(ids)) == len(ids)


def test_stopped_instances_report_zero_utilisation(provider):
    for instance in provider.list_instances():
        if instance.state == "stopped":
            assert instance.cpu_utilization_pct == 0
            assert instance.memory_used_pct == 0


def test_running_instances_report_real_utilisation(provider):
    for instance in provider.list_instances():
        if instance.state == "running":
            assert instance.cpu_utilization_pct > 0


def test_storage_cost_reconciles_with_s3_service_line(provider):
    """The S3 total must be consistent with the bucket inventory it summarises."""
    buckets_cost = sum((b.monthly_cost.amount for b in provider.list_buckets()), Decimal("0"))
    start, end = date(2026, 10, 1), date(2026, 10, 25)
    s3 = next(r for r in provider.service_costs(start, end) if r.service == "Amazon S3")
    assert s3.cost.amount > buckets_cost  # storage + requests + transfer
    assert s3.cost.amount < buckets_cost * Decimal("3")


def test_alarm_states_are_mixed(provider):
    states = {a.state.value for a in provider.list_alarms()}
    assert {"ALARM", "OK", "INSUFFICIENT_DATA"} <= states


def test_health_reports_ok(provider):
    assert provider.health()["provider"] == "ok"


# ── registry ──────────────────────────────────────────────────────────────
def test_explicit_mock_mode(monkeypatch):
    settings = Settings(provider_mode="mock")
    bundle = registry.build_provider(settings)
    assert bundle.info.mode == "mock"
    assert bundle.info.live is False
    assert bundle.info.fallback_reason


def test_auto_mode_falls_back_without_credentials(monkeypatch):
    monkeypatch.setattr(registry, "_has_credentials", lambda s: (False, "no creds in test"))
    bundle = registry.build_provider(Settings(provider_mode="auto"))
    assert bundle.info.mode == "mock"
    assert bundle.info.requested_mode == "auto"
    assert bundle.info.live is False
    assert bundle.info.fallback_reason == "no creds in test"


def test_aws_mode_without_credentials_raises(monkeypatch):
    monkeypatch.setattr(registry, "_has_credentials", lambda s: (False, "missing"))
    with pytest.raises(ProviderUnavailableError) as exc:
        registry.build_provider(Settings(provider_mode="aws"))
    assert "missing" in (exc.value.detail or "")


def test_aws_mode_with_credentials_selects_aws(monkeypatch):
    monkeypatch.setattr(registry, "_has_credentials", lambda s: (True, None))
    bundle = registry.build_provider(Settings(provider_mode="auto"))
    assert bundle.info.mode == "aws"
    assert bundle.info.live is True
    assert bundle.info.fallback_reason is None
