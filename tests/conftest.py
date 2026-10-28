"""Shared fixtures. Every test runs against temporary directories only."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "reportgen-home"
    monkeypatch.setenv("REPORTGEN_HOME", str(home))
    return home


@pytest.fixture()
def sales_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": ["2025-01-05", "2025-01-20", "2025-02-03", "2025-02-18", "2025-03-02"],
            "region": ["North", "South", "North", "East", "South"],
            "product": ["Lamp", "Chair", "Lamp", "Mouse", "Chair"],
            "quantity": [2, 1, 3, 4, 2],
            "unit_price": [50.0, 180.0, 50.0, 25.0, 180.0],
            "revenue": [100.0, 180.0, 150.0, 100.0, 360.0],
        }
    )


@pytest.fixture()
def sales_csv(tmp_path: Path, sales_frame: pd.DataFrame) -> Path:
    path = tmp_path / "sales.csv"
    sales_frame.to_csv(path, index=False)
    return path


@pytest.fixture()
def write_config(tmp_path: Path):
    """Return a helper that writes a YAML config and returns its path."""

    def _write(text: str, name: str = "report.yaml") -> Path:
        path = tmp_path / name
        path.write_text(text, encoding="utf-8")
        return path

    return _write


@pytest.fixture()
def minimal_sales_config(sales_csv: Path, write_config, tmp_path: Path) -> Path:
    return write_config(
        f"""
version: 1
report:
  title: Test Sales Report
  template: sales
sources:
  - name: sales
    type: csv
    path: {sales_csv}
exports:
  output_dir: {tmp_path / "out"}
  filename: test-report
  formats: [html, pdf, xlsx]
  overwrite: true
"""
    )
