"""Aggregated overview model backing the dashboard's KPI strip."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from app.models.billing import ServiceCost
from app.models.common import Money


class Kpi(BaseModel):
    key: str
    label: str
    value: str
    unit: str = ""
    delta_pct: float = 0.0
    status: Literal["ok", "warn", "critical"] = "ok"
    spark: list[float] = []


class Overview(BaseModel):
    kpis: list[Kpi]
    spend_month_to_date: Money
    spend_forecast: Money
    budget: Money
    budget_used_pct: float
    buckets: int
    instances: int
    instances_running: int
    alarms_firing: int
    storage_bytes: int
    top_services: list[ServiceCost]
    health_score: int
