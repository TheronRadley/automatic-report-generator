"""Excel (.xlsx) adapter built on OpenPyXL via pandas.

The workbook is pre-flighted with OpenPyXL before pandas touches it:
zip expansion and worksheet dimensions are bounded, and the *original*
header row (after any skipped rows) is checked for duplicates —
otherwise pandas would silently rename duplicates to ``a``/``a.1``.
Declared columns (``columns``/``expected_columns``) are preserved even
when every value in them is missing.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook

from reportgen.config.models import SourceConfig
from reportgen.errors import SourceError
from reportgen.sources.base import LoadedDataset, check_duplicate_columns, finalise_frame

_DEFAULT_MAX_EXPANDED_MB = 500.0
_DEFAULT_MAX_CELLS = 10_000_000


def list_sheets(path: Path) -> list[str]:
    try:
        workbook = load_workbook(path, read_only=True, data_only=True)
        try:
            return list(workbook.sheetnames)
        finally:
            workbook.close()
    except Exception as exc:  # openpyxl raises several types for bad files
        raise SourceError(f"Cannot open Excel workbook {path.name}: {exc}") from exc


def _check_expansion(path: Path, max_expanded_mb: float) -> None:
    """Bound zip expansion: a small .xlsx must not expand unboundedly."""
    try:
        with zipfile.ZipFile(path) as archive:
            total = sum(info.file_size for info in archive.infolist())
    except zipfile.BadZipFile as exc:
        raise SourceError(
            f"Cannot open Excel workbook {path.name}: not a valid xlsx (zip) file"
        ) from exc
    except OSError as exc:
        raise SourceError(f"Cannot open Excel workbook {path.name}: {exc}") from exc
    if total > max_expanded_mb * 1024 * 1024:
        raise SourceError(
            f"Workbook {path.name} expands to {total / 1e6:.0f} MB uncompressed, above the "
            f"{max_expanded_mb:.0f} MB limit (options.max_expanded_mb)."
        )


def _resolve_sheet(workbook, sheet, path: Path) -> tuple[object, str]:
    sheets = list(workbook.sheetnames)
    if isinstance(sheet, str):
        if sheet not in sheets:
            raise SourceError(
                f"Worksheet '{sheet}' not found in {path.name}. "
                f"Available worksheets: {', '.join(sheets) or '(none)'}"
            )
        return workbook[sheet], sheet
    index = int(sheet)
    if not (0 <= index < len(sheets)):
        raise SourceError(
            f"Worksheet index {index} is out of range for {path.name} "
            f"({len(sheets)} worksheet(s): {', '.join(sheets)})."
        )
    return workbook[sheets[index]], sheets[index]


def _check_original_header(worksheet, physical_row: int, path: Path, sheet_label: str) -> None:
    """Validate the header row as written in the file, before pandas
    gets a chance to rename duplicates."""
    header_values: list = []
    for row in worksheet.iter_rows(min_row=physical_row, max_row=physical_row, values_only=True):
        header_values = list(row)
        break
    while header_values and header_values[-1] is None:
        header_values.pop()
    if not header_values:
        raise SourceError(
            f"Worksheet '{sheet_label}' in {path.name} has an empty header "
            f"row (row {physical_row})."
        )
    names: list[str] = []
    for index, value in enumerate(header_values):
        if value is None or str(value).strip() == "":
            # A blank header over a column that only ever holds blanks is
            # a spacer column -- tolerate it (it is dropped after load).
            # Over real data it is an error, like the CSV adapter.
            column_values = [
                cell[0]
                for cell in worksheet.iter_rows(
                    min_row=physical_row + 1,
                    min_col=index + 1,
                    max_col=index + 1,
                    values_only=True,
                )
            ]
            if any(v is not None and str(v).strip() != "" for v in column_values):
                raise SourceError(
                    f"Worksheet '{sheet_label}' in {path.name} has an empty "
                    f"header for data column {index + 1} (row {physical_row})."
                )
            names.append(f"__blank_{index}")
        else:
            names.append(str(value))
    check_duplicate_columns(names, f"Worksheet '{sheet_label}' in {path.name}")


def load_excel(cfg: SourceConfig, path: Path) -> LoadedDataset:
    options = cfg.options
    sheet = options.get("sheet", 0)
    header_row = int(options.get("header_row", 0) or 0)
    skip_rows = int(options.get("skip_rows", 0) or 0)
    physical_row = skip_rows + header_row + 1  # openpyxl rows are 1-based
    _check_expansion(path, float(options.get("max_expanded_mb", _DEFAULT_MAX_EXPANDED_MB)))

    try:
        workbook = load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:
        raise SourceError(f"Cannot open Excel workbook {path.name}: {exc}") from exc
    try:
        worksheet, sheet_label = _resolve_sheet(workbook, sheet, path)
        max_cells = int(options.get("max_cells", _DEFAULT_MAX_CELLS))
        cells = int(worksheet.max_row or 0) * int(worksheet.max_column or 0)
        if cells > max_cells:
            raise SourceError(
                f"Worksheet '{sheet_label}' in {path.name} declares {cells:,} cells, "
                f"above the {max_cells:,} limit (options.max_cells)."
            )
        _check_original_header(worksheet, physical_row, path, sheet_label)
    finally:
        workbook.close()

    read_kwargs: dict = {"sheet_name": sheet_label, "engine": "openpyxl"}
    if options.get("header_row") is not None:
        read_kwargs["header"] = header_row
    if skip_rows:
        read_kwargs["skiprows"] = skip_rows
    if options.get("parse_dates"):
        read_kwargs["parse_dates"] = list(options["parse_dates"])
    if cfg.max_rows is not None:
        read_kwargs["nrows"] = cfg.max_rows + 1  # +1 so truncation is reported
    try:
        frame = pd.read_excel(path, **read_kwargs)
    except Exception as exc:
        raise SourceError(f"Cannot read worksheet from {path.name}: {exc}") from exc
    frame = frame.dropna(axis=0, how="all")
    # Drop fully-empty columns Excel leaves behind, but never a column the
    # user declared -- an all-missing declared column is real information.
    declared = set(cfg.expected_columns) | set(cfg.columns or [])
    droppable = [col for col in frame.columns if col not in declared and frame[col].isna().all()]
    if droppable:
        frame = frame.drop(columns=droppable)
    return finalise_frame(frame, cfg, f"Excel file {path.name} (sheet '{sheet_label}')")
