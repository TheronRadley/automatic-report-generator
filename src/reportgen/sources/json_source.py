"""JSON and JSON Lines adapter with explicit nested-record extraction."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from reportgen.config.models import SourceConfig
from reportgen.errors import SourceError
from reportgen.sources.base import LoadedDataset, finalise_frame


def _extract_path(data: Any, record_path: str | None, file_label: str) -> Any:
    if not record_path:
        return data
    current = data
    for part in record_path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            raise SourceError(
                f"Record path '{record_path}' not found in {file_label} "
                f"(failed at '{part}'). Check options.record_path."
            )
    return current


def _to_frame(records: Any, file_label: str) -> pd.DataFrame:
    if isinstance(records, dict):
        nested_keys = [
            key
            for key, value in records.items()
            if isinstance(value, list) and value and isinstance(value[0], dict)
        ]
        if nested_keys:
            raise SourceError(
                f"{file_label} is a JSON object containing record arrays "
                f"({', '.join(nested_keys)}). Set options.record_path to one of "
                "these keys (e.g. record_path: orders) to extract the rows."
            )
        records = [records]
    if not isinstance(records, list):
        raise SourceError(
            f"{file_label} must contain a JSON array of objects (or set "
            "options.record_path to the array inside the document)."
        )
    if records and not all(isinstance(item, dict) for item in records):
        raise SourceError(f"{file_label} array items must all be JSON objects.")
    if not records:
        return pd.DataFrame()
    # json_normalize flattens nested objects to dotted column names.
    return pd.json_normalize(records)


def _read_jsonl(cfg: SourceConfig, path: Path, encoding: str) -> list[Any]:
    """Stream JSON Lines without materialising the whole file as one
    string; stop one record past ``max_rows`` so truncation is both
    bounded and reported by ``finalise_frame``."""
    records: list[Any] = []
    limit = cfg.max_rows + 1 if cfg.max_rows is not None else None
    try:
        with path.open(encoding=encoding) as handle:
            for lineno, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise SourceError(
                        f"Invalid JSON on line {lineno} of {path.name}: {exc.msg}"
                    ) from exc
                if limit is not None and len(records) >= limit:
                    break
    except UnicodeDecodeError as exc:
        raise SourceError(f"Cannot decode {path.name} with encoding '{encoding}'.") from exc
    except OSError as exc:
        raise SourceError(f"Cannot read JSON file {path}: {exc}") from exc
    return records


def load_json(cfg: SourceConfig, path: Path) -> LoadedDataset:
    options = cfg.options
    encoding = str(options.get("encoding", "utf-8"))
    is_lines = cfg.type == "jsonl" or path.suffix.lower() in {".jsonl", ".ndjson"}
    if is_lines:
        frame = _to_frame(_read_jsonl(cfg, path, encoding), path.name)
    else:
        try:
            text = path.read_text(encoding=encoding)
        except UnicodeDecodeError as exc:
            raise SourceError(f"Cannot decode {path.name} with encoding '{encoding}'.") from exc
        except OSError as exc:
            raise SourceError(f"Cannot read JSON file {path}: {exc}") from exc
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SourceError(f"Invalid JSON in {path.name}: {exc}") from exc
        nested = _extract_path(data, options.get("record_path"), path.name)
        frame = _to_frame(nested, path.name)

    mapping = options.get("field_mapping") or {}
    if mapping:
        if not isinstance(mapping, dict):
            raise SourceError("options.field_mapping must be a mapping of old -> new names.")
        unknown = [old for old in mapping if old not in frame.columns]
        if unknown:
            raise SourceError(
                f"field_mapping references columns not in {path.name}: "
                f"{', '.join(unknown)}. Available: {', '.join(frame.columns)}"
            )
        frame = frame.rename(columns={str(k): str(v) for k, v in mapping.items()})
    label = "JSON Lines" if is_lines else "JSON"
    return finalise_frame(frame, cfg, f"{label} file {path.name}")
