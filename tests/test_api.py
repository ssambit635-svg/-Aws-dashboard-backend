"""HTTP contract tests: status codes, envelopes, auth, pagination, validation."""

from __future__ import annotations

from datetime import UTC, date

import pytest

from app.core.errors import ProviderUnavailableError
from tests.conftest import API_KEY

PROTECTED = [
    "/api/v1/overview",
    "/api/v1/buckets",
    "/api/v1/instances",
    "/api/v1/alarms",
    "/api/v1/billing/summary",
    "/api/v1/billing/trend",
    "/api/v1/billing/forecast",
]


# ── health ────────────────────────────────────────────────────────────────
def test_livez_is_public(client):
    response = client.get("/livez")
    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


def test_readyz_reports_provider(client):
    response = client.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["provider"] == "mock"
    assert body["checks"]["provider"] == "ok"


# ── authentication ────────────────────────────────────────────────────────
@pytest.mark.parametrize("path", PROTECTED)
def test_protected_routes_require_a_key(client, path):
    response = client.get(path)
    assert response.status_code == 401
    error = response.json()["error"]
    assert error["code"] == "unauthorized"
    assert error["request_id"]


@pytest.mark.parametrize("path", PROTECTED)
def test_protected_routes_reject_wrong_key(client, path):
    response = client.get(path, headers={"X-API-Key": "nope"})
    assert response.status_code == 401


@pytest.mark.parametrize("path", PROTECTED)
def test_protected_routes_accept_valid_key(client, path):
    response = client.get(path, headers={"X-API-Key": API_KEY})
    assert response.status_code == 200


def test_bearer_token_is_accepted(client):
    response = client.get("/api/v1/overview", headers={"Authorization": f"Bearer {API_KEY}"})
    assert response.status_code == 200


def test_docs_are_public(authed):
    assert authed.get("/docs").status_code == 200
    assert authed.get("/openapi.json").status_code == 200


# ── response envelope ─────────────────────────────────────────────────────
def test_overview_envelope_shape(authed):
    body = authed.get("/api/v1/overview").json()
    assert set(body) == {"data", "meta"}
    data, meta = body["data"], body["meta"]

    assert meta["provider"] == "mock"
    assert meta["request_id"]
    assert len(data["kpis"]) == 5
    assert 0 <= data["health_score"] <= 100
    assert data["budget_used_pct"] >= 0
    assert len(data["top_services"]) == 5
    # money is a Decimal serialised as a fixed-precision string, never a float
    assert isinstance(data["spend_month_to_date"]["amount"], str)
    assert data["spend_month_to_date"]["amount"].count(".") == 1
    assert len(data["spend_month_to_date"]["amount"].split(".")[1]) == 2


def test_request_id_header_is_echoed(authed):
    response = authed.get("/api/v1/overview", headers={"X-Request-ID": "abc123"})
    assert response.headers["X-Request-ID"] == "abc123"
    assert response.json()["meta"]["request_id"] == "abc123"


def test_response_time_header(authed):
    response = authed.get("/livez")
    assert float(response.headers["X-Response-Time-ms"]) >= 0


# ── buckets ───────────────────────────────────────────────────────────────
def test_buckets_pagination(authed):
    page1 = authed.get("/api/v1/buckets?page=1&page_size=2").json()
    assert len(page1["data"]["items"]) == 2
    pg = page1["meta"]["pagination"]
    assert pg["page"] == 1
    assert pg["page_size"] == 2
    assert pg["total"] == 5
    assert pg["total_pages"] == 3
    assert pg["has_next"] is True

    page3 = authed.get("/api/v1/buckets?page=3&page_size=2").json()
    assert len(page3["data"]["items"]) == 1
    assert page3["meta"]["pagination"]["has_next"] is False


def test_buckets_aggregates_cover_all_rows_not_just_page(authed):
    """A paginated list must still report account-wide totals."""
    body = authed.get("/api/v1/buckets?page=1&page_size=1").json()["data"]
    assert len(body["items"]) == 1
    assert body["total_objects"] == sum(
        b["object_count"] for b in authed.get("/api/v1/buckets").json()["data"]["items"]
    )


