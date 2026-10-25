"""Deterministic demo provider.

Important properties:

* **Deterministic.** All pseudo-randomness is seeded from a SHA-256 of stable
  identifiers (resource name, ISO date), so the same request twice returns the
  same numbers. That makes the UI stable, screenshots reproducible and tests
  meaningful — the opposite of ``random.random()`` per request.
* **Realistic.** Shapes, sizes, ID formats and price rates follow AWS
  conventions, so the frontend is exercised against production-like data.
* **Obviously fake.** ``mode == "mock"`` and the UI surfaces a MOCK badge, so
  no one can mistake this for live billing data.
"""

from __future__ import annotations

import hashlib
import random
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from app.models.billing import ServiceCost, TrendPoint
from app.models.common import Money
from app.models.resources import Alarm, AlarmState, Bucket, Instance, Severity, StorageClass
from app.providers.base import CloudProvider

GB = 1024**3
TB = 1024**4
MB = 1024**2
GIB = Decimal(1024**3)

# On-demand USD/GB-month by storage class (order-of-magnitude realistic).
STORAGE_RATES: dict[StorageClass, Decimal] = {
    StorageClass.STANDARD: Decimal("0.023"),
    StorageClass.STANDARD_IA: Decimal("0.0125"),
    StorageClass.INTELLIGENT_TIERING: Decimal("0.0195"),
    StorageClass.GLACIER: Decimal("0.004"),
    StorageClass.DEEP_ARCHIVE: Decimal("0.00099"),
}

# Monthly baseline spend per service. Shape mirrors a real EKS-based estate:
# compute dominates, then data, then managed services.
SERVICE_BASELINES: dict[str, Decimal] = {
    "Amazon EC2": Decimal("3842.18"),
    "Amazon RDS": Decimal("1204.55"),
    "Amazon S3": Decimal("486.32"),
    "Amazon EKS": Decimal("438.00"),
    "Amazon CloudFront": Decimal("214.60"),
    "AWS Data Transfer": Decimal("167.23"),
    "AWS Lambda": Decimal("128.74"),
    "Amazon CloudWatch": Decimal("92.15"),
}

# Static on-demand price map used to estimate per-instance monthly cost.
INSTANCE_RATES: dict[str, Decimal] = {
    "t3.micro": Decimal("7.59"),
    "t3.small": Decimal("15.18"),
    "t3.medium": Decimal("30.37"),
    "t3.large": Decimal("60.74"),
    "m6i.large": Decimal("70.08"),
    "m6i.xlarge": Decimal("140.16"),
    "c6i.2xlarge": Decimal("247.74"),
    "r6i.large": Decimal("91.98"),
}

_BUCKETS: list[dict] = [
    {
        "name": "user-uploads",
        "size_bytes": 412 * GB + 823 * MB,
        "object_count": 1_284_331,
        "storage_class": StorageClass.STANDARD,
        "age_days": 612,
        "versioning": True,
        "encrypted": True,
        "public_access_blocked": True,
    },
    {
        "name": "backup-data",
        "size_bytes": 2 * TB + 409 * GB,
        "object_count": 88_402,
        "storage_class": StorageClass.INTELLIGENT_TIERING,
        "age_days": 934,
        "versioning": True,
        "encrypted": True,
        "public_access_blocked": True,
    },
    {
        "name": "static-website",
        "size_bytes": 18 * MB + 611 * 1024,
        "object_count": 214,
        "storage_class": StorageClass.STANDARD,
        "age_days": 1204,
        "versioning": False,
        "encrypted": True,
        "public_access_blocked": True,
    },
    {
        "name": "logs-archive",
        "size_bytes": 6 * TB + 113 * GB,
        "object_count": 3_900_112,
        "storage_class": StorageClass.GLACIER,
        "age_days": 1450,
        "versioning": True,
        "encrypted": True,
        "public_access_blocked": True,
    },
    {
        "name": "my-first-bucket",
        "size_bytes": 1 * GB + 204 * MB,
        "object_count": 5_310,
        "storage_class": StorageClass.STANDARD_IA,
        "age_days": 1810,
        "versioning": False,
        "encrypted": False,
        "public_access_blocked": False,
    },
]

