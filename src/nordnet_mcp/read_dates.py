"""Strict calendar dates for read-only native requests."""
from datetime import date


def read_date(value):
    if not isinstance(value, str) or len(value) != 10:
        raise ValueError('Dates must be YYYY-MM-DD')
    result = date.fromisoformat(value)
    if result.isoformat() != value:
        raise ValueError('Dates must be YYYY-MM-DD')
    return result