def test_bucket_fields_are_well_formed(authed):
    bucket = authed.get("/api/v1/buckets").json()["data"]["items"][0]
    assert bucket["name"]
    assert bucket["size_bytes"] >= 0
    assert bucket["storage_class"] in {
        "STANDARD",
        "STANDARD_IA",
        "INTELLIGENT_TIERING",
        "GLACIER",
        "DEEP_ARCHIVE",
    }
    assert bucket["created_at"].endswith("Z") or "+" in bucket["created_at"]


def test_bucket_region_filter(authed):
    body = authed.get("/api/v1/buckets?region=eu-west-1").json()["data"]
    assert body["items"] == []


def test_page_size_is_bounded(authed):
    assert authed.get("/api/v1/buckets?page_size=1000").status_code == 422
    assert authed.get("/api/v1/buckets?page=0").status_code == 422


# ── instances ─────────────────────────────────────────────────────────────
def test_instance_ids_match_aws_format(authed):
    import re

    pattern = re.compile(r"^i-(?:[0-9a-f]{8}|[0-9a-f]{17})$")
    for instance in authed.get("/api/v1/instances").json()["data"]["items"]:
        assert pattern.match(instance["id"]), instance["id"]


def test_instance_state_filter_and_counts(authed):
    body = authed.get("/api/v1/instances?state=running").json()["data"]
    assert all(i["state"] == "running" for i in body["items"])
    assert sum(body["by_state"].values()) == 8


def test_cpu_and_memory_are_percentages(authed):
    for instance in authed.get("/api/v1/instances").json()["data"]["items"]:
        assert 0 <= instance["cpu_utilization_pct"] <= 100
        assert 0 <= instance["memory_used_pct"] <= 100


# ── alarms ────────────────────────────────────────────────────────────────
def test_alarms_filter_by_state(authed):
    body = authed.get("/api/v1/alarms?state=ALARM").json()["data"]
    assert body["items"]
    assert all(a["state"] == "ALARM" for a in body["items"])
    # by_state describes the whole account, not the filtered slice, because the
    # UI renders it as filter chips whose counts must not shift when filtering.
    assert body["by_state"] == {"ALARM": 3, "OK": 2, "INSUFFICIENT_DATA": 1}


def test_alarm_state_enum_is_enforced(authed):
    assert authed.get("/api/v1/alarms?state=BANANA").status_code == 422


# ── billing ───────────────────────────────────────────────────────────────
def test_billing_total_equals_sum_of_services(authed):
    from decimal import Decimal

    body = authed.get("/api/v1/billing/summary").json()["data"]
    total = Decimal(body["total"]["amount"])
    summed = sum(Decimal(row["cost"]["amount"]) for row in body["services"])
    assert abs(total - summed) <= Decimal("0.05")


def test_billing_shares_sum_to_about_100(authed):
    body = authed.get("/api/v1/billing/summary").json()["data"]
    assert 99.0 <= sum(row["share_pct"] for row in body["services"]) <= 101.0


def test_billing_period_is_current_month(authed):
    body = authed.get("/api/v1/billing/summary").json()["data"]
    start = date.fromisoformat(body["period_start"])
    assert start.day == 1
    assert start <= date.fromisoformat(body["period_end"])


def test_billing_rows_expose_usage_quantity_not_hours(authed):
    """Cost Explorer's UsageQuantity is mixed-unit; calling it 'hours' was a lie."""
    row = authed.get("/api/v1/billing/summary").json()["data"]["services"][0]
    assert "usage_quantity" in row
    assert "usage_hours" not in row
    assert row["usage_quantity"] >= 0


def test_services_sorted_by_cost_desc(authed):
    services = authed.get("/api/v1/billing/summary").json()["data"]["services"]
    costs = [float(row["cost"]["amount"]) for row in services]
    assert costs == sorted(costs, reverse=True)


