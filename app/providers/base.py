"""The contract every cloud data source must satisfy.

The routers and services depend only on this interface, which is what makes the
service testable without AWS and swappable for a mock in CI.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

from app.models.billing import ServiceCost, TrendPoint
from app.models.common import Money
from app.models.resources import Alarm, Bucket, Instance


class CloudProvider(ABC):
    """Read-only view over a cloud account."""

    mode: str = "base"
    region: str = "us-east-1"

    @abstractmethod
    def list_buckets(self) -> list[Bucket]:
        """All S3 buckets visible to the caller."""

    @abstractmethod
    def list_instances(self) -> list[Instance]:
        """All EC2 instances in the account, across states."""

    @abstractmethod
    def list_alarms(self) -> list[Alarm]:
        """All CloudWatch metric alarms."""

    @abstractmethod
    def service_costs(self, start: date, end: date) -> list[ServiceCost]:
        """Cost grouped by AWS service for the inclusive period."""

    @abstractmethod
    def forecast(self) -> Money:
        """Projected spend for the current month."""

    @abstractmethod
    def cost_trend(self, days: int) -> list[TrendPoint]:
        """Daily total spend, broken down per service."""

    def health(self) -> dict[str, str]:
        return {"provider": "ok"}
