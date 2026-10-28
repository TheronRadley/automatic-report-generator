"""End-to-end: real reports from the shipped synthetic example, verified."""

from __future__ import annotations

import os
from pathlib import Path

import openpyxl
import pandas as pd
import pytest

from reportgen.reports.builder import run_report
from reportgen.utils.formatting import format_number

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG = REPO_ROOT / "examples" / "configs" / "sales-report.yaml"
EXAMPLE_DATA = REPO_ROOT / "examples" / "data" / "synthetic_sales.csv"


def test_sales_demo_all_formats_with_correct_figures(tmp_path):
    outputs = run_report(
        EXAMPLE_CONFIG,
        formats=["pdf", "html", "xlsx"],
        output_dir=tmp_path / "demo",
        overwrite=True,
    )
    assert {p.suffix for p in outputs} == {".pdf", ".html", ".xlsx"}
    for path in outputs:
        assert path.stat().st_size > 3000

    # Figures in the report must match an independent pandas computation.
    data = pd.read_csv(EXAMPLE_DATA)
    total_revenue = format_number(float(data["revenue"].sum()), "currency", 2)
    total_units = format_number(float(data["quantity"].sum()), "integer", 2)
    html = next(p for p in outputs if p.suffix == ".html").read_text(encoding="utf-8")
    assert total_revenue in html
    assert total_units in html
    assert str(len(data)) in html  # order count appears in summary/metrics
    assert "synthetic" in html.lower()  # example data honestly labelled

    pdf = next(p for p in outputs if p.suffix == ".pdf").read_bytes()
    assert pdf.startswith(b"%PDF")
    assert pdf.count(b"/Type /Page") >= 1  # multi-element document

    book = openpyxl.load_workbook(next(p for p in outputs if p.suffix == ".xlsx"))
    assert "Summary" in book.sheetnames
    assert "Regional detail" in book.sheetnames
    region_sheet = book["Regional detail"]
    totals = [region_sheet.cell(row=r, column=2).value for r in range(2, 7)]
    assert sum(v for v in totals if isinstance(v, (int, float))) == pytest.approx(
        float(data["revenue"].sum()), rel=1e-9
    )

    # A repeatable rerun reproduces the same figures.
    rerun = run_report(
        EXAMPLE_CONFIG, formats=["html"], output_dir=tmp_path / "demo2", overwrite=True
    )
    assert total_revenue in rerun[0].read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "config_name",
    [
        "business-performance.yaml",
        "financial-summary.yaml",
        "operations-report.yaml",
        "data-analysis.yaml",
        "sales-from-sqlite.yaml",
        "sales-from-excel.yaml",
        "sales-from-json.yaml",
    ],
)
def test_every_example_config_runs(tmp_path, config_name):
    config = REPO_ROOT / "examples" / "configs" / config_name
    outputs = run_report(
        config, formats=["pdf", "html", "xlsx"], output_dir=tmp_path / "ex", overwrite=True
    )
    assert len(outputs) == 3
    assert all(p.stat().st_size > 1000 for p in outputs)


def test_multi_source_merge_report(tmp_path, write_config, sales_frame):
    sales_path = tmp_path / "s.csv"
    sales_frame.to_csv(sales_path, index=False)
    targets_path = tmp_path / "t.json"
    targets_path.write_text(
        '[{"region": "North", "target": 300}, {"region": "South", "target": 600},'
        ' {"region": "East", "target": 200}]',
        encoding="utf-8",
    )
    config = write_config(f"""
version: 1
report:
  title: Regional targets
  template: general
sources:
  - {{name: sales, type: csv, path: "{sales_path}"}}
  - {{name: targets, type: json, path: "{targets_path}"}}
processing:
  - dataset: sales
    output: by_region
    steps:
      - {{op: group_aggregate, group_by: [region], aggregations: {{revenue: sum}}}}
      - {{op: merge, other: targets, "on": [region], how: left}}
      - {{op: calculate, column: attainment, expression: "revenue / target"}}
metrics:
  - {{name: revenue, label: Revenue, dataset: sales, aggregation: sum,
     column: revenue, format: currency}}
sections:
  - {{title: Attainment, type: table, dataset: by_region}}
exports:
  output_dir: "{tmp_path / "out"}"
  filename: merged
  overwrite: true
""")
    outputs = run_report(config, formats=["xlsx"])
    book = openpyxl.load_workbook(outputs[0])
    sheet = book["Attainment"]
    rows = {
        sheet.cell(row=r, column=1).value: sheet.cell(row=r, column=4).value for r in range(2, 5)
    }
    assert rows["North"] == pytest.approx(250 / 300)
    assert rows["East"] == pytest.approx(100 / 200)


@pytest.mark.postgres
@pytest.mark.skipif(
    not os.environ.get("REPORTGEN_TEST_PG_URL"),
    reason="set REPORTGEN_TEST_PG_URL to run the live PostgreSQL test",
)
def test_postgres_source_live():
    """Live PostgreSQL round-trip (opt-in; not part of the default run)."""
    from reportgen.config.models import SourceConfig
    from reportgen.sources.factory import load_dataset

    cfg = SourceConfig(
        name="pg",
        type="postgres",
        url_env="REPORTGEN_TEST_PG_URL",
        query="SELECT 1 AS one, 'ok' AS label",
    )
    loaded = load_dataset(cfg, REPO_ROOT)
    assert loaded.frame.loc[0, "label"] == "ok"
