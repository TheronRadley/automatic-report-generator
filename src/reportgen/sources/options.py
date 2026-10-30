"""Typed schemas for per-source ``options``.

Options used to be an unvalidated dict: ``delimeter: ";"`` was
silently ignored and ``timeout_seconds: "soon"`` failed deep inside a
driver. Each adapter now declares its options with kinds and bounds,
validated when the report definition is loaded.
"""

from __future__ import annotations

from typing import Any

OPTION_SPECS: dict[str, dict[str, str]] = {
    "csv": {
        "delimiter": "str",
        "encoding": "str",
        "header_row": "nonneg_int",
        "skip_rows": "nonneg_int",
        "na_values": "list",
        "parse_dates": "str_list",
        "chunk_rows": "pos_int",
    },
    "tsv": {
        "delimiter": "str",
        "encoding": "str",
        "header_row": "nonneg_int",
        "skip_rows": "nonneg_int",
        "na_values": "list",
        "parse_dates": "str_list",
        "chunk_rows": "pos_int",
    },
    "excel": {
        "sheet": "str_or_int",
        "header_row": "nonneg_int",
        "skip_rows": "nonneg_int",
        "parse_dates": "str_list",
        "max_expanded_mb": "pos_number",
        "max_cells": "pos_int",
    },
    "json": {"encoding": "str", "record_path": "str", "field_mapping": "dict"},
    "jsonl": {"encoding": "str", "field_mapping": "dict"},
    "sqlite": {"timeout_seconds": "pos_int", "chunk_rows": "pos_int"},
    "postgres": {"timeout_seconds": "pos_int", "chunk_rows": "pos_int"},
    "database": {"timeout_seconds": "pos_int", "chunk_rows": "pos_int"},
}


def _check(source_type: str, name: str, value: Any, kind: str) -> None:
    ok = True
    if kind == "str":
        ok = isinstance(value, str)
    elif kind == "str_or_int":
        ok = isinstance(value, (str, int)) and not isinstance(value, bool)
    elif kind == "str_list":
        ok = isinstance(value, list) and all(isinstance(v, str) for v in value)
    elif kind == "list":
        ok = isinstance(value, list)
    elif kind == "dict":
        ok = isinstance(value, dict)
    elif kind == "pos_int":
        ok = isinstance(value, int) and not isinstance(value, bool) and value > 0
    elif kind == "nonneg_int":
        ok = isinstance(value, int) and not isinstance(value, bool) and value >= 0
    elif kind == "pos_number":
        ok = isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0
    if not ok:
        raise ValueError(
            f"Option '{name}' for {source_type} sources must be {kind}, "
            f"got {type(value).__name__} ({value!r})."
        )


def validate_source_options(source_type: str, source_name: str, options: dict[str, Any]) -> None:
    spec = OPTION_SPECS.get(source_type, {})
    unknown = sorted(set(options) - set(spec))
    if unknown:
        raise ValueError(
            f"Source '{source_name}' ({source_type}) has unknown option(s): "
            f"{', '.join(unknown)}. Allowed: {', '.join(sorted(spec)) or '(none)'}."
        )
    for name, value in options.items():
        _check(source_type, name, value, spec[name])
