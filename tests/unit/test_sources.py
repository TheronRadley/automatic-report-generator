"""Source adapters: CSV, Excel, JSON, JSONL, SQLite."""

from __future__ import annotations

import json
import sqlite3

import pandas as pd
import pytest

from reportgen.config.models import SourceConfig
from reportgen.errors import SourceError, ValidationError
from reportgen.sources.factory import load_dataset, load_from_path


def _load(cfg: SourceConfig, tmp_path):
    return load_dataset(cfg, tmp_path)


def test_csv_semicolon_delimiter_detected(tmp_path):
    path = tmp_path / "semi.csv"
    path.write_text("a;b;c\n1;2;3\n4;5;6\n", encoding="utf-8")
    loaded = _load(SourceConfig(name="d", type="csv", path=str(path)), tmp_path)
    assert list(loaded.frame.columns) == ["a", "b", "c"]
    assert len(loaded.frame) == 2


def test_csv_latin1_encoding(tmp_path):
    path = tmp_path / "latin.csv"
    path.write_bytes("ville,montant\nMontréal,10\n".encode("latin-1"))
    cfg = SourceConfig(name="d", type="csv", path=str(path), options={"encoding": "latin-1"})
    loaded = _load(cfg, tmp_path)
    assert loaded.frame.loc[0, "ville"] == "Montréal"


def test_csv_duplicate_header_rejected(tmp_path):
    path = tmp_path / "dup.csv"
    path.write_text("a,b,a\n1,2,3\n", encoding="utf-8")
    with pytest.raises(SourceError, match="duplicate"):
        _load(SourceConfig(name="d", type="csv", path=str(path)), tmp_path)


def test_csv_missing_file(tmp_path):
    with pytest.raises(SourceError, match="not found"):
        _load(SourceConfig(name="d", type="csv", path="missing.csv"), tmp_path)


def test_csv_empty_file_rejected(tmp_path):
    path = tmp_path / "empty.csv"
    path.write_text("", encoding="utf-8")
    with pytest.raises(SourceError):
        _load(SourceConfig(name="d", type="csv", path=str(path)), tmp_path)


def test_csv_chunked_with_max_rows(tmp_path, sales_frame):
    big = pd.concat([sales_frame] * 20, ignore_index=True)
    path = tmp_path / "big.csv"
    big.to_csv(path, index=False)
    cfg = SourceConfig(name="d", type="csv", path=str(path), max_rows=10, options={"chunk_rows": 4})
    loaded = _load(cfg, tmp_path)
    assert len(loaded.frame) == 10
    assert loaded.warnings


def test_expected_columns_validation(tmp_path, sales_csv):
    cfg = SourceConfig(
        name="d", type="csv", path=str(sales_csv), expected_columns=["region", "missing_col"]
    )
    with pytest.raises(ValidationError, match="missing_col"):
        _load(cfg, tmp_path)


def test_excel_sheets(tmp_path, sales_frame):
    path = tmp_path / "book.xlsx"
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        sales_frame.to_excel(writer, sheet_name="Sales", index=False)
        sales_frame.head(2).to_excel(writer, sheet_name="Sample", index=False)
    loaded = _load(
        SourceConfig(name="d", type="excel", path=str(path), options={"sheet": "Sample"}),
        tmp_path,
    )
    assert len(loaded.frame) == 2
    with pytest.raises(SourceError, match="not found"):
        _load(
            SourceConfig(name="d", type="excel", path=str(path), options={"sheet": "Nope"}),
            tmp_path,
        )


def test_excel_corrupt_file(tmp_path):
    path = tmp_path / "bad.xlsx"
    path.write_bytes(b"this is not a workbook")
    with pytest.raises(SourceError):
        _load(SourceConfig(name="d", type="excel", path=str(path)), tmp_path)


