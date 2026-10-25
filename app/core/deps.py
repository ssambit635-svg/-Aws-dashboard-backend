"""Shared FastAPI dependencies."""

from __future__ import annotations

from fastapi import Depends, Request

from app.core.config import Settings, get_settings
from app.models.common import Meta, Pagination
from app.services.cloud import CloudService
from app.services.container import Container, get_container


def container() -> Container:
    return get_container()


def settings_dep() -> Settings:
    return get_settings()


def cloud_service(ctr: Container = Depends(container)) -> CloudService:
    """A service bound to the active provider but sharing the container cache."""
    return CloudService(ctr.provider, ctr.settings, ctr.cache)


def make_meta(
    request: Request,
    ctr: Container = Depends(container),
    cached: bool = False,
    pagination: Pagination | None = None,
) -> Meta:
    return Meta(
        provider=ctr.info.mode,
        cached=cached,
        request_id=getattr(request.state, "request_id", None),
        pagination=pagination,
    )