# (id, type, state, az, cpu, mem, age_days, name-tag)
_INSTANCES: list[tuple[str, str, str, str, float, float, int, str]] = [
    ("i-0a3f9c2e7b1d4f68a", "m6i.large", "running", "us-east-1a", 34.2, 61.8, 402, "api-prod-01"),
    ("i-0b71e4d9c2a83f105", "m6i.xlarge", "running", "us-east-1b", 58.7, 72.4, 402, "api-prod-02"),
    (
        "i-0c92a1f4d7e63b28e",
        "c6i.2xlarge",
        "running",
        "us-east-1c",
        71.5,
        44.9,
        288,
        "worker-prod-01",
    ),
    ("i-0d40b8e2f1c95a376", "r6i.large", "running", "us-east-1a", 22.9, 81.3, 288, "cache-prod-01"),
    ("i-0e18c3a6b4d72f901", "t3.medium", "running", "us-east-1b", 12.4, 38.1, 156, "bastion-prod"),
    ("i-0f67d2b8a1e45c3d9", "t3.small", "stopped", "us-east-1c", 0.0, 0.0, 611, "staging-web"),
    ("i-0a95e7c1d3f28b604", "t3.large", "running", "us-east-1a", 46.3, 55.7, 94, "batch-2024"),
    ("i-0b28f4a9e6c13d785", "t3.small", "stopped", "us-east-1b", 0.0, 0.0, 823, "legacy-legacy-01"),
]

_ALARMS: list[tuple[str, str, str, str, float, float, str]] = [
    ("api-prod-high-cpu", "ALARM", "AWS/EC2", "CPUUtilization", 80.0, 91.4, "critical"),
    ("rds-storage-low", "ALARM", "AWS/RDS", "FreeStorageSpace", 20.0, 14.2, "critical"),
    (
        "worker-queue-depth",
        "ALARM",
        "AWS/SQS",
        "ApproximateNumberOfMessagesVisible",
        1000.0,
        2431.0,
        "warning",
    ),
    ("lambda-error-rate", "OK", "AWS/Lambda", "Errors", 5.0, 0.4, "info"),
    ("alb-target-5xx", "OK", "AWS/ApplicationELB", "HTTPCode_Target_5XX_Count", 10.0, 1.0, "info"),
    ("s3-4xx-spike", "INSUFFICIENT_DATA", "AWS/S3", "4xxErrors", 50.0, 0.0, "warning"),
]


def _rng(*parts: object) -> random.Random:
    """A ``Random`` seeded reproducibly from the given parts."""
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()
    return random.Random(int(digest[:16], 16))


