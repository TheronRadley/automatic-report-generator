"""Shared dataset container, validation, and inspection helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from reportgen.config.models import SourceConfig
from reportgen.errors import SourceError, ValidationError


@dataclass
class LoadedDataset:
    name: str
    frame: pd.DataFrame
    description: str
    warnings: list[str] = field(default_factory=list)


def check_duplicate_columns(columns: list[str], source_label: str) -> None:
    seen: dict[str, int] = {}
    for col in columns:
        key = str(col).strip()
        seen[key] = seen.get(key, 0) + 1
    dupes = sorted(name for name, count in seen.items() if count > 1)
    if dupes:
        raise SourceError(
            f"{source_label} has duplicate column names: {', '.join(dupes)}. "
            "Rename them in the source file so every column is unique."
        )
    if any(not str(col).strip() for col in columns):
        raise SourceError(f"{source_label} has an empty column name in its header.")


def finalise_frame(frame: pd.DataFrame, cfg: SourceConfig, description: str) -> LoadedDataset:
    """Apply column selection, expected-column and row-limit checks."""
    warnings: list[str] = []
    frame = frame.copy()
    frame.columns = [str(col).strip() for col in frame.columns]
    check_duplicate_columns(list(frame.columns), f"Source '{cfg.name}'")

    missing = [col for col in cfg.expected_columns if col not in frame.columns]
    if missing:
        raise ValidationError(
            f"Source '{cfg.name}' is missing expected columns: {', '.join(missing)}. "
            f"Available columns: {', '.join(frame.columns)}"
        )
    if cfg.columns:
        absent = [col for col in cfg.columns if col not in frame.columns]
        if absent:
            raise ValidationError(
                f"Source '{cfg.name}' does not contain requested columns: "
                f"{', '.join(absent)}. Available: {', '.join(frame.columns)}"
            )
        frame = frame[list(cfg.columns)]
    if cfg.max_rows is not None and len(frame) > cfg.max_rows:
        warnings.append(
            f"Source '{cfg.name}' truncated to {cfg.max_rows} rows (was {len(frame)}) by max_rows."
        )
        frame = frame.head(cfg.max_rows)
    return LoadedDataset(name=cfg.name, frame=frame, description=description, warnings=warnings)


def inspect_dataframe(frame: pd.DataFrame, name: str = "dataset") -> dict[str, Any]:
    """Return a JSON-serialisable summary used by `sources inspect`."""
    columns: list[dict[str, Any]] = []
    for col in frame.columns:
        series = frame[col]
        try:
            unique = int(series.nunique(dropna=True))
        except TypeError:  # unhashable values (e.g. nested lists from JSON)
            unique = -1
        entry: dict[str, Any] = {
            "name": str(col),
            "dtype": str(series.dtype),
            "nulls": int(series.isna().sum()),
            "unique": unique,
        }
        if pd.api.types.is_numeric_dtype(series) and series.notna().any():
            entry["min"] = float(series.min())
            entry["max"] = float(series.max())
            entry["mean"] = float(series.mean())
        else:
            sample = series.dropna().astype(str).head(3).tolist()
            entry["sample"] = sample
        columns.append(entry)
    return {
        "name": name,
        "rows": int(len(frame)),
        "columns": columns,
        "empty": bool(frame.empty),
    }
