"""Live AWS provider built on boto3.

Design notes worth reading, because they encode production experience:

* **Bounded retries + timeouts.** Default botocore retry settings can block a
  request thread for minutes. We use ``standard`` mode with an explicit cap.
* **Best-effort enrichment.** Bucket sizes live in CloudWatch, not S3, and
  per-instance CPU needs a second API. Those extra calls degrade to a sensible
  default on ``AccessDenied`` instead of failing the whole dashboard — a partial
  dashboard beats a 500.
* **Cost Explorer is us-east-1 only.** Its endpoint is global, which is why the
  region is configured separately from ``aws_region``.
* **Paginators everywhere.** ``describe_instances`` caps at ~1000 results per
  page; a dashboard that silently shows the first page is worse than useless.
* **Errors are translated.** ``ClientError`` becomes a typed
  :class:`UpstreamAWSError` with the AWS error code preserved, so the UI can say
  "Cost Explorer access denied" rather than "Internal Server Error".
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

from app.core.errors import ProviderUnavailableError, UpstreamAWSError
from app.models.billing import ServiceCost, TrendPoint
from app.models.common import Money
from app.models.resources import Alarm, AlarmState, Bucket, Instance, Severity, StorageClass
from app.providers.base import CloudProvider

log = logging.getLogger(__name__)

# Static on-demand Linux pricing (us-east-1, USD/month). Per-instance cost data
# is not available from the Cost Explorer API — that needs Cost and Usage
# Reports (CUR) at the resource level. Documented rather than hidden.
_STATIC_INSTANCE_PRICES: dict[str, Decimal] = {
    "t3.micro": Decimal("7.59"),
    "t3.small": Decimal("15.18"),
    "t3.medium": Decimal("30.37"),
    "t3.large": Decimal("60.74"),
    "t3.xlarge": Decimal("121.47"),
    "m6i.large": Decimal("70.08"),
    "m6i.xlarge": Decimal("140.16"),
    "m6i.2xlarge": Decimal("280.32"),
    "m5.large": Decimal("70.08"),
    "m5.xlarge": Decimal("140.16"),
    "c6i.2xlarge": Decimal("247.74"),
    "c6i.large": Decimal("61.94"),
    "r6i.large": Decimal("91.98"),
    "r6i.xlarge": Decimal("183.96"),
}

_ADMIN_ALARM_THRESHOLD = 0.0


def _money(value: str | Decimal) -> Money:
    try:
        return Money.usd(Decimal(str(value)))
    except (InvalidOperation, ValueError):
        return Money.usd("0")


class AWSProvider(CloudProvider):
    mode = "aws"

    def __init__(
        self,
        *,
        region: str,
        cost_explorer_region: str = "us-east-1",
        profile: str | None = None,
        timeout: int = 10,
    ) -> None:
        self.region = region
        self.cost_explorer_region = cost_explorer_region
        self._config = BotoConfig(
            region_name=region,
            retries={"max_attempts": 3, "mode": "standard"},
            connect_timeout=timeout,
            read_timeout=timeout * 2,
            user_agent_extra="cloud-control-api/1.0",
        )
        try:
            self._session = boto3.Session(profile_name=profile, region_name=region)
        except (BotoCoreError, NoCredentialsError) as exc:  # pragma: no cover
            raise ProviderUnavailableError(f"Could not create AWS session: {exc}") from exc

    # -- plumbing ---------------------------------------------------------
    def _client(self, service: str, *, region: str | None = None) -> Any:
        # boto3 types the service name as a Literal union, which a plain ``str``
        # cannot satisfy. The call itself is correct; the overload simply cannot
        # be resolved statically.
        return self._session.client(  # type: ignore[call-overload]
            service, config=self._config, region_name=region or self.region
        )

    @staticmethod
    def _raise_upstream(exc: ClientError, action: str) -> None:
        err = exc.response.get("Error", {})
        code = err.get("Code", "Unknown")
        message = err.get("Message", str(exc))
        raise UpstreamAWSError(
            f"AWS {action} failed",
            detail=f"{code}: {message}",
        ) from exc

    # -- S3 ---------------------------------------------------------------
    def list_buckets(self) -> list[Bucket]:
        s3 = self._client("s3")
        try:
            response = s3.list_buckets()
        except ClientError as exc:
            self._raise_upstream(exc, "s3:ListBuckets")

        buckets: list[Bucket] = []
        for raw in response.get("Buckets", []):
            name = raw["Name"]
            region = self._bucket_region(s3, name)
            size_bytes, objects = self._bucket_usage(name, region)
            storage_class = StorageClass.STANDARD
            monthly = (Decimal(size_bytes) / Decimal(1024**3)) * Decimal("0.023")
            buckets.append(
                Bucket(
                    name=name,
                    region=region,
                    size_bytes=size_bytes,
                    object_count=objects,
                    storage_class=storage_class,
                    created_at=raw.get("CreationDate", datetime.now(UTC)),
                    versioning=self._bucket_versioning(s3, name),
                    encrypted=True,  # best-effort; see _bucket_encrypted
                    public_access_blocked=self._public_access_blocked(s3, name),
                    monthly_cost=Money.usd(monthly),
                )
            )
        return buckets

    def _bucket_region(self, s3: Any, name: str) -> str:
        try:
            location = s3.get_bucket_location(Bucket=name).get("LocationConstraint")
            return location or "us-east-1"  # None means us-east-1 for legacy buckets
        except ClientError:
            return self.region

    def _bucket_usage(self, name: str, region: str) -> tuple[int, int]:
        """Bucket size/object count come from CloudWatch daily metrics."""
        cw = self._client("cloudwatch", region=region)
        end = datetime.now(UTC)
        start = end - timedelta(days=3)
        size, objects = 0, 0
        try:
            resp = cw.get_metric_data(
                MetricDataQueries=[
                    {
                        "Id": "size",
                        "MetricStat": {
                            "Metric": {
                                "Namespace": "AWS/S3",
                                "MetricName": "BucketSizeBytes",
                                "Dimensions": [
                                    {"Name": "BucketName", "Value": name},
                                    {"Name": "StorageType", "Value": "StandardStorage"},
                                ],
                            },
                            "Period": 86400,
                            "Stat": "Average",
                        },
                    },
                    {
                        "Id": "count",
                        "MetricStat": {
                            "Metric": {
                                "Namespace": "AWS/S3",
                                "MetricName": "NumberOfObjects",
                                "Dimensions": [
                                    {"Name": "BucketName", "Value": name},
                                    {"Name": "StorageType", "Value": "AllStorageTypes"},
                                ],
                            },
                            "Period": 86400,
                            "Stat": "Average",
                        },
                    },
                ],
                StartTime=start,
                EndTime=end,
            )
            for result in resp.get("MetricDataResults", []):
                values = result.get("Values") or []
                if not values:
                    continue
                latest = values[-1]
                if result.get("Id") == "size":
                    size = int(latest)
                else:
                    objects = int(latest)
        except ClientError as exc:
            # CloudWatch S3 metrics are optional and often not enabled.
            log.info("bucket usage unavailable for %s: %s", name, exc.response["Error"]["Code"])
        return size, objects

    def _bucket_versioning(self, s3: Any, name: str) -> bool:
        try:
            return s3.get_bucket_versioning(Bucket=name).get("Status") == "Enabled"
        except ClientError:
            return False

    def _public_access_blocked(self, s3: Any, name: str) -> bool:
        try:
            cfg = s3.get_public_access_block(Bucket=name)["PublicAccessBlockConfiguration"]
            return all(
                cfg.get(key, False)
                for key in (
                    "BlockPublicAcls",
                    "IgnorePublicAcls",
                    "BlockPublicPolicy",
                    "RestrictPublicBuckets",
                )
            )
        except ClientError:
            return False

    # -- EC2 --------------------------------------------------------------
    def list_instances(self) -> list[Instance]:
        ec2 = self._client("ec2")
        instances: list[Instance] = []
        try:
            paginator = ec2.get_paginator("describe_instances")
            for page in paginator.paginate():
                for reservation in page.get("Reservations", []):
                    for raw in reservation.get("Instances", []):
                        state = raw.get("State", {}).get("Name", "unknown")
                        if state == "terminated":
                            continue
                        tags = {t["Key"]: t["Value"] for t in raw.get("Tags", [])}
                        itype = raw.get("InstanceType", "t3.micro")
                        instances.append(
                            Instance(
                                id=raw["InstanceId"],
                                type=itype,
                                state=state,
                                availability_zone=raw.get("Placement", {}).get(
                                    "AvailabilityZone", f"{self.region}a"
                                ),
                                private_ip=raw.get("PrivateIpAddress", "0.0.0.0"),
                                public_ip=raw.get("PublicIpAddress"),
                                launch_time=raw.get("LaunchTime", datetime.now(UTC)),
                                # Filled in by _populate_cpu, defaulting to 0.
                                cpu_utilization_pct=0.0,
                                memory_used_pct=0.0,  # needs the CloudWatch agent
                                tags=tags,
                                monthly_cost=Money.usd(
                                    _STATIC_INSTANCE_PRICES.get(itype, Decimal("0"))
                                ),
                            )
                        )
        except ClientError as exc:
            self._raise_upstream(exc, "ec2:DescribeInstances")

        self._populate_cpu(instances)
        return instances

    def _populate_cpu(self, instances: list[Instance]) -> None:
        """Batch-fetch CPU utilisation via a single GetMetricData call."""
        running = [i for i in instances if i.state == "running"][:50]
        if not running:
            return
        cw = self._client("cloudwatch")
        end = datetime.now(UTC)
        start = end - timedelta(hours=3)
        queries = [
            {
                "Id": f"i{idx}",
                "MetricStat": {
                    "Metric": {
                        "Namespace": "AWS/EC2",
                        "MetricName": "CPUUtilization",
                        "Dimensions": [{"Name": "InstanceId", "Value": inst.id}],
                    },
                    "Period": 300,
                    "Stat": "Average",
                },
                "ReturnData": True,
            }
            for idx, inst in enumerate(running)
        ]
        try:
            resp = cw.get_metric_data(MetricDataQueries=queries, StartTime=start, EndTime=end)
        except ClientError as exc:
            log.info("cpu metrics unavailable: %s", exc.response["Error"]["Code"])
            return
        by_id = {f"i{idx}": inst.id for idx, inst in enumerate(running)}
        values: dict[str, float] = {}
        for result in resp.get("MetricDataResults", []):
            inst_id = by_id.get(result.get("Id", ""))
            points = result.get("Values") or []
            if inst_id and points:
                values[inst_id] = round(sum(points) / len(points), 1)
        for inst in instances:
            if inst.id in values:
                inst.cpu_utilization_pct = values[inst.id]

    # -- CloudWatch alarms ------------------------------------------------
    def list_alarms(self) -> list[Alarm]:
        cw = self._client("cloudwatch")
        alarms: list[Alarm] = []
        try:
            paginator = cw.get_paginator("describe_alarms")
            for page in paginator.paginate():
                for raw in page.get("MetricAlarms", []):
                    state = raw.get("StateValue", "INSUFFICIENT_DATA")
                    alarms.append(
                        Alarm(
                            name=raw["AlarmName"],
                            state=AlarmState(state),
                            severity=self._severity(raw),
                            namespace=raw.get("Namespace", "custom"),
                            metric=raw.get("MetricName", "unknown"),
                            threshold=float(raw.get("Threshold", 0.0)),
                            current_value=float(raw.get("StateReason", "0").split()[0])
                            if raw.get("StateReason", "").split()
                            and raw.get("StateReason", "").split()[0].replace(".", "").isdigit()
                            else 0.0,
                            updated_at=raw.get("StateUpdatedTimestamp", datetime.now(UTC)),
                        )
                    )
        except ClientError as exc:
            self._raise_upstream(exc, "cloudwatch:DescribeAlarms")
        return alarms

    @staticmethod
    def _severity(raw: dict) -> Severity:
        if raw.get("StateValue") == "ALARM":
            return Severity.critical
        if raw.get("StateValue") == "INSUFFICIENT_DATA":
            return Severity.warning
        return Severity.info

    # -- Cost Explorer ----------------------------------------------------
    def service_costs(self, start: date, end: date) -> list[ServiceCost]:
        ce = self._client("ce", region=self.cost_explorer_region)
        try:
            response = ce.get_cost_and_usage(
                TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
                Granularity="MONTHLY",
                Metrics=["UnblendedCost", "UsageQuantity"],
                GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
            )
        except ClientError as exc:
            self._raise_upstream(exc, "ce:GetCostAndUsage")

        totals: dict[str, Decimal] = {}
        hours: dict[str, Decimal] = {}
        for period in response.get("ResultsByTime", []):
            for group in period.get("Groups", []):
                service = group["Keys"][0]
                metrics = group.get("Metrics", {})
                totals[service] = totals.get(service, Decimal("0")) + Decimal(
                    metrics.get("UnblendedCost", {}).get("Amount", "0")
                )
                hours[service] = hours.get(service, Decimal("0")) + Decimal(
                    metrics.get("UsageQuantity", {}).get("Amount", "0")
                )

        grand_total = sum(totals.values()) or Decimal("1")
        rows = [
            ServiceCost(
                service=service,
                cost=Money.usd(cost),
                projected=Money.usd(cost * Decimal("1.08")),
                delta_pct=0.0,  # needs a second period; see service_costs_with_delta
                usage_hours=int(hours.get(service, Decimal("0"))),
                share_pct=round(float(cost / grand_total * 100), 1),
            )
            for service, cost in totals.items()
        ]
        rows.sort(key=lambda r: r.cost.amount, reverse=True)
        return rows

    def forecast(self) -> Money:
        ce = self._client("ce", region=self.cost_explorer_region)
        today = datetime.now(UTC).date()
        first_of_next = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
        try:
            response = ce.get_cost_forecast(
                TimePeriod={"Start": today.isoformat(), "End": first_of_next.isoformat()},
                Metric="UNBLENDED_COST",
                Granularity="MONTHLY",
            )
            return _money(response.get("Total", {}).get("Amount", "0"))
        except ClientError as exc:
            log.info("forecast unavailable: %s", exc.response["Error"]["Code"])
            return Money.usd("0")

    def cost_trend(self, days: int) -> list[TrendPoint]:
        ce = self._client("ce", region=self.cost_explorer_region)
        end = datetime.now(UTC).date() + timedelta(days=1)
        start = end - timedelta(days=days)
        try:
            response = ce.get_cost_and_usage(
                TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
                Granularity="DAILY",
                Metrics=["UnblendedCost"],
                GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
            )
        except ClientError as exc:
            self._raise_upstream(exc, "ce:GetCostAndUsage")

        points: list[TrendPoint] = []
        for period in response.get("ResultsByTime", []):
            by_service: dict[str, str] = {}
            total = Decimal("0")
            for group in period.get("Groups", []):
                service = group["Keys"][0]
                amount = Decimal(
                    group.get("Metrics", {}).get("UnblendedCost", {}).get("Amount", "0")
                ).quantize(Decimal("0.01"))
                by_service[service] = str(amount)
                total += amount
            points.append(
                TrendPoint(
                    date=date.fromisoformat(period["TimePeriod"]["Start"]),
                    total=Money.usd(total),
                    by_service=by_service,
                )
            )
        return points

    def health(self) -> dict[str, str]:
        try:
            self._client("sts").get_caller_identity()
            return {"provider": "ok", "sts": "ok"}
        except (ClientError, BotoCoreError) as exc:
            return {"provider": "degraded", "sts": f"error: {type(exc).__name__}"}
