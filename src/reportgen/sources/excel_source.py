"""Excel (.xlsx) adapter built on OpenPyXL via pandas."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from reportgen.config.models import SourceConfig
from reportgen.errors import SourceError
from reportgen.sources.base import LoadedDataset, finalise_frame


def list_sheets(path: Path) -> list[str]:
    try:
        with pd.ExcelFile(path, engine="openpyxl") as book:
            return list(book.sheet_names)
    except Exception as exc:  # openpyxl raises several types for bad files
        raise SourceError(f"Cannot open Excel workbook {path.name}: {exc}") from exc


def load_excel(cfg: SourceConfig, path: Path) -> LoadedDataset:
    options = cfg.options
    sheet = options.get("sheet", 0)
    sheets = list_sheets(path)
    if isinstance(sheet, str) and sheet not in sheets:
        raise SourceError(
            f"Worksheet '{sheet}' not found in {path.name}. "
            f"Available worksheets: {', '.join(sheets) or '(none)'}"
        )
    if isinstance(sheet, int) and not (0 <= sheet < len(sheets)):
        raise SourceError(
            f"Worksheet index {sheet} is out of range for {path.name} "
            f"({len(sheets)} worksheet(s): {', '.join(sheets)})."
        )
    read_kwargs: dict = {"sheet_name": sheet, "engine": "openpyxl"}
    if options.get("header_row") is not None:
        read_kwargs["header"] = int(options["header_row"])
    if options.get("skip_rows"):
        read_kwargs["skiprows"] = int(options["skip_rows"])
    if options.get("parse_dates"):
        read_kwargs["parse_dates"] = list(options["parse_dates"])
    try:
        frame = pd.read_excel(path, **read_kwargs)
    except Exception as exc:
        raise SourceError(f"Cannot read worksheet from {path.name}: {exc}") from exc
    # Drop fully-empty trailing rows/columns that Excel often leaves behind.
    frame = frame.dropna(axis=0, how="all").dropna(axis=1, how="all")
    sheet_label = sheets[sheet] if isinstance(sheet, int) else str(sheet)
    return finalise_frame(frame, cfg, f"Excel file {path.name} (sheet '{sheet_label}')")