# ── trend ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("days", [7, 30, 90])
def test_trend_length_and_dates(authed, days):
    body = authed.get(f"/api/v1/billing/trend?days={days}").json()["data"]
    assert body["days"] == days
    assert len(body["points"]) == days
    for point in body["points"]:
        assert date.fromisoformat(point["date"])  # strict ISO-8601, not "Oct 25"
        assert point["by_service"]


def test_trend_is_chronological_and_ends_today(authed):
    from datetime import datetime

    points = authed.get("/api/v1/billing/trend?days=10").json()["data"]["points"]
    dates = [date.fromisoformat(p["date"]) for p in points]
    assert dates == sorted(dates)
    assert dates[-1] == datetime.now(UTC).date()


def test_trend_rejects_out_of_range(authed):
    assert authed.get("/api/v1/billing/trend?days=0").status_code == 422
    assert authed.get("/api/v1/billing/trend?days=365").status_code == 422


def test_trend_peak_matches_max_point(authed):
    body = authed.get("/api/v1/billing/trend?days=14").json()["data"]
    peak = max(body["points"], key=lambda p: float(p["total"]["amount"]))
    assert body["peak"]["date"] == peak["date"]


# ── forecast ──────────────────────────────────────────────────────────────
def test_forecast_endpoint(authed):
    body = authed.get("/api/v1/billing/forecast").json()["data"]
    assert "forecast" in body and "budget" in body
    assert isinstance(body["will_exceed_budget"], bool)


# ── system ────────────────────────────────────────────────────────────────
def test_system_info_never_leaks_secrets(authed):
    body = authed.get("/api/v1/system/info").json()
    serialised = str(body)
    assert API_KEY not in serialised
    assert "api_key" not in {k.lower() for k in body["settings"]}


def test_system_info_lists_endpoints(authed):
    body = authed.get("/api/v1/system/info").json()
    assert "/api/v1/overview" in body["endpoints"]
    assert body["provider"]["mode"] == "mock"


# ── errors ────────────────────────────────────────────────────────────────
def test_unknown_api_route_returns_json_error(authed):
    response = authed.get("/api/v1/does-not-exist")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_unknown_page_route_returns_html(authed):
    response = authed.get("/nope")
    assert response.status_code == 404
    assert "text/html" in response.headers["content-type"]


def test_validation_error_shape(authed):
    response = authed.get("/api/v1/buckets?page=abc")
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert error["fields"]
    assert error["request_id"]


def test_security_headers_present(authed):
    response = authed.get("/livez")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "no-referrer"


def test_gzip_applied_to_large_payloads(authed):
    response = authed.get("/api/v1/instances?page_size=100", headers={"Accept-Encoding": "gzip"})
    assert response.status_code == 200
    assert response.headers.get("content-encoding") == "gzip"


def test_vary_header_advertises_compression(authed):
    """Without Vary a shared cache can serve gzip to a client that cannot read it."""
    response = authed.get("/api/v1/buckets", headers={"Accept-Encoding": "gzip"})
    assert "accept-encoding" in response.headers.get("vary", "").lower()


def test_cors_exposes_the_request_id_header(authed):
    response = authed.get("/api/v1/overview", headers={"Origin": "http://localhost:8000"})
    exposed = response.headers.get("access-control-expose-headers", "").lower()
    assert "x-request-id" in exposed


# ── provider failure handling ─────────────────────────────────────────────
def test_provider_failure_maps_to_503(authed, monkeypatch, container):
    def explode():
        raise ProviderUnavailableError("AWS is unhappy", detail="ThrottlingException")

    monkeypatch.setattr(container.provider, "list_buckets", explode)
    response = authed.get("/api/v1/buckets")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "provider_unavailable"


def test_unauthenticated_before_provider_work(client, monkeypatch):
    """Auth must be checked before any upstream call is attempted."""

    def should_not_run():
        raise AssertionError("provider was called before authentication")

    from app.services.container import get_container

    monkeypatch.setattr(get_container().provider, "list_buckets", should_not_run)
    assert client.get("/api/v1/buckets").status_code == 401
