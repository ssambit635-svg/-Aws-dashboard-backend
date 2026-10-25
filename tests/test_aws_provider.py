"""AWS provider tests, driven by botocore's Stubber.

This module is the reason these bugs were found: ``app/providers/aws.py`` is the
only code that talks to AWS, so it is the code most likely to break silently.
Every test here runs the real boto3 request/response cycle against recorded
payloads — no network, no credentials, no mocks of our own classes.
"""

from __future__ import annotations

from contextlib import ExitStack
from datetime import UTC, date, datetime
from decimal import Decimal

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber

from app.core.errors import UpstreamAWSError
from app.models.resources import AlarmState
from app.providers.aws import AWSProvider, _normalise_comparison, _parse_state_value


def stub_client(service: str = "ec2", region: str = "us-east-1"):
    """A real boto3 client with fake credentials, ready for Stubber."""
    return boto3.client(
        service,
        region_name=region,
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )


def instance_payload(
    instance_id: str = "i-0123456789abcdef0",
    state: str = "running",
    instance_type: str = "t3.micro",
    **overrides,
) -> dict:
    payload = {
        "InstanceId": instance_id,
        "InstanceType": instance_type,
        "State": {"Name": state},
        "Placement": {"AvailabilityZone": "us-east-1a"},
        "PrivateIpAddress": "10.0.0.10",
        "LaunchTime": datetime(2026, 1, 1, tzinfo=UTC),
        "Tags": [{"Key": "Name", "Value": "web-01"}],
    }
    payload.update(overrides)
    return payload


def describe_response(*instances: dict) -> dict:
    return {"Reservations": [{"Instances": list(instances)}]}


@pytest.fixture
def provider(monkeypatch):
    """An AWSProvider whose boto3 clients are swapped for stubbed ones."""
    p = AWSProvider(region="us-east-1")
    # Keyed by (service, region) exactly like the real cache, otherwise a test
    # that exercises two regions silently gets the same client back.
    clients: dict[tuple[str, str], object] = {}

    def fake_client(service: str, *, region: str | None = None):
        key = (service, region or "us-east-1")
        if key not in clients:
            clients[key] = stub_client(service, region=key[1])
        return clients[key]

    monkeypatch.setattr(p, "_client", fake_client)
    return p


# ── EC2 ───────────────────────────────────────────────────────────────────
def test_shutting_down_state_is_accepted(provider):
    """Regression: AWS returns six states; models that omit 'shutting-down'
    raise ValidationError and 500 the entire /instances endpoint."""
    ec2 = provider._client("ec2")
    with Stubber(ec2) as stubber:
        stubber.add_response(
            "describe_instances", describe_response(instance_payload(state="shutting-down"))
        )
        with Stubber(provider._client("cloudwatch")) as cw_stubber:
            cw_stubber.add_response(
                "get_metric_data", {"MetricDataResults": [{"Id": "i0", "Values": [42.0]}]}
            )
            instances = provider.list_instances()

    assert len(instances) == 1
    assert instances[0].state == "shutting-down"


def test_unrecognised_state_degrades_to_unknown(provider):
    ec2 = provider._client("ec2")
    with Stubber(ec2) as stubber:
        stubber.add_response(
            "describe_instances", describe_response(instance_payload(state="hibernating"))
        )
        with Stubber(provider._client("cloudwatch")) as cw_stubber:
            cw_stubber.assert_no_pending_responses  # noqa: B018 - documents no CPU call expected
            instances = provider.list_instances()
    assert instances[0].state == "unknown"


def test_terminated_instances_are_filtered_out(provider):
    ec2 = provider._client("ec2")
    with Stubber(ec2) as stubber:
        stubber.add_response(
            "describe_instances",
            describe_response(
                instance_payload(instance_id="i-0123456789abcdef0", state="terminated"),
                instance_payload(instance_id="i-0fedcba9876543210", state="running"),
            ),
        )
        with Stubber(provider._client("cloudwatch")) as cw_stubber:
            cw_stubber.add_response(
                "get_metric_data", {"MetricDataResults": [{"Id": "i0", "Values": [10.0]}]}
            )
            instances = provider.list_instances()
    assert [i.id for i in instances] == ["i-0fedcba9876543210"]


