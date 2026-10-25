"""Provider selection.

Resolution order for ``DASH_PROVIDER_MODE``:

* ``mock`` — always the deterministic fixture provider.
* ``aws``  — always boto3; raises loudly if credentials are missing.
* ``auto`` — boto3 when credentials resolve, otherwise the mock provider.

``auto`` is the default so the repository is runnable (and demoable, and CI
testable) by anyone cloning it, while still exercising the real AWS code path
the moment credentials exist. The reason for any fallback is surfaced in the
API and shown in the UI — silent fallbacks are how dashboards end up lying.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.core.config import Settings
from app.core.errors import ProviderUnavailableError
from app.models.common import ProviderInfo
from app.providers.aws import AWSProvider
from app.providers.base import CloudProvider
from app.providers.mock import MockProvider

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProviderBundle:
    provider: CloudProvider
    info: ProviderInfo


def _has_credentials(settings: Settings) -> tuple[bool, str | None]:
    """Check for usable AWS credentials without making a network call."""
    try:
        import boto3

        session = boto3.Session(profile_name=settings.aws_profile)
        creds = session.get_credentials()
        if creds is None:
            return False, "no AWS credentials found in environment/profile"
        return True, None
    except Exception as exc:
        return False, f"credential resolution failed: {type(exc).__name__}"


def build_provider(settings: Settings) -> ProviderBundle:
    requested = settings.provider_mode

    if requested == "mock":
        provider: CloudProvider = MockProvider(region=settings.aws_region)
        return ProviderBundle(
            provider=provider,
            info=ProviderInfo(
                mode="mock",
                requested_mode=requested,
                live=False,
                region=settings.aws_region,
                fallback_reason="DASH_PROVIDER_MODE=mock was set explicitly",
            ),
        )

    have_creds, reason = _has_credentials(settings)

    if requested == "aws" and not have_creds:
        raise ProviderUnavailableError(
            "DASH_PROVIDER_MODE=aws but no AWS credentials are available",
            detail=reason,
        )

    if have_creds:
        try:
            provider = AWSProvider(
                region=settings.aws_region,
                cost_explorer_region=settings.cost_explorer_region,
                profile=settings.aws_profile,
                timeout=settings.aws_timeout_seconds,
            )
            info = ProviderInfo(
                mode="aws",
                requested_mode=requested,
                live=True,
                region=settings.aws_region,
            )
            return ProviderBundle(provider=provider, info=info)
        except ProviderUnavailableError:
            if requested == "aws":
                raise
            log.warning("AWS provider unavailable, falling back to mock")

    provider = MockProvider(region=settings.aws_region)
    return ProviderBundle(
        provider=provider,
        info=ProviderInfo(
            mode="mock",
            requested_mode=requested,
            live=False,
            region=settings.aws_region,
            fallback_reason=reason or "AWS provider could not be initialised",
        ),
    )
