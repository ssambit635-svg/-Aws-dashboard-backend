"""Shared response primitives: money, envelopes, pagination."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_serializer

T = TypeVar("T")

_CENTS = Decimal("0.01")


def utcnow() -> datetime:
    return datetime.now(UTC)


class Money(BaseModel):
    """A monetary amount.

    Money is ``Decimal`` internally and serialised as a fixed-precision string.
    Never a float: rounding errors in a billing dashboard are exactly the kind
    of bug that erodes trust in the product.
    """

    model_config = ConfigDict(frozen=True)

    amount: Decimal
    currency: str = "USD"

    @field_serializer("amount")
    def _serialise_amount(self, value: Decimal) -> str:
        return str(value.quantize(_CENTS, rounding=ROUND_HALF_UP))

    @classmethod
    def usd(cls, value: str | int | float | Decimal) -> Money:
        return cls(amount=Decimal(str(value)).quantize(_CENTS, rounding=ROUND_HALF_UP))

    def __add__(self, other: Money) -> Money:
        return Money(amount=self.amount + other.amount, currency=self.currency)


class Pagination(BaseModel):
    page: int = Field(ge=1)
    page_size: int = Field(ge=1)
    total: int = Field(ge=0)
    total_pages: int = Field(ge=0)
    has_next: bool

    @classmethod
    def build(cls, *, page: int, page_size: int, total: int) -> Pagination:
        total_pages = (total + page_size - 1) // page_size if page_size else 0
        return cls(
            page=page,
            page_size=page_size,
            total=total,
            total_pages=total_pages,
            has_next=page < total_pages,
        )


class Meta(BaseModel):
    """Envelope metadata attached to every successful response."""

    generated_at: datetime = Field(default_factory=utcnow)
    provider: str = "mock"
    cached: bool = False
    request_id: str | None = None
    pagination: Pagination | None = None


class Envelope(BaseModel, Generic[T]):
    data: T
    meta: Meta


class HealthStatus(BaseModel):
    status: str
    version: str
    provider: str
    region: str
    uptime_seconds: float
    checks: dict[str, str]


class ProviderInfo(BaseModel):
    """Transparency about where the numbers came from."""

    mode: str
    requested_mode: str
    live: bool
    region: str
    fallback_reason: str | None = None


class SystemInfo(BaseModel):
    settings: dict
    provider: ProviderInfo
    cache: dict
    endpoints: list[str]
