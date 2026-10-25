"""The single aggregated call the dashboard loads first."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.core.deps import cloud_service, container, make_meta
from app.core.security import require_api_key
from app.models.common import Envelope
from app.models.overview import Overview
from app.services.cloud import CloudService
from app.services.container import Container

router = APIRouter(prefix="/api/v1", tags=["overview"], dependencies=[Depends(require_api_key)])


@router.get("/overview", response_model=Envelope[Overview], summary="Dashboard KPI overview")
def overview(
    request: Request,
    svc: CloudService = Depends(cloud_service),
    ctr: Container = Depends(container),
) -> Envelope[Overview]:
    payload = svc.overview()
    # ``overview`` fans out into several cached calls; report cache state of the
    # spend series, which is the most expensive of them.
    _, cached = svc.trend(14)
    return Envelope[Overview](data=payload, meta=make_meta(request, ctr, cached))