def test_paginator_collects_every_page(provider):
    """A fleet larger than one page must not be silently truncated."""
    ec2 = provider._client("ec2")
    with Stubber(ec2) as stubber:
        stubber.add_response(
            "describe_instances",
            {
                "Reservations": [{"Instances": [instance_payload("i-0123456789abcdef0")]}],
                "NextToken": "page2",
            },
        )
        stubber.add_response(
            "describe_instances",
            {"Reservations": [{"Instances": [instance_payload("i-0fedcba9876543210")]}]},
        )
        with Stubber(provider._client("cloudwatch")) as cw_stubber:
            cw_stubber.add_response(
                "get_metric_data",
                {
                    "MetricDataResults": [
                        {"Id": "i0", "Values": [10.0]},
                        {"Id": "i1", "Values": [20.0]},
                    ]
                },
            )
            instances = provider.list_instances()
    assert len(instances) == 2


def test_cpu_is_averaged_from_cloudwatch(provider):
    ec2 = provider._client("ec2")
    with Stubber(ec2) as stubber:
        stubber.add_response("describe_instances", describe_response(instance_payload()))
        with Stubber(provider._client("cloudwatch")) as cw_stubber:
            cw_stubber.add_response(
                "get_metric_data",
                {"MetricDataResults": [{"Id": "i0", "Values": [10.0, 20.0, 30.0]}]},
            )
            instances = provider.list_instances()
    assert instances[0].cpu_utilization_pct == 20.0


def test_cpu_failure_does_not_break_the_listing(provider):
    """CloudWatch permissions are often missing; the fleet list must survive."""
    ec2 = provider._client("ec2")
    with Stubber(ec2) as stubber:
        stubber.add_response("describe_instances", describe_response(instance_payload()))
        with Stubber(provider._client("cloudwatch")) as cw_stubber:
            cw_stubber.add_client_error("get_metric_data", "AccessDenied")
            instances = provider.list_instances()
    assert len(instances) == 1
    assert instances[0].cpu_utilization_pct == 0.0


def test_unknown_instance_type_costs_zero_rather_than_failing(provider):
    ec2 = provider._client("ec2")
    with Stubber(ec2) as stubber:
        stubber.add_response(
            "describe_instances", describe_response(instance_payload(instance_type="x9.mega"))
        )
        with Stubber(provider._client("cloudwatch")) as cw_stubber:
            cw_stubber.add_response("get_metric_data", {"MetricDataResults": []})
            instances = provider.list_instances()
    assert instances[0].monthly_cost.amount == Decimal("0.00")


def test_describe_instances_error_is_translated(provider):
    ec2 = provider._client("ec2")
    with Stubber(ec2) as stubber:
        stubber.add_client_error("describe_instances", "UnauthorizedOperation")
        with pytest.raises(UpstreamAWSError) as exc:
            provider.list_instances()
    assert "UnauthorizedOperation" in (exc.value.detail or "")


def test_clients_are_cached_per_service_and_region():
    """boto3 client construction is expensive; the first draft did it per call."""
    created: list[tuple[str, str]] = []

    class CountingSession:
        def client(self, service, **kwargs):
            created.append((service, kwargs["region_name"]))
            return stub_client(service, kwargs["region_name"])

    p = AWSProvider(region="us-east-1")
    p._session = CountingSession()

    first = p._client("ec2")
    second = p._client("ec2")
    same_region_different_service = p._client("cloudwatch")
    other_region = p._client("ec2", region="eu-west-1")

    assert first is second
    assert same_region_different_service is not first
    assert other_region is not first
    assert created == [
        ("ec2", "us-east-1"),
        ("cloudwatch", "us-east-1"),
        ("ec2", "eu-west-1"),
    ]


# ── CloudWatch alarms ─────────────────────────────────────────────────────
def alarm_payload(**overrides) -> dict:
    payload = {
        "AlarmName": "api-prod-high-cpu",
        "StateValue": "ALARM",
        "Namespace": "AWS/EC2",
        "MetricName": "CPUUtilization",
        "Threshold": 80.0,
        "ComparisonOperator": "GreaterThanOrEqualToThreshold",
        "StateReason": (
            "Threshold Crossed: 1 datapoint [91.44 (25/10/26 07:00:00)] was greater than "
            "the threshold (80.0)."
        ),
        "StateUpdatedTimestamp": datetime(2026, 10, 25, 7, tzinfo=UTC),
    }
    payload.update(overrides)
    return payload