class MockProvider(CloudProvider):
    mode = "mock"

    def __init__(self, region: str = "us-east-1") -> None:
        self.region = region

    # -- resources --------------------------------------------------------
    def list_buckets(self) -> list[Bucket]:
        now = datetime.now(UTC)
        buckets: list[Bucket] = []
        for spec in _BUCKETS:
            size_bytes = int(spec["size_bytes"])
            rate = STORAGE_RATES[spec["storage_class"]]
            monthly = (Decimal(size_bytes) / GIB) * rate
            buckets.append(
                Bucket(
                    name=spec["name"],
                    region=self.region,
                    size_bytes=size_bytes,
                    object_count=spec["object_count"],
                    storage_class=spec["storage_class"],
                    created_at=now - timedelta(days=spec["age_days"]),
                    versioning=spec["versioning"],
                    encrypted=spec["encrypted"],
                    public_access_blocked=spec["public_access_blocked"],
                    monthly_cost=Money.usd(monthly),
                )
            )
        return buckets

    def list_instances(self) -> list[Instance]:
        now = datetime.now(UTC)
        instances: list[Instance] = []
        for idx, (iid, itype, state, az, cpu, mem, age, name) in enumerate(_INSTANCES):
            jitter = _rng("instance", iid).uniform(0.9, 1.1)
            rate = INSTANCE_RATES.get(itype, Decimal("30.00")) * Decimal(str(round(jitter, 4)))
            instances.append(
                Instance(
                    id=iid,
                    type=itype,
                    state=state,
                    availability_zone=az,
                    private_ip=f"10.0.{idx // 4}.{20 + idx * 7 % 200}",
                    public_ip=None
                    if idx % 3 == 0
                    else f"54.2{idx}.{11 + idx}.{40 + idx * 13 % 200}",
                    launch_time=now - timedelta(days=age, hours=idx * 3),
                    cpu_utilization_pct=cpu,
                    memory_used_pct=mem,
                    tags={
                        "Name": name,
                        "env": "prod" if "prod" in name else "staging",
                        "team": "platform",
                        "managed-by": "terraform",
                    },
                    monthly_cost=Money.usd(rate),
                )
            )
        return instances

    def list_alarms(self) -> list[Alarm]:
        now = datetime.now(UTC)
        alarms: list[Alarm] = []
        for idx, (name, state, ns, metric, threshold, current, sev) in enumerate(_ALARMS):
            alarms.append(
                Alarm(
                    name=name,
                    state=AlarmState(state),
                    severity=Severity(sev),
                    namespace=ns,
                    metric=metric,
                    threshold=threshold,
                    current_value=current,
                    updated_at=now - timedelta(minutes=3 + idx * 11),
                )
            )
        return alarms

    # -- billing ----------------------------------------------------------
    def _baselines(self) -> dict[str, Decimal]:
        """Service baselines with S3 reconciled against real bucket storage.

        Storage alone is only part of an S3 bill (requests, retrieval, transfer),
        but leaving the two numbers unrelated would mean the dashboard's
        "8.9 TB across 5 buckets" card and its "Amazon S3" cost row contradict
        each other. Deriving one from the other keeps the story consistent.
        """
        storage = sum((b.monthly_cost.amount for b in self.list_buckets()), Decimal("0"))
        baselines = dict(SERVICE_BASELINES)
        baselines["Amazon S3"] = (storage * Decimal("1.45")).quantize(Decimal("0.01"))
        return baselines

    def service_costs(self, start: date, end: date) -> list[ServiceCost]:
        span_days = max((end - start).days + 1, 1)
        month_days = 30
        baselines = self._baselines()
        sum(baselines.values())
        proration = Decimal(str(round(span_days / month_days, 6)))
        staged: list[tuple[str, Decimal, Decimal, float, int]] = []
        for service, base in baselines.items():
            rng = _rng("cost", service, start.isoformat(), end.isoformat())
            cost = base * proration
            projected = base * Decimal(str(round(rng.uniform(1.02, 1.22), 4)))
            staged.append(
                (
                    service,
                    cost,
                    projected,
                    round(rng.uniform(-18.0, 34.0), 1),
                    int(rng.uniform(500, 12000)),
                )
            )

        # Shares are of the spend that was actually returned, so they always
        # sum to 100% regardless of how much of the month has elapsed.
        period_total = sum((row[1] for row in staged), Decimal("0")) or Decimal("1")
        rows = [
            ServiceCost(
                service=service,
                cost=Money.usd(cost),
                projected=Money.usd(projected),
                delta_pct=delta,
                usage_hours=hours,
                share_pct=round(float(cost / period_total) * 100, 1),
            )
            for service, cost, projected, delta, hours in staged
        ]
        rows.sort(key=lambda r: r.cost.amount, reverse=True)
        return rows

    def forecast(self) -> Money:
        rng = _rng("forecast", date.today().isoformat())
        total = sum(self._baselines().values())
        return Money.usd(total * Decimal(str(round(rng.uniform(1.03, 1.19), 4))))

    def cost_trend(self, days: int) -> list[TrendPoint]:
        today = datetime.now(UTC).date()
        baselines = self._baselines()
        month_total = sum(baselines.values())
        daily_base = month_total / Decimal(30)
        points: list[TrendPoint] = []
        for offset in range(days - 1, -1, -1):
            day = today - timedelta(days=offset)
            by_service: dict[str, str] = {}
            day_total = Decimal("0")
            for service, base in baselines.items():
                rng = _rng("trend", service, day.isoformat())
                share = base / month_total
                weekend = Decimal("0.82") if day.weekday() >= 5 else Decimal("1.0")
                noise = Decimal(str(round(rng.uniform(0.90, 1.12), 4)))
                value = (daily_base * share * weekend * noise).quantize(Decimal("0.01"))
                by_service[service] = str(value)
                day_total += value
            points.append(TrendPoint(date=day, total=Money.usd(day_total), by_service=by_service))
        return points

    def health(self) -> dict[str, str]:
        return {"provider": "ok", "source": "deterministic-fixtures"}
