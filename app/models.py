"""Shared value types and integer money helpers.

Prices are stored as integer ten-thousandths of a dollar (price4), cash as integer cents.
Buy amounts round up and sell amounts round down, so rounding never favors the player.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal


@dataclass(frozen=True)
class Quote:
    symbol: str
    price4: int
    as_of: datetime
    source: str
    tradable: bool = True
    reason: str | None = None


def to_price4(x) -> int:
    return int((Decimal(str(x)) * 10000).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def buy_cents(qty: int, price4: int) -> int:
    return -(-qty * price4 // 100)


def sell_cents(qty: int, price4: int) -> int:
    return qty * price4 // 100


def slip_up(price4: int, bps: int) -> int:
    return -(-price4 * (10000 + bps) // 10000)


def slip_down(price4: int, bps: int) -> int:
    return price4 * (10000 - bps) // 10000


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
