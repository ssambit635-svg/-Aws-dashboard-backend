"""Model-level tests: money precision, validation and serialisation."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.models.common import Money, Pagination
from app.models.resources import Instance


# ── money ─────────────────────────────────────────────────────────────────
def test_money_serialises_as_two_decimal_string():
    dumped = Money.usd("12.005").model_dump()
    assert dumped["amount"] == "12.01"  # half-up, not banker's rounding
    assert dumped["currency"] == "USD"


def test_money_never_uses_float():
    money = Money.usd(0.1) + Money.usd(0.2)
    assert str(money.amount) == "0.30"
    assert isinstance(money.amount, Decimal)


def test_money_addition_preserves_currency():
    total = Money.usd("10.00") + Money.usd("0.01")
    assert total.model_dump()["amount"] == "10.01"


def test_money_is_immutable():
    money = Money.usd("1.00")
    with pytest.raises(ValidationError):
        money.amount = Decimal("2.00")


def test_large_amounts_keep_precision():
    assert Money.usd("123456789.999").model_dump()["amount"] == "123456790.00"


# ── pagination ────────────────────────────────────────────────────────────
def test_pagination_build_exact_multiple():
    meta = Pagination.build(page=1, page_size=5, total=10)
    assert meta.total_pages == 2
    assert meta.has_next is True
    last = Pagination.build(page=2, page_size=5, total=10)
    assert last.has_next is False


def test_pagination_empty_set():
    meta = Pagination.build(page=1, page_size=10, total=0)
    assert meta.total_pages == 0
    assert meta.has_next is False


# ── resource validation ───────────────────────────────────────────────────
def base_instance(**overrides):
    payload = {
        "id": "i-0123456789abcdef0",
        "type": "t3.micro",
        "state": "running",
        "availability_zone": "us-east-1a",
        "private_ip": "10.0.0.1",
        "launch_time": "2026-01-01T00:00:00Z",
        "cpu_utilization_pct": 10.0,
        "memory_used_pct": 20.0,
        "monthly_cost": Money.usd("7.59"),
    }
    payload.update(overrides)
    return payload


def test_valid_instance_parses():
    assert Instance(**base_instance()).id == "i-0123456789abcdef0"


@pytest.mark.parametrize(
    "bad_id",
    ["i-123", "i-XYZ", "not-an-instance", "0123456789abcdef0"],
)
def test_instance_id_pattern_is_enforced(bad_id):
    with pytest.raises(ValidationError):
        Instance(**base_instance(id=bad_id))


def test_instance_state_is_constrained():
    with pytest.raises(ValidationError):
        Instance(**base_instance(state="exploded"))


@pytest.mark.parametrize("cpu", [-1, 101])
def test_cpu_bounds(cpu):
    with pytest.raises(ValidationError):
        Instance(**base_instance(cpu_utilization_pct=cpu))


def test_instance_display_name_falls_back_to_id():
    instance = Instance(**base_instance())
    assert instance.name == "i-0123456789abcdef0"

    named = Instance(**base_instance(tags={"Name": "web-01"}))
    assert named.name == "web-01"
