"""CSV / TSV adapter with delimiter detection and chunked reading."""

from __future__ import annotations

import csv
from pathlib import Path

import pandas as pd

from reportgen.config.models import SourceConfig
from reportgen.errors import SourceError
from reportgen.sources.base import LoadedDataset, check_duplicate_columns, finalise_frame

_SAMPLE_BYTES = 8192


def _detect_delimiter(path: Path, encoding: str) -> str:
    try:
        sample = path.read_bytes()[:_SAMPLE_BYTES].decode(encoding, errors="replace")
    except OSError as exc:
        raise SourceError(f"Cannot read CSV file {path}: {exc}") from exc
    if not sample.strip():
        raise SourceError(f"CSV file {path.name} is empty (no header row found).")
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        return dialect.delimiter
    except csv.Error:
        first_line = sample.splitlines()[0] if sample.splitlines() else ""
        return "\t" if "\t" in first_line else ","


def _check_header(path: Path, encoding: str, delimiter: str, header_row: int) -> None:
    try:
        with path.open(encoding=encoding, newline="") as handle:
            reader = csv.reader(handle, delimiter=delimiter)
            for index, row in enumerate(reader):
                if index == header_row:
                    check_duplicate_columns([str(c) for c in row], f"CSV file {path.name}")
                    return
    except UnicodeDecodeError as exc:
        raise SourceError(
            f"Cannot decode {path.name} with encoding '{encoding}'. "
            "Set options.encoding (e.g. 'latin-1') in the source config."
        ) from exc
    except OSError as exc:
        raise SourceError(f"Cannot read CSV file {path}: {exc}") from exc
    raise SourceError(f"CSV file {path.name} is empty (no header row found).")


def load_csv(cfg: SourceConfig, path: Path) -> LoadedDataset:
    options = cfg.options
    encoding = str(options.get("encoding", "utf-8-sig"))
    header_row = int(options.get("header_row", 0))
    delimiter = options.get("delimiter")
    if delimiter is None:
        delimiter = "\t" if cfg.type == "tsv" or path.suffix.lower() == ".tsv" else None
    if delimiter is None:
        delimiter = _detect_delimiter(path, encoding)
    _check_header(path, encoding, str(delimiter), header_row)

    read_kwargs: dict = {
        "sep": str(delimiter),
        "encoding": encoding,
        "header": header_row,
        "skipinitialspace": False,
    }
    if options.get("skip_rows"):
        read_kwargs["skiprows"] = int(options["skip_rows"])
    if options.get("na_values"):
        read_kwargs["na_values"] = options["na_values"]
    if options.get("parse_dates"):
        read_kwargs["parse_dates"] = list(options["parse_dates"])

    chunk_rows = options.get("chunk_rows")
    try:
        if chunk_rows:
            frames: list[pd.DataFrame] = []
            total = 0
            limit = cfg.max_rows
            for chunk in pd.read_csv(path, chunksize=int(chunk_rows), **read_kwargs):
                frames.append(chunk)
                total += len(chunk)
                if limit is not None and total >= limit:
                    break
            frame = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        else:
            frame = pd.read_csv(path, **read_kwargs)
    except pd.errors.ParserError as exc:
        raise SourceError(f"Malformed CSV in {path.name}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise SourceError(
            f"Cannot decode {path.name} with encoding '{encoding}'. "
            "Set options.encoding to the correct encoding."
        ) from exc
    except OSError as exc:
        raise SourceError(f"Cannot read CSV file {path}: {exc}") from exc
    if frame.columns.empty:
        raise SourceError(f"CSV file {path.name} has no columns.")
    return finalise_frame(frame, cfg, f"CSV file {path.name}")
