"""Live AWS provider built on boto3.

Design notes worth reading, because they encode production experience:

* **Bounded retries + timeouts.** Default botocore retry settings can block a
  request thread for minutes. We use ``standard`` mode with an explicit cap.
* **Clients are cached.** ``boto3.Session.client()`` is expensive (model
  loading, endpoint resolution). Creating one per call, as the first draft did,
  added hundreds of milliseconds to every request.
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
* **Enums are coerced, never trusted.** AWS adds values (``shutting-down``) and
  returns operator strings (``GreaterThanOrEqualToThreshold``) that a strict
  model rejects. Anything unrecognised degrades to a safe default instead of
  raising — a dashboard must not 500 because of an unusual resource.
"""

from __future__ import annotations

import calendar
import logging
import re
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Literal

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
    "c6i.large": Decimal("61.94"),
    "c6i.2xlarge": Decimal("247.74"),
    "c6i.4xlarge": Decimal("495.48"),
    "r6i.large": Decimal("91.98"),
    "r6i.xlarge": Decimal("183.96"),
}

# EC2 publishes these; a value outside the set is bucketed as "unknown" so one
# odd instance cannot invalidate the whole response.
_VALID_INSTANCE_STATES = {
    "pending",
    "running",
    "shutting-down",
    "terminated",
    "stopping",
    "stopped",
}

# GetMetricData accepts at most 500 MetricDataQueries per call.
_METRIC_QUERY_LIMIT = 500

# S3 standard storage, USD per GB-month. Used for the estimated bucket cost.
_S3_STANDARD_RATE = Decimal("0.023")

# StateReason looks like:
#   "Threshold Crossed: 1 datapoint [91.44 (25/10/26 07:00:00)] was greater..."
_STATE_VALUE_RE = re.compile(r"\[\s*(-?\d+(?:\.\d+)?)")


def _money(value: str | Decimal | float) -> Money:
    try:
        return Money.usd(Decimal(str(value)))
    except (InvalidOperation, ValueError):
        return Money.usd("0")


def _coerce_instance_state(value: str) -> str:
    return value if value in _VALID_INSTANCE_STATES else "unknown"


def _coerce_alarm_state(value: str) -> AlarmState:
    try:
        return AlarmState(value)
    except ValueError:
        return AlarmState.INSUFFICIENT_DATA


def _normalise_comparison(value: str) -> Literal["GreaterThanThreshold", "LessThanThreshold"]:
    """Collapse AWS's seven comparison operators onto the two we model.

    The frontend only needs to know whether "over threshold" means above or
    below, so ``GreaterThanOrEqualToThreshold`` must not silently behave like
    ``LessThanThreshold`` (which is what a naive equality check does).
    """
    return "GreaterThanThreshold" if value.startswith("GreaterThan") else "LessThanThreshold"


