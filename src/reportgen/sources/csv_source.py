"""CSV / TSV adapter with delimiter detection and chunked reading."""

from __future__ import annotations

import csv
from pathlib import Path

import pandas as pd

from reportgen.config.models import SourceConfig
from reportgen.errors import SourceError
from reportgen.sources.base import LoadedDataset, check_duplicate_columns, finalise_frame

_SAMPLE_BYTES = 8192


def _read_sample(path: Path, encoding: str, from_row: int = 0) -> str:
    """Stream just the sample window (never the whole file) and start it
    at the physical header row so skipped banner rows cannot confuse
    delimiter detection."""
    try:
        with path.open("rb") as handle:
            raw = handle.read(_SAMPLE_BYTES * 4)
    except OSError as exc:
        raise SourceError(f"Cannot read CSV file {path}: {exc}") from exc
    text = raw.decode(encoding, errors="replace")
    if from_row:
        text = "\n".join(text.splitlines()[from_row:])
    return text[:_SAMPLE_BYTES]


def _detect_delimiter(path: Path, encoding: str, from_row: int = 0) -> str:
    sample = _read_sample(path, encoding, from_row=from_row)
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
    skip_rows = int(options.get("skip_rows", 0) or 0)
    # Physical line of the header in the file: pandas counts `header`
    # after `skiprows`, so duplicate detection must look at the sum --
    # otherwise it validates the wrong row and pandas silently mangles
    # duplicate names to `a`, `a.1`.
    physical_header = skip_rows + header_row
    delimiter = options.get("delimiter")
    if delimiter is None:
        delimiter = "\t" if cfg.type == "tsv" or path.suffix.lower() == ".tsv" else None
    if delimiter is None:
        delimiter = _detect_delimiter(path, encoding, from_row=physical_header)
    _check_header(path, encoding, str(delimiter), physical_header)

    read_kwargs: dict = {
        "sep": str(delimiter),
        "encoding": encoding,
        "header": header_row,
        "skipinitialspace": False,
    }
    if skip_rows:
        read_kwargs["skiprows"] = skip_rows
    if options.get("na_values"):
        read_kwargs["na_values"] = options["na_values"]
    if options.get("parse_dates"):
        read_kwargs["parse_dates"] = list(options["parse_dates"])

    chunk_rows = options.get("chunk_rows")
    try:
        if chunk_rows:
            frames: list[pd.DataFrame] = []
            total = 0
            # Read at most max_rows+1 rows so finalise can both truncate
            # and report that truncation happened.
            limit = cfg.max_rows + 1 if cfg.max_rows is not None else None
            for chunk in pd.read_csv(path, chunksize=int(chunk_rows), **read_kwargs):
                frames.append(chunk)
                total += len(chunk)
                if limit is not None and total >= limit:
                    break
            frame = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        else:
            if cfg.max_rows is not None:
                # Apply the row limit during loading, not after the whole
                # file has been materialised.
                read_kwargs["nrows"] = cfg.max_rows + 1
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
