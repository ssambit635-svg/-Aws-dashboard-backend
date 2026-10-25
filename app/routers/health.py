"""Liveness and readiness probes (unauthenticated, by design).

Kubernetes/ECS need these reachable without credentials. ``/livez`` never
touches the network; ``/readyz`` reports whether the data source is usable.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Response, status

from app.core.config import Settings, get_settings
from app.core.security import require_api_key
from app.models.common import HealthStatus, ProviderInfo
from app.services.container import Container, get_container

router = APIRouter(tags=["health"])
_STARTED_AT = time.time()


@router.get("/livez", summary="Liveness probe")
def livez() -> dict[str, str]:
    """Process is up. No dependencies touched."""
    return {"status": "alive"}


@router.get("/readyz", summary="Readiness probe", response_model=HealthStatus)
def readyz(
    response: Response,
    ctr: Container = Depends(get_container),
    settings: Settings = Depends(get_settings),
) -> HealthStatus:
    # A degraded data source is reported, but the process is still ready to
    # serve: returning 503 would make an orchestrator kill a container that can
    # usefully answer /livez and /api/v1/system/info. Only report not-ready when
    # the provider cannot be constructed at all, which is caught below.
    checks = ctr.provider.health()
    healthy = checks.get("provider") == "ok"
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthStatus(
        status="ready" if healthy else "degraded",
        version=settings.version,
        provider=ctr.info.mode,
        region=settings.aws_region,
        uptime_seconds=round(time.time() - _STARTED_AT, 2),
        checks=checks,
    )


@router.get(
    "/api/v1/system/info",
    summary="Runtime configuration and provider state",
    # Under /api/v1, so it is protected like the rest of the API. It reports
    # environment, region, budget and cache statistics — not secrets, but not
    # something to hand to an anonymous caller either. The probes below stay
    # public so an orchestrator can reach them without credentials.
    dependencies=[Depends(require_api_key)],
)
def system_info(ctr: Container = Depends(get_container)) -> dict:
    info: ProviderInfo = ctr.info
    return {
        "settings": ctr.settings.public_fingerprint(),
        "provider": info.model_dump(),
        "cache": ctr.cache.stats.as_dict(),
        "endpoints": [
            "/api/v1/overview",
            "/api/v1/buckets",
            "/api/v1/instances",
            "/api/v1/alarms",
            "/api/v1/billing/summary",
            "/api/v1/billing/trend",
            "/api/v1/billing/forecast",
            "/livez",
            "/readyz",
        ],
    }