def test_json_array_and_mapping(tmp_path):
    path = tmp_path / "data.json"
    path.write_text(json.dumps([{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]), encoding="utf-8")
    cfg = SourceConfig(
        name="d", type="json", path=str(path), options={"field_mapping": {"a": "amount"}}
    )
    loaded = _load(cfg, tmp_path)
    assert list(loaded.frame.columns) == ["amount", "b"]


def test_json_nested_record_path(tmp_path):
    path = tmp_path / "nested.json"
    path.write_text(json.dumps({"payload": {"rows": [{"v": 5}, {"v": 6}]}}), encoding="utf-8")
    cfg = SourceConfig(
        name="d", type="json", path=str(path), options={"record_path": "payload.rows"}
    )
    loaded = _load(cfg, tmp_path)
    assert loaded.frame["v"].tolist() == [5, 6]


def test_json_nested_without_path_gives_hint(tmp_path):
    path = tmp_path / "nested.json"
    path.write_text(json.dumps({"rows": [{"v": 5}]}), encoding="utf-8")
    with pytest.raises(SourceError, match="record_path"):
        _load(SourceConfig(name="d", type="json", path=str(path)), tmp_path)


def test_json_bad_record_path(tmp_path):
    path = tmp_path / "data.json"
    path.write_text(json.dumps({"a": 1}), encoding="utf-8")
    cfg = SourceConfig(
        name="d", type="json", path=str(path), options={"record_path": "missing.deep"}
    )
    with pytest.raises(SourceError, match="not found"):
        _load(cfg, tmp_path)


def test_jsonl_and_invalid_line(tmp_path):
    path = tmp_path / "rows.jsonl"
    path.write_text('{"a": 1}\n{"a": 2}\n', encoding="utf-8")
    loaded = _load(SourceConfig(name="d", type="jsonl", path=str(path)), tmp_path)
    assert len(loaded.frame) == 2
    path.write_text('{"a": 1}\nnot json\n', encoding="utf-8")
    with pytest.raises(SourceError, match="line 2"):
        _load(SourceConfig(name="d", type="jsonl", path=str(path)), tmp_path)


@pytest.fixture()
def sqlite_db(tmp_path, sales_frame):
    path = tmp_path / "data.sqlite"
    with sqlite3.connect(path) as conn:
        sales_frame.to_sql("sales", conn, index=False)
    return path


def test_sqlite_table_and_query(tmp_path, sqlite_db):
    by_table = _load(
        SourceConfig(name="d", type="sqlite", path=str(sqlite_db), table="sales"), tmp_path
    )
    assert len(by_table.frame) == 5
    by_query = _load(
        SourceConfig(
            name="d",
            type="sqlite",
            path=str(sqlite_db),
            query="SELECT region, SUM(revenue) AS total FROM sales "
            "WHERE region = :region GROUP BY region",
            params={"region": "North"},
        ),
        tmp_path,
    )
    assert float(by_query.frame.loc[0, "total"]) == 250.0


def test_sqlite_write_query_rejected(tmp_path, sqlite_db):
    with pytest.raises(Exception, match="read-only|Only"):
        _load(
            SourceConfig(name="d", type="sqlite", path=str(sqlite_db), query="DELETE FROM sales"),
            tmp_path,
        )


def test_sqlite_injection_param_is_literal(tmp_path, sqlite_db):
    """Bind parameters must be treated as values, never as SQL."""
    loaded = _load(
        SourceConfig(
            name="d",
            type="sqlite",
            path=str(sqlite_db),
            query="SELECT * FROM sales WHERE region = :region",
            params={"region": "North' OR '1'='1"},
        ),
        tmp_path,
    )
    assert len(loaded.frame) == 0


def test_load_from_path_infers_types(sales_csv, sqlite_db):
    assert len(load_from_path(sales_csv).frame) == 5
    assert len(load_from_path(sqlite_db).frame) == 5
    with pytest.raises(SourceError):
        load_from_path(sqlite_db.with_suffix(".bin"))