def test_alarm_state_value_is_parsed_from_state_reason(provider):
    cw = provider._client("cloudwatch")
    with Stubber(cw) as stubber:
        stubber.add_response("describe_alarms", {"MetricAlarms": [alarm_payload()]})
        alarms = provider.list_alarms()
    assert alarms[0].current_value == 91.44


def test_alarm_without_numeric_state_reason_reports_zero(provider):
    cw = provider._client("cloudwatch")
    with Stubber(cw) as stubber:
        stubber.add_response(
            "describe_alarms",
            {
                "MetricAlarms": [
                    alarm_payload(StateReason="Insufficient Data: 1 datapoint was unknown")
                ]
            },
        )
        alarms = provider.list_alarms()
    assert alarms[0].current_value == 0.0


@pytest.mark.parametrize(
    "operator,expected",
    [
        ("GreaterThanThreshold", "GreaterThanThreshold"),
        ("GreaterThanOrEqualToThreshold", "GreaterThanThreshold"),
        ("LessThanThreshold", "LessThanThreshold"),
        ("LessThanOrEqualToThreshold", "LessThanThreshold"),
    ],
)
def test_comparison_operator_is_normalised(operator, expected):
    """AWS has seven operators; all 'over threshold' variants mean 'above'."""
    assert _normalise_comparison(operator) == expected


def test_unknown_alarm_state_degrades(provider):
    cw = provider._client("cloudwatch")
    with Stubber(cw) as stubber:
        stubber.add_response(
            "describe_alarms", {"MetricAlarms": [alarm_payload(StateValue="WEIRD")]}
        )
        alarms = provider.list_alarms()
    assert alarms[0].state is AlarmState.INSUFFICIENT_DATA


def test_alarm_severity_follows_state(provider):
    cw = provider._client("cloudwatch")
    with Stubber(cw) as stubber:
        stubber.add_response(
            "describe_alarms",
            {
                "MetricAlarms": [
                    alarm_payload(AlarmName="a", StateValue="ALARM"),
                    alarm_payload(AlarmName="b", StateValue="INSUFFICIENT_DATA"),
                    alarm_payload(AlarmName="c", StateValue="OK"),
                ]
            },
        )
        alarms = provider.list_alarms()
    assert [a.severity.value for a in alarms] == ["critical", "warning", "info"]


@pytest.mark.parametrize(
    "reason,expected",
    [
        ("Threshold Crossed: 1 datapoint [91.44 (25/10/26 07:00:00)] was greater", 91.44),
        ("Threshold Crossed: [-5.5 ...]", -5.5),
        ("no brackets here", 0.0),
        ("", 0.0),
        ("[not-a-number]", 0.0),
    ],
)
def test_parse_state_value(reason, expected):
    assert _parse_state_value(reason) == expected


# ── Cost Explorer ─────────────────────────────────────────────────────────
def cost_response(*groups: tuple[str, str, str]) -> dict:
    return {
        "ResultsByTime": [
            {
                "TimePeriod": {"Start": "2026-10-01", "End": "2026-10-25"},
                "Groups": [
                    {
                        "Keys": [service],
                        "Metrics": {
                            "UnblendedCost": {"Amount": cost},
                            "UsageQuantity": {"Amount": quantity},
                        },
                    }
                    for service, cost, quantity in groups
                ],
            }
        ]
    }


def test_service_costs_computes_delta_against_the_previous_period(provider):
    ce = provider._client("ce")
    start, end = date(2026, 10, 1), date(2026, 10, 25)
    with Stubber(ce) as stubber:
        stubber.add_response("get_cost_and_usage", cost_response(("Amazon EC2", "150.00", "100")))
        stubber.add_response("get_cost_and_usage", cost_response(("Amazon EC2", "100.00", "80")))
        rows = provider.service_costs(start, end)
    assert rows[0].delta_pct == 50.0  # +50% versus the comparison window


def test_service_costs_shares_sum_to_one_hundred(provider):
    ce = provider._client("ce")
    with Stubber(ce) as stubber:
        stubber.add_response(
            "get_cost_and_usage",
            cost_response(
                ("Amazon EC2", "300.00", "1"),
                ("Amazon S3", "100.00", "2"),
                ("AWS Lambda", "12.50", "3"),
            ),
        )
        stubber.add_response("get_cost_and_usage", cost_response())
        rows = provider.service_costs(date(2026, 10, 1), date(2026, 10, 25))
    assert sum(row.share_pct for row in rows) == pytest.approx(100.0, abs=0.2)
    assert [r.service for r in rows] == ["Amazon EC2", "Amazon S3", "AWS Lambda"]


