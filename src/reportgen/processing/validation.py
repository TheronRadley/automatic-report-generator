"""Dataset validation and single-value aggregation helpers."""

from __future__ import annotations

import math

import pandas as pd

from reportgen.errors import ProcessingError, ValidationError

AGGREGATION_ALIASES = {
    "count": "count",
    "sum": "sum",
    "mean": "mean",
    "avg": "mean",
    "average": "mean",
    "min": "min",
    "max": "max",
    "median": "median",
    "std": "std",
    "nunique": "nunique",
    "unique_count": "nunique",
}


def normalise_aggregation(name: str) -> str:
    canonical = AGGREGATION_ALIASES.get(str(name).lower())
    if canonical is None:
        raise ProcessingError(
            f"Unsupported aggregation '{name}'. Supported: "
            "count, sum, mean, min, max, median, std, nunique."
        )
    return canonical


def require_columns(frame: pd.DataFrame, columns: list[str], context: str) -> None:
    missing = [col for col in columns if col not in frame.columns]
    if missing:
        raise ProcessingError(
            f"{context}: missing column(s) {', '.join(missing)}. "
            f"Available columns: {', '.join(str(c) for c in frame.columns)}"
        )


def validate_frame(
    frame: pd.DataFrame,
    required_columns: list[str] | None = None,
    non_empty: bool = False,
    min_rows: int | None = None,
    context: str = "Dataset",
) -> None:
    if required_columns:
        missing = [col for col in required_columns if col not in frame.columns]
        if missing:
            raise ValidationError(
                f"{context} is missing required columns: {', '.join(missing)}. "
                f"Available: {', '.join(str(c) for c in frame.columns)}"
            )
    if non_empty and frame.empty:
        raise ValidationError(f"{context} is empty. Refusing to build a report from no data.")
    if min_rows is not None and len(frame) < min_rows:
        raise ValidationError(
            f"{context} has {len(frame)} rows, below the required minimum of {min_rows}."
        )


def aggregate_value(frame: pd.DataFrame, aggregation: str, column: str | None) -> float:
    """Compute one aggregate over a frame. Missing data is never invented:
    an all-missing sum is 0, but mean/median of nothing is NaN (rendered '—').
    """
    agg = normalise_aggregation(aggregation)
    if agg == "count" and column is None:
        return float(len(frame))
    if column is None:
        raise ProcessingError(f"Aggregation '{aggregation}' requires a column.")
    require_columns(frame, [column], f"Aggregation '{aggregation}'")
    series = frame[column]
    if agg == "count":
        return float(series.count())
    if agg == "nunique":
        return float(series.nunique(dropna=True))
    numeric = pd.to_numeric(series, errors="coerce")
    if numeric.notna().sum() == 0 and agg != "sum":
        return math.nan
    result = getattr(numeric, agg)()
    try:
        return float(result)
    except (TypeError, ValueError):
        return math.nan
