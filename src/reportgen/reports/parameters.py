"""Reusable run parameters with date-range tokens.

Report definitions may declare a ``parameters:`` mapping. String
values matching a date token (``today``, ``month_start``,
``previous_month_end``, ``days_ago:30`` ...) are resolved **at run
time** in the report's timezone, which is what makes scheduled jobs
useful: the same definition covers "last month" every month without
edits. Anywhere a scalar is accepted, ``${params.<name>}`` is
substituted (source SQL params, filter values, processing params,
export filename). ``--param name=value`` (CLI/jobs) overrides values.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from reportgen.errors import ConfigError

_PARAM_RE = re.compile(r"\$\{params\.([A-Za-z0-9_]+)\}")
_TOKENS_WITH_ARG = re.compile(r"^(days_ago|days_from_now|months_ago):(\d+)$")


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _month_end(d: date) -> date:
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def _shift_months(d: date, months: int) -> date:
    total = d.year * 12 + (d.month - 1) + months
    year, month = divmod(total, 12)
    return date(year, month + 1, 1)


def resolve_date_token(value: str, today: date) -> str | None:
    """Return the ISO date for a token, or None if ``value`` isn't one."""
    token = value.strip().lower()
    fixed = {
        "today": today,
        "yesterday": today - timedelta(days=1),
        "tomorrow": today + timedelta(days=1),
        "month_start": _month_start(today),
        "month_end": _month_end(today),
        "previous_month_start": _shift_months(today, -1),
        "previous_month_end": _month_end(_shift_months(today, -1)),
        "next_month_start": _shift_months(today, 1),
        "year_start": date(today.year, 1, 1),
        "year_end": date(today.year, 12, 31),
        "quarter_start": date(today.year, 3 * ((today.month - 1) // 3) + 1, 1),
    }
    if token in fixed:
        return fixed[token].isoformat()
    match = _TOKENS_WITH_ARG.match(token)
    if match:
        kind, amount = match.group(1), int(match.group(2))
        if kind == "days_ago":
            return (today - timedelta(days=amount)).isoformat()
        if kind == "days_from_now":
            return (today + timedelta(days=amount)).isoformat()
        return _shift_months(today, -amount).isoformat()  # months_ago -> its 1st
    return None


def resolve_parameters(
    raw: dict[str, Any] | None,
    timezone_name: str = "UTC",
    overrides: dict[str, str] | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Merge declared parameters with overrides and resolve date tokens."""
    if today is None:
        today = datetime.now(ZoneInfo(timezone_name)).date()
    merged: dict[str, Any] = dict(raw or {})
    for key, value in (overrides or {}).items():
        merged[key] = value
    resolved: dict[str, Any] = {}
    for key, value in merged.items():
        if isinstance(value, str):
            resolved[key] = resolve_date_token(value, today) or value
        else:
            resolved[key] = value
    return resolved


def substitute_params(value: Any, resolved: dict[str, Any]) -> Any:
    """Recursively substitute ``${params.name}`` inside strings/dicts/
    lists. A string that is *exactly* one placeholder keeps the
    parameter's native scalar type (so numeric SQL/filter params stay
    numeric); embedded placeholders are stringified."""
    if isinstance(value, str):
        exact = _PARAM_RE.fullmatch(value.strip())
        if exact:
            return _lookup(exact.group(1), resolved)
        return _PARAM_RE.sub(lambda m: str(_lookup(m.group(1), resolved)), value)
    if isinstance(value, dict):
        return {k: substitute_params(v, resolved) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute_params(item, resolved) for item in value]
    return value


def _lookup(name: str, resolved: dict[str, Any]) -> Any:
    if name not in resolved:
        raise ConfigError(
            f"Reference to undefined parameter '${{params.{name}}}'. "
            f"Defined parameters: {', '.join(sorted(resolved)) or '(none)'}. "
            "Declare it under 'parameters:' or pass --param name=value."
        )
    return resolved[name]
