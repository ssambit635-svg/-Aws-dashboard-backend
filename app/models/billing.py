"""Billing / cost models."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field

from app.models.common import Money


class ServiceCost(BaseModel):
    service: str
    cost: Money
    projected: Money
    delta_pct: float = Field(description="Percentage change vs. the comparison period")
    usage_hours: int = Field(ge=0)
    share_pct: float = Field(ge=0, le=100)


class BillingSummary(BaseModel):
    period_start: date
    period_end: date
    total: Money
    forecast: Money
    budget: Money
    budget_used_pct: float
    services: list[ServiceCost]
    currency: str


class TrendPoint(BaseModel):
    date: date
    total: Money
    by_service: dict[str, str]


class BillingTrend(BaseModel):
    days: int
    points: list[TrendPoint]
    services: list[str]
    total: Money
    daily_average: Money
    peak: TrendPoint | None = None
