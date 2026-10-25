"""Cost and billing endpoints."""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, Query, Request

from app.core.deps import cloud_service, container, make_meta
from app.core.security import require_api_key
from app.models.billing import BillingSummary, BillingTrend
from app.models.common import Envelope, Money
from app.services.cloud import CloudService
from app.services.container import Container

router = APIRouter(
    prefix="/api/v1/billing", tags=["billing"], dependencies=[Depends(require_api_key)]
)


@router.get(
    "/summary", response_model=Envelope[BillingSummary], summary="Month-to-date spend by service"
)
def billing_summary(
    request: Request,
    svc: CloudService = Depends(cloud_service),
    ctr: Container = Depends(container),
) -> Envelope[BillingSummary]:
    services, cached = svc.service_costs()
    start, end = svc.current_period()
    total = sum((row.cost.amount for row in services), Decimal("0"))
    payload = BillingSummary(
        period_start=start,
        period_end=end,
        total=Money.usd(total),
        forecast=svc.forecast(),
        budget=Money.usd(ctr.settings.monthly_budget),
        budget_used_pct=round(float(total / Decimal(str(ctr.settings.monthly_budget)) * 100), 1)
        if ctr.settings.monthly_budget
        else 0.0,
        services=services,
        currency=ctr.settings.cost_currency,
    )
    return Envelope[BillingSummary](data=payload, meta=make_meta(request, ctr, cached))


@router.get("/trend", response_model=Envelope[BillingTrend], summary="Daily spend trend")
def billing_trend(
    request: Request,
    days: int = Query(30, ge=1, le=90),
    svc: CloudService = Depends(cloud_service),
    ctr: Container = Depends(container),
) -> Envelope[BillingTrend]:
    points, cached = svc.trend(days)
    services: list[str] = []
    for point in points:
        for service in point.by_service:
            if service not in services:
                services.append(service)
    total = sum((p.total.amount for p in points), Decimal("0"))
    peak = max(points, key=lambda p: p.total.amount) if points else None
    payload = BillingTrend(
        days=len(points),
        points=points,
        services=services,
        total=Money.usd(total),
        daily_average=Money.usd(total / len(points) if points else Decimal("0")),
        peak=peak,
    )
    return Envelope[BillingTrend](data=payload, meta=make_meta(request, ctr, cached))


@router.get("/forecast", summary="Forecasted month-end spend")
def forecast(
    request: Request,
    svc: CloudService = Depends(cloud_service),
    ctr: Container = Depends(container),
) -> dict:
    value = svc.forecast()
    return {
        "data": {
            "forecast": value.model_dump(),
            "budget": Money.usd(ctr.settings.monthly_budget).model_dump(),
            "will_exceed_budget": float(value.amount) > ctr.settings.monthly_budget,
        },
        "meta": make_meta(request, ctr).model_dump(mode="json"),
    }
