"""S3 buckets, EC2 instances and CloudWatch alarms."""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, Query, Request

from app.core.deps import cloud_service, container, make_meta
from app.core.security import require_api_key
from app.models.common import Envelope, Money
from app.models.resources import (
    AlarmList,
    BucketList,
    InstanceList,
)
from app.services.cloud import CloudService
from app.services.container import Container

router = APIRouter(prefix="/api/v1", tags=["resources"], dependencies=[Depends(require_api_key)])


@router.get("/buckets", response_model=Envelope[BucketList], summary="List S3 buckets")
def list_buckets(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    region: str | None = Query(None, description="Filter by region"),
    svc: CloudService = Depends(cloud_service),
    ctr: Container = Depends(container),
) -> Envelope[BucketList]:
    buckets, cached = svc.buckets()
    if region:
        buckets = [b for b in buckets if b.region == region]
    page_items, pagination = svc.paginate(buckets, page, page_size)
    payload = BucketList(
        items=page_items,
        total_size_bytes=sum(b.size_bytes for b in buckets),
        total_objects=sum(b.object_count for b in buckets),
        total_monthly_cost=Money.usd(sum((b.monthly_cost.amount for b in buckets), Decimal("0"))),
    )
    return Envelope[BucketList](data=payload, meta=make_meta(request, ctr, cached, pagination))


@router.get("/instances", response_model=Envelope[InstanceList], summary="List EC2 instances")
def list_instances(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    state: str | None = Query(None, description="Filter by instance state"),
    svc: CloudService = Depends(cloud_service),
    ctr: Container = Depends(container),
) -> Envelope[InstanceList]:
    instances, cached = svc.instances()
    # Aggregates describe the whole fleet, not the filtered slice: the client
    # renders these counts as filter chips, so they must stay stable.
    by_state: dict[str, int] = {}
    for inst in instances:
        by_state[inst.state] = by_state.get(inst.state, 0) + 1
    if state:
        instances = [i for i in instances if i.state == state]
    page_items, pagination = svc.paginate(instances, page, page_size)
    payload = InstanceList(items=page_items, by_state=by_state)
    return Envelope[InstanceList](data=payload, meta=make_meta(request, ctr, cached, pagination))


@router.get("/alarms", response_model=Envelope[AlarmList], summary="List CloudWatch alarms")
def list_alarms(
    request: Request,
    state: str | None = Query(None, pattern="^(OK|ALARM|INSUFFICIENT_DATA)$"),
    svc: CloudService = Depends(cloud_service),
    ctr: Container = Depends(container),
) -> Envelope[AlarmList]:
    alarms, cached = svc.alarms()
    by_state: dict[str, int] = {}
    for alarm in alarms:
        by_state[alarm.state.value] = by_state.get(alarm.state.value, 0) + 1
    if state:
        alarms = [a for a in alarms if a.state.value == state]
    payload = AlarmList(items=alarms, by_state=by_state)
    return Envelope[AlarmList](data=payload, meta=make_meta(request, ctr, cached))
