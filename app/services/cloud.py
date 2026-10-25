"""Domain services: the place where provider data becomes product answers.

Routers stay dumb (parse → call → return). Anything that involves aggregation,
caching or business rules lives here so it can be unit tested without HTTP.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TypeVar

from app.core.cache import TTLCache
from app.core.config import Settings
from app.models.common import Money, Pagination
from app.models.overview import Kpi, Overview
from app.models.resources import Alarm, Bucket, Instance
from app.providers.base import CloudProvider

log = logging.getLogger(__name__)

T = TypeVar("T")


class CloudService:
    """Read-side service over a :class:`CloudProvider`."""

    def __init__(self, provider: CloudProvider, settings: Settings, cache: TTLCache) -> None:
        self.provider = provider
        self.settings = settings
        self.cache = cache

    # -- caching helper ---------------------------------------------------
    def _cached(self, key: str, producer: Callable[[], T]) -> tuple[T, bool]:
        hit = self.cache.get(key)
        if hit is not None:
            return hit, True
        value = producer()
        self.cache.set(key, value)
        return value, False

    # -- resources --------------------------------------------------------
    def buckets(self) -> tuple[list[Bucket], bool]:
        return self._cached("buckets", self.provider.list_buckets)

    def instances(self) -> tuple[list[Instance], bool]:
        return self._cached("instances", self.provider.list_instances)

    def alarms(self) -> tuple[list[Alarm], bool]:
        return self._cached("alarms", self.provider.list_alarms)

    @staticmethod
    def paginate(items: list, page: int, page_size: int) -> tuple[list, Pagination]:
        start = (page - 1) * page_size
        end = start + page_size
        return items[start:end], Pagination.build(page=page, page_size=page_size, total=len(items))

    # -- billing ----------------------------------------------------------
    def current_period(self) -> tuple[date, date]:
        today = datetime.now(UTC).date()
        return today.replace(day=1), today

    def service_costs(self) -> tuple[list, bool]:
        start, end = self.current_period()
        key = f"costs:{start.isoformat()}:{end.isoformat()}"
        return self._cached(key, lambda: self.provider.service_costs(start, end))

    def forecast(self) -> Money:
        value, _ = self._cached("forecast", self.provider.forecast)
        return value

    def trend(self, days: int) -> tuple[list, bool]:
        return self._cached(f"trend:{days}", lambda: self.provider.cost_trend(days))

    # -- derived ----------------------------------------------------------
    def overview(self) -> Overview:
        costs, _ = self.service_costs()
        forecast = self.forecast()
        buckets, _ = self.buckets()
        instances, _ = self.instances()
        alarms, _ = self.alarms()
        trend, _ = self.trend(14)

        spend_mtd = sum((row.cost.amount for row in costs), Decimal("0"))
        budget = Decimal(str(self.settings.monthly_budget))
        used_pct = float(spend_mtd / budget * 100) if budget else 0.0

        running = [i for i in instances if i.state == "running"]
        firing = [a for a in alarms if a.state.value == "ALARM"]
        storage = sum(b.size_bytes for b in buckets)

        spend_series = [float(p.total.amount) for p in trend]
        cpu_series = [i.cpu_utilization_pct for i in running]
        # Storage spend is a genuine daily series (Cost Explorer groups by
        # service), so this sparkline is real rather than decorative.
        storage_series = [
            float(p.by_service.get(key, 0))
            for p in trend
            for key in [
                next((k for k in p.by_service if "s3" in k.lower() or "storage" in k.lower()), "")
            ]
            if key
        ]

        kpis = [
            Kpi(
                key="spend_mtd",
                label="Month-to-date spend",
                value=f"{spend_mtd:,.2f}",
                unit=self.settings.cost_currency,
                delta_pct=self._series_delta(spend_series),
                status="critical" if used_pct > 100 else "warn" if used_pct > 85 else "ok",
                spark=spend_series,
            ),
            Kpi(
                key="forecast",
                label="Forecasted total",
                value=f"{forecast.amount:,.2f}",
                unit=self.settings.cost_currency,
                delta_pct=round((float(forecast.amount) / float(spend_mtd) - 1) * 100, 1)
                if spend_mtd
                else 0.0,
                status="warn" if float(forecast.amount) > float(budget) else "ok",
                spark=spend_series,
            ),
            Kpi(
                key="instances",
                label="EC2 instances",
                value=str(len(running)),
                unit=f"/ {len(instances)}",
                delta_pct=0.0,
                status="ok",
                spark=cpu_series,
            ),
            Kpi(
                key="buckets",
                label="S3 storage",
                value=_human_bytes(storage),
                unit=f"{len(buckets)} buckets",
                delta_pct=0.0,
                status="ok",
                spark=storage_series,
            ),
            Kpi(
                key="alarms",
                label="Alarms firing",
                value=str(len(firing)),
                unit=f"/ {len(alarms)} total",
                delta_pct=0.0,
                status="critical" if firing else "ok",
                spark=[],
            ),
        ]

        return Overview(
            kpis=kpis,
            spend_month_to_date=Money.usd(spend_mtd),
            spend_forecast=forecast,
            budget=Money.usd(budget),
            budget_used_pct=round(used_pct, 1),
            buckets=len(buckets),
            instances=len(instances),
            instances_running=len(running),
            alarms_firing=len(firing),
            storage_bytes=storage,
            top_services=costs[:5],
            health_score=self._health_score(
                buckets=buckets, alarms=alarms, used_pct=used_pct, instances=instances
            ),
        )

    @staticmethod
    def _series_delta(series: list[float]) -> float:
        """Percentage change of the last 7 points vs the previous 7."""
        if len(series) < 14:
            return 0.0
        recent, prior = series[-7:], series[-14:-7]
        prior_sum = sum(prior)
        if not prior_sum:
            return 0.0
        return round((sum(recent) - prior_sum) / prior_sum * 100, 1)

    @staticmethod
    def _health_score(
        *, buckets: list[Bucket], alarms: list[Alarm], used_pct: float, instances: list[Instance]
    ) -> int:
        """A transparent 0-100 score. Penalties are documented in the README."""
        score = 100.0
        score -= 8 * sum(1 for a in alarms if a.state.value == "ALARM")
        score -= 3 * sum(1 for b in buckets if not b.encrypted)
        score -= 5 * sum(1 for b in buckets if not b.public_access_blocked)
        score -= 10 if used_pct > 100 else 4 if used_pct > 85 else 0.0
        score -= 2 * sum(1 for i in instances if i.state == "stopped")
        return max(0, min(100, round(score)))


def _human_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if size < 1024 or unit == "PB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} PB"