def _parse_state_value(reason: str) -> float:
    """Extract the triggering datapoint from an alarm's StateReason."""
    match = _STATE_VALUE_RE.search(reason or "")
    if not match:
        return 0.0
    try:
        return float(match.group(1))
    except ValueError:  # pragma: no cover - regex guarantees a number
        return 0.0


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
        self._clients: dict[tuple[str, str], Any] = {}
        try:
            self._session = boto3.Session(profile_name=profile, region_name=region)
        except (BotoCoreError, NoCredentialsError) as exc:  # pragma: no cover
            raise ProviderUnavailableError(f"Could not create AWS session: {exc}") from exc

    # -- plumbing ---------------------------------------------------------
    def _client(self, service: str, *, region: str | None = None) -> Any:
        """Return a cached boto3 client for ``(service, region)``."""
        key = (service, region or self.region)
        client = self._clients.get(key)
        if client is None:
            # boto3 types the service name as a Literal union, which a plain
            # ``str`` cannot satisfy. The call is correct; the overload simply
            # cannot be resolved statically.
            client = self._session.client(  # type: ignore[call-overload]
                service, config=self._config, region_name=key[1]
            )
            self._clients[key] = client
        return client

    @staticmethod
    def _raise_upstream(exc: ClientError, action: str) -> None:
        err = exc.response.get("Error", {})
        code = err.get("Code", "Unknown")
        message = err.get("Message", str(exc))
        raise UpstreamAWSError(f"AWS {action} failed", detail=f"{code}: {message}") from exc

    # -- S3 ---------------------------------------------------------------
    def list_buckets(self) -> list[Bucket]:
        s3 = self._client("s3")
        try:
            response = s3.list_buckets()
        except ClientError as exc:
            self._raise_upstream(exc, "s3:ListBuckets")

        listed = response.get("Buckets", [])
        regions = {raw["Name"]: self._bucket_region(s3, raw["Name"]) for raw in listed}
        usage = self._bucket_usage(regions)

        buckets: list[Bucket] = []
        for raw in listed:
            name = raw["Name"]
            region = regions[name]
            size_bytes, objects = usage.get(name, (0, 0))
            monthly = (Decimal(size_bytes) / Decimal(1024**3)) * _S3_STANDARD_RATE
            buckets.append(
                Bucket(
                    name=name,
                    region=region,
                    size_bytes=size_bytes,
                    object_count=objects,
                    # Per-object storage classes are only visible in inventory
                    # reports, not the ListBuckets API.
                    storage_class=StorageClass.STANDARD,
                    created_at=raw.get("CreationDate", datetime.now(UTC)),
                    versioning=self._bucket_versioning(s3, name),
                    # The API exposes no single "is encrypted" flag; SSE-S3 is the
                    # default for new buckets, so this is an optimistic default
                    # rather than a verified fact.
                    encrypted=True,
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

    def _bucket_usage(self, regions: dict[str, str]) -> dict[str, tuple[int, int]]:
        """Fetch size and object count for every bucket, batched per region.

        Two metrics per bucket, one ``GetMetricData`` call per region (chunked to
        the 500-query limit). The first draft issued a separate call per bucket,
        which is 100+ round trips on a real account.
        """
        results: dict[str, tuple[int, int]] = {}
        by_region: dict[str, list[str]] = {}
        for name, region in regions.items():
            by_region.setdefault(region, []).append(name)

        end = datetime.now(UTC)
        start = end - timedelta(days=3)

        for region, names in by_region.items():
            cw = self._client("cloudwatch", region=region)
            # 2 queries per bucket; stay under the per-call limit.
            for offset in range(0, len(names), _METRIC_QUERY_LIMIT // 2):
                chunk = names[offset : offset + _METRIC_QUERY_LIMIT // 2]
                queries: list[dict[str, Any]] = []
                index: dict[str, tuple[str, str]] = {}
                for idx, name in enumerate(chunk):
                    size_id, count_id = f"s{idx}", f"c{idx}"
                    index[size_id] = (name, "size")
                    index[count_id] = (name, "count")
                    queries.append(
                        {
                            "Id": size_id,
                            "MetricStat": {
                                "Metric": {
                                    "Namespace": "AWS/S3",
                                    "MetricName": "BucketSizeBytes",
                                    "Dimensions": [
                                        {"Name": "BucketName", "Value": name},
                                        # Required by the metric definition. Data
                                        # in other storage classes is not counted.
                                        {"Name": "StorageType", "Value": "StandardStorage"},
                                    ],
                                },
                                "Period": 86400,
                                "Stat": "Average",
                            },
                            "ReturnData": True,
                        }
                    )
                    queries.append(
                        {
                            "Id": count_id,
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
                            "ReturnData": True,
                        }
                    )

                try:
                    resp = cw.get_metric_data(
                        MetricDataQueries=queries, StartTime=start, EndTime=end
                    )
                except ClientError as exc:
                    # S3 request metrics are opt-in on most buckets; absence is
                    # normal, so this must never fail the listing.
                    log.info(
                        "bucket usage unavailable in %s: %s",
                        region,
                        exc.response.get("Error", {}).get("Code", "Unknown"),
                    )
                    continue

                for result in resp.get("MetricDataResults", []):
                    mapped = index.get(result.get("Id", ""))
                    points = result.get("Values") or []
                    if not mapped or not points:
                        continue
                    name, kind = mapped
                    size, objects = results.get(name, (0, 0))
                    latest = int(points[-1])
                    results[name] = (latest, objects) if kind == "size" else (size, latest)

        return results

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
                        if itype not in _STATIC_INSTANCE_PRICES:
                            log.debug("no static price for instance type %s", itype)
                        instances.append(
                            Instance(
                                id=raw["InstanceId"],
                                type=itype,
                                state=_coerce_instance_state(state),
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
                        # Respect the paginator: a fleet larger than one page must
                        # not be silently truncated.
        except ClientError as exc:
            self._raise_upstream(exc, "ec2:DescribeInstances")

        self._populate_cpu(instances)
        return instances

    def _populate_cpu(self, instances: list[Instance]) -> None:
        """Attach average CPU utilisation from CloudWatch, in batched calls."""
        running = [i for i in instances if i.state == "running"]
        if not running:
            return

        cw = self._client("cloudwatch")
        end = datetime.now(UTC)
        start = end - timedelta(hours=3)

        for offset in range(0, len(running), _METRIC_QUERY_LIMIT):
            chunk = running[offset : offset + _METRIC_QUERY_LIMIT]
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
                for idx, inst in enumerate(chunk)
            ]
            try:
                resp = cw.get_metric_data(MetricDataQueries=queries, StartTime=start, EndTime=end)
            except ClientError as exc:
                log.info(
                    "cpu metrics unavailable: %s",
                    exc.response.get("Error", {}).get("Code", "Unknown"),
                )
                return

            by_query = {f"i{idx}": inst for idx, inst in enumerate(chunk)}
            for result in resp.get("MetricDataResults", []):
                inst = by_query.get(result.get("Id", ""))
                points = result.get("Values") or []
                if inst is not None and points:
                    inst.cpu_utilization_pct = round(sum(points) / len(points), 1)

    # -- CloudWatch alarms ------------------------------------------------
    def list_alarms(self) -> list[Alarm]:
        cw = self._client("cloudwatch")
        alarms: list[Alarm] = []
        try:
            paginator = cw.get_paginator("describe_alarms")
            for page in paginator.paginate():
                for raw in page.get("MetricAlarms", []):
                    alarms.append(
                        Alarm(
                            name=raw["AlarmName"],
                            state=_coerce_alarm_state(raw.get("StateValue", "")),
                            severity=self._severity(raw),
                            namespace=raw.get("Namespace", "custom"),
                            metric=raw.get("MetricName", "unknown"),
                            threshold=float(raw.get("Threshold", 0.0)),
                            current_value=_parse_state_value(raw.get("StateReason", "")),
                            comparison=_normalise_comparison(
                                raw.get("ComparisonOperator", "GreaterThanThreshold")
                            ),
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
    def _costs_by_service(self, start: date, end: date) -> dict[str, tuple[Decimal, Decimal]]:
        """Cost and usage, grouped by service, for ``[start, end)``."""
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

        totals: dict[str, tuple[Decimal, Decimal]] = {}
        for period in response.get("ResultsByTime", []):
            for group in period.get("Groups", []):
                service = group["Keys"][0]
                metrics = group.get("Metrics", {})
                cost = Decimal(metrics.get("UnblendedCost", {}).get("Amount", "0"))
                quantity = Decimal(metrics.get("UsageQuantity", {}).get("Amount", "0"))
                prev_cost, prev_qty = totals.get(service, (Decimal("0"), Decimal("0")))
                totals[service] = (prev_cost + cost, prev_qty + quantity)
        return totals

    def service_costs(self, start: date, end: date) -> list[ServiceCost]:
        current = self._costs_by_service(start, end)
        if not current:
            return []

        # Comparison period, same length, immediately before the current one.
        span = max((end - start).days, 1)
        previous = self._costs_by_service(start - timedelta(days=span), start)

        grand_total = sum((cost for cost, _ in current.values()), Decimal("0")) or Decimal("1")

        # Projection is a linear extrapolation of month-to-date spend: honest,
        # explainable, and derived from the same numbers the user can see.
        today = datetime.now(UTC).date()
        days_in_month = calendar.monthrange(start.year, start.month)[1]
        elapsed = min(max((today - start).days + 1, 1), days_in_month)
        factor = Decimal(days_in_month) / Decimal(elapsed)

        rows = [
            ServiceCost(
                service=service,
                cost=Money.usd(cost),
                projected=Money.usd(cost * factor),
                delta_pct=self._delta(cost, previous.get(service, (Decimal("0"),))[0]),
                usage_quantity=float(quantity),
                share_pct=round(float(cost / grand_total * 100), 1),
            )
            for service, (cost, quantity) in current.items()
        ]
        rows.sort(key=lambda r: r.cost.amount, reverse=True)
        return rows

    @staticmethod
    def _delta(current: Decimal, previous: Decimal) -> float:
        if previous <= 0:
            return 0.0
        return round(float((current - previous) / previous * 100), 1)

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
            # Forecasts need at least a few days of history and are not available
            # in every account/region. Zero is a safe, honest fallback.
            log.info(
                "forecast unavailable: %s", exc.response.get("Error", {}).get("Code", "Unknown")
            )
            return Money.usd("0")

    def cost_trend(self, days: int) -> list[TrendPoint]:
        ce = self._client("ce", region=self.cost_explorer_region)
        end = datetime.now(UTC).date() + timedelta(days=1)  # CE end is exclusive
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
                ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
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