def test_service_costs_projection_extrapolates_month_to_date(provider):
    ce = provider._client("ce")
    with Stubber(ce) as stubber:
        stubber.add_response("get_cost_and_usage", cost_response(("Amazon EC2", "100.00", "1")))
        stubber.add_response("get_cost_and_usage", cost_response())
        rows = provider.service_costs(date(2026, 10, 1), date(2026, 10, 25))
    # Projection must exceed actual spend (the month is not over yet).
    assert rows[0].projected.amount > rows[0].cost.amount


def test_service_costs_handles_a_new_service_with_no_history(provider):
    ce = provider._client("ce")
    with Stubber(ce) as stubber:
        stubber.add_response("get_cost_and_usage", cost_response(("Amazon Bedrock", "42.00", "1")))
        stubber.add_response("get_cost_and_usage", cost_response())
        rows = provider.service_costs(date(2026, 10, 1), date(2026, 10, 25))
    assert rows[0].delta_pct == 0.0  # no division by zero
    assert rows[0].share_pct == 100.0


def test_service_costs_empty_response_returns_empty_list(provider):
    ce = provider._client("ce")
    with Stubber(ce) as stubber:
        stubber.add_response("get_cost_and_usage", {"ResultsByTime": []})
        stubber.add_response("get_cost_and_usage", {"ResultsByTime": []})
        assert provider.service_costs(date(2026, 10, 1), date(2026, 10, 25)) == []


def test_cost_trend_parses_daily_groups(provider):
    ce = provider._client("ce")
    with Stubber(ce) as stubber:
        stubber.add_response(
            "get_cost_and_usage",
            {
                "ResultsByTime": [
                    {
                        "TimePeriod": {"Start": "2026-10-24", "End": "2026-10-25"},
                        "Groups": [
                            {
                                "Keys": ["Amazon EC2"],
                                "Metrics": {"UnblendedCost": {"Amount": "12.345"}},
                            },
                            {
                                "Keys": ["Amazon S3"],
                                "Metrics": {"UnblendedCost": {"Amount": "0.5"}},
                            },
                        ],
                    }
                ]
            },
        )
        points = provider.cost_trend(7)
    assert points[0].date == date(2026, 10, 24)
    assert points[0].total.amount == Decimal("12.85")
    assert points[0].by_service["Amazon S3"] == "0.50"


def test_forecast_returns_zero_when_unavailable(provider):
    ce = provider._client("ce")
    with Stubber(ce) as stubber:
        stubber.add_client_error("get_cost_forecast", "DataUnavailableException")
        assert provider.forecast().amount == Decimal("0.00")


def test_forecast_parses_total(provider):
    ce = provider._client("ce")
    with Stubber(ce) as stubber:
        stubber.add_response("get_cost_forecast", {"Total": {"Amount": "7412.55", "Unit": "USD"}})
        assert provider.forecast().amount == Decimal("7412.55")


