"""Strict provider identities and decimal wire values (no floating point orders)."""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re


@dataclass(frozen=True)
class ListingIdentity:
    market_id: int
    identifier: str

    def __post_init__(self):
        if type(self.market_id) is not int or self.market_id <= 0:
            raise ValueError("market_id must be a positive integer")
        if not isinstance(self.identifier, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", self.identifier):
            raise ValueError("identifier must identify one exact listing")

    @property
    def key(self) -> str:
        return f"{self.market_id}:{self.identifier}"


def positive_id(value: int) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError("provider ID must be a positive integer")
    return value


def decimal_wire(value: str, *, whole: bool = False) -> str:
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("amount must be a decimal string")
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("invalid decimal amount") from exc
    if not amount.is_finite() or amount <= 0 or amount.adjusted() > 18 or amount.as_tuple().exponent < -12:
        raise ValueError("amount must be finite, positive and within provider bounds")
    if whole and amount != amount.to_integral_value():
        raise ValueError("cash-share volume must be a whole number")
    return format(amount, "f")


def currency_code(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z]{3}", value):
        raise ValueError("currency must be an explicit three-letter code")
    return value
