"""Route a :class:`SourceConfig` to the correct adapter."""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

from reportgen.config.models import SourceConfig
from reportgen.errors import SourceError
from reportgen.sources.base import LoadedDataset
from reportgen.sources.csv_source import load_csv
from reportgen.sources.database_source import load_database
from reportgen.sources.excel_source import load_excel
from reportgen.sources.json_source import load_json
from reportgen.utils.security import check_input_size, resolve_input_path

_FILE_TYPES = {"csv", "tsv", "excel", "json", "jsonl"}
_SUFFIX_TO_TYPE = {
    ".csv": "csv",
    ".tsv": "tsv",
    ".txt": "csv",
    ".xlsx": "excel",
    ".xlsm": "excel",
    ".json": "json",
    ".jsonl": "jsonl",
    ".ndjson": "jsonl",
    ".sqlite": "sqlite",
    ".sqlite3": "sqlite",
    ".db": "sqlite",
}


def load_dataset(
    cfg: SourceConfig, base_dir: Path, max_input_bytes: int | None = None
) -> LoadedDataset:
    if cfg.type in _FILE_TYPES:
        path = resolve_input_path(cfg.path or "", base_dir)
        if max_input_bytes is not None:
            check_input_size(path, max_input_bytes)
        if cfg.type in {"csv", "tsv"}:
            return load_csv(cfg, path)
        if cfg.type == "excel":
            return load_excel(cfg, path)
        return load_json(cfg, path)
    if cfg.type in {"sqlite", "postgres", "database"}:
        if cfg.path and max_input_bytes is not None and cfg.type == "sqlite":
            check_input_size(resolve_input_path(cfg.path, base_dir), max_input_bytes)
        return load_database(cfg, base_dir)
    raise SourceError(f"Unsupported source type: {cfg.type}")


def infer_type(path: Path) -> str:
    source_type = _SUFFIX_TO_TYPE.get(path.suffix.lower())
    if source_type is None:
        raise SourceError(
            f"Cannot infer a source type from '{path.suffix}'. "
            f"Supported: {', '.join(sorted(_SUFFIX_TO_TYPE))}"
        )
    return source_type


def _dataset_name_for(path: Path) -> str:
    """A valid source identifier derived from any filename — including
    ones with spaces or punctuation ('my sales data.csv')."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", path.stem or "").strip("_-")
    return cleaned or "data"


def load_from_path(path: Path, table: str | None = None) -> LoadedDataset:
    """Load a single file by extension — backs `sources inspect`."""
    if not path.exists():
        raise SourceError(f"File not found: {path}")
    path = path.resolve()  # adapters resolve cfg.path against the base dir
    source_type = infer_type(path)
    cfg_kwargs: dict = {"name": _dataset_name_for(path), "type": source_type}
    if source_type == "sqlite":
        cfg_kwargs["path"] = str(path)
        cfg_kwargs["table"] = table or _first_sqlite_table(path)
    else:
        cfg_kwargs["path"] = str(path)
    cfg = SourceConfig(**cfg_kwargs)
    return load_dataset(cfg, path.resolve().parent)


def _first_sqlite_table(path: Path) -> str:
    import sqlite3

    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name LIMIT 1"
            ).fetchone()
    except sqlite3.Error as exc:
        raise SourceError(f"Cannot open SQLite database {path.name}: {exc}") from exc
    if row is None:
        raise SourceError(f"SQLite database {path.name} contains no tables.")
    return str(row[0])


def empty_frame() -> pd.DataFrame:  # small helper for tests/callers
    return pd.DataFrame()
