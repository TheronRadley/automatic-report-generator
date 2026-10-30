"""Number / date formatting shared by every exporter so values agree."""

from __future__ import annotations

import math
from datetime import date, datetime

import pandas as pd


def format_number(
    value: object, fmt: str = "number", decimals: int = 2, currency: str = "$"
) -> str:
    """Format a value as number | currency | percent | integer.

    ``percent`` expects a fraction (0.125 -> '12.50%'). Missing values
    render as an em dash rather than an invented zero.
    """
    if value is None:
        return "—"
    try:
        if pd.isna(value):  # type: ignore[arg-type]
            return "—"
    except (TypeError, ValueError):
        pass
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, str):
        return value
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return str(value)
    if math.isnan(number) or math.isinf(number):
        return "—"
    fmt = (fmt or "number").lower()
    if fmt == "integer":
        return f"{number:,.0f}"
    if fmt == "currency":
        sign = "-" if number < 0 else ""
        return f"{sign}{currency}{abs(number):,.{decimals}f}"
    if fmt == "percent":
        return f"{number * 100:,.{decimals}f}%"
    # Plain number: drop trailing zeros for whole values.
    if float(number).is_integer() and abs(number) < 1e15:
        return f"{number:,.0f}"
    return f"{number:,.{decimals}f}"


def format_cell(value: object, decimals: int = 2) -> str:
    """Format an arbitrary table cell (dates, numbers, text)."""
    if value is None:
        return ""
    try:
        if pd.isna(value):  # type: ignore[arg-type]
            return ""
    except (TypeError, ValueError):
        pass
    if isinstance(value, float):
        return format_number(value, "number", decimals)
    return format_number(value, "number", decimals)