# ── S3 ────────────────────────────────────────────────────────────────────
def test_buckets_get_region_and_usage_from_batched_metrics(provider):
    s3 = provider._client("s3")
    with Stubber(s3) as s3_stubber:
        s3_stubber.add_response(
            "list_buckets",
            {
                "Buckets": [
                    {"Name": "alpha", "CreationDate": datetime(2025, 1, 1, tzinfo=UTC)},
                    {"Name": "beta", "CreationDate": datetime(2025, 2, 1, tzinfo=UTC)},
                ]
            },
        )
        # "alpha" lives in eu-west-1 (LocationConstraint), beta in us-east-1 (None)
        s3_stubber.add_response(
            "get_bucket_location", {"LocationConstraint": "eu-west-1"}, {"Bucket": "alpha"}
        )
        # Real AWS omits LocationConstraint for us-east-1 rather than returning
        # null; the provider must treat an absent key as us-east-1.
        s3_stubber.add_response("get_bucket_location", {}, {"Bucket": "beta"})
        # Responses are queued in the provider's call order: it enriches each
        # bucket in a single pass, so versioning and public-access calls
        # interleave per bucket rather than grouping by operation.
        s3_stubber.add_response("get_bucket_versioning", {"Status": "Enabled"}, {"Bucket": "alpha"})
        s3_stubber.add_response(
            "get_public_access_block",
            {
                "PublicAccessBlockConfiguration": {
                    "BlockPublicAcls": True,
                    "IgnorePublicAcls": True,
                    "BlockPublicPolicy": True,
                    "RestrictPublicBuckets": True,
                }
            },
            {"Bucket": "alpha"},
        )
        s3_stubber.add_response("get_bucket_versioning", {}, {"Bucket": "beta"})
        s3_stubber.add_client_error(
            "get_public_access_block",
            "NoSuchPublicAccessBlockConfiguration",
            expected_params={"Bucket": "beta"},
        )

        # Stubbers must be active while the provider runs: one batched
        # GetMetricData per region, not one per bucket.
        with ExitStack() as stack:
            cw_eu = stack.enter_context(Stubber(provider._client("cloudwatch", region="eu-west-1")))
            cw_eu.add_response(
                "get_metric_data",
                {
                    "MetricDataResults": [
                        {"Id": "s0", "Values": [2.0 * 1024**3]},
                        {"Id": "c0", "Values": [10.0]},
                    ]
                },
            )
            cw_us = stack.enter_context(Stubber(provider._client("cloudwatch", region="us-east-1")))
            cw_us.add_response(
                "get_metric_data",
                {
                    "MetricDataResults": [
                        {"Id": "s0", "Values": [1.0 * 1024**3]},
                        {"Id": "c0", "Values": [5.0]},
                    ]
                },
            )

            buckets = provider.list_buckets()

            # Exactly one call per region: proves the batching works.
            cw_eu.assert_no_pending_responses()
            cw_us.assert_no_pending_responses()

    by_name = {b.name: b for b in buckets}
    assert by_name["alpha"].region == "eu-west-1"
    assert by_name["beta"].region == "us-east-1"  # None means us-east-1
    assert by_name["alpha"].size_bytes == 2 * 1024**3
    assert by_name["alpha"].object_count == 10
    assert by_name["alpha"].versioning is True
    assert by_name["beta"].versioning is False
    assert by_name["alpha"].public_access_blocked is True
    assert by_name["beta"].public_access_blocked is False
    # 2 GiB * $0.023/GiB-month
    assert by_name["alpha"].monthly_cost.amount == Decimal("0.05")


def test_bucket_usage_failure_does_not_break_the_listing(provider):
    s3 = provider._client("s3")
    with Stubber(s3) as s3_stubber:
        s3_stubber.add_response(
            "list_buckets",
            {"Buckets": [{"Name": "alpha", "CreationDate": datetime(2025, 1, 1, tzinfo=UTC)}]},
        )
        s3_stubber.add_response("get_bucket_location", {}, {"Bucket": "alpha"})
        s3_stubber.add_response("get_bucket_versioning", {}, {"Bucket": "alpha"})
        s3_stubber.add_client_error(
            "get_public_access_block", "AccessDenied", expected_params={"Bucket": "alpha"}
        )
        with Stubber(provider._client("cloudwatch")) as cw_stubber:
            cw_stubber.add_client_error("get_metric_data", "AccessDenied")
            buckets = provider.list_buckets()
    assert buckets[0].size_bytes == 0  # honest zero, not a fabricated size


def test_list_buckets_error_is_translated(provider):
    s3 = provider._client("s3")
    with Stubber(s3) as stubber:
        stubber.add_client_error("list_buckets", "AccessDenied")
        with pytest.raises(UpstreamAWSError) as exc:
            provider.list_buckets()
    assert "AccessDenied" in (exc.value.detail or "")


# ── health ────────────────────────────────────────────────────────────────
def test_health_reports_ok_when_sts_answers(provider):
    sts = provider._client("sts")
    with Stubber(sts) as stubber:
        stubber.add_response("get_caller_identity", {"Account": "123456789012"})
        assert provider.health()["provider"] == "ok"


def test_health_reports_degraded_without_touching_sts_permissions(provider):
    sts = provider._client("sts")
    with Stubber(sts) as stubber:
        stubber.add_client_error("get_caller_identity", "AccessDenied")
        result = provider.health()
    assert result["provider"] == "degraded"
    assert "ClientError" in result["sts"]


def test_client_error_without_error_block_does_not_crash():
    """A malformed ClientError must still produce a useful message."""
    exc = ClientError({}, "DescribeInstances")
    with pytest.raises(UpstreamAWSError) as raised:
        AWSProvider._raise_upstream(exc, "ec2:DescribeInstances")
    assert "Unknown" in (raised.value.detail or "")
