"""All five templates work from minimal configs with their documented schema."""

from __future__ import annotations

import pandas as pd
import pytest

from reportgen.errors import ConfigError
from reportgen.reports.builder import run_report
from reportgen.reports.templates import TEMPLATES, get_template

DATASETS = {
    "sales": pd.DataFrame(
        {
            "date": ["2025-01-05", "2025-02-05", "2025-03-05"],
            "region": ["N", "S", "N"],
            "product": ["A", "B", "A"],
            "quantity": [1, 2, 3],
            "unit_price": [10.0, 20.0, 10.0],
            "revenue": [10.0, 40.0, 30.0],
        }
    ),
    "business": pd.DataFrame(
        {
            "date": ["2025-01-01", "2025-02-01"],
            "department": ["Retail", "Online"],
            "revenue": [1000.0, 2000.0],
            "expenses": [600.0, 1500.0],
        }
    ),
    "financial": pd.DataFrame(
        {
            "date": ["2025-01-03", "2025-02-10", "2025-03-01"],
            "category": ["Income", "Payroll", "Income"],
            "account": ["Op", "Op", "Op"],
            "amount": [500.0, -200.0, 700.0],
        }
    ),
    "operations": pd.DataFrame(
        {
            "date": ["2025-01-01", "2025-01-02"],
            "site": ["A", "B"],
            "output_units": [100, 120],
            "defects": [2, 1],
            "downtime_hours": [0.5, 1.0],
        }
    ),
    "general": pd.DataFrame({"city": ["X", "Y"], "score": [1.5, 2.5]}),
}


def test_five_templates_registered():
    assert set(TEMPLATES) == {"business", "financial", "sales", "operations", "general"}
    with pytest.raises(ConfigError):
        get_template("nonexistent")


@pytest.mark.parametrize("template", sorted(DATASETS))
def test_template_defaults_generate_report(tmp_path, write_config, template):
    csv_path = tmp_path / f"{template}.csv"
    DATASETS[template].to_csv(csv_path, index=False)
    config = write_config(
        f"""
version: 1
report:
  title: {template} report
  template: {template}
sources:
  - {{name: data, type: csv, path: "{csv_path}"}}
exports:
  output_dir: "{tmp_path / "out"}"
  filename: {template}
  overwrite: true
""",
        name=f"{template}.yaml",
    )
    outputs = run_report(config, formats=["html"])
    html = outputs[0].read_text(encoding="utf-8")
    assert f"{template} report" in html
    assert "Executive summary" in html


def test_template_missing_columns_gives_clear_error(tmp_path, write_config):
    csv_path = tmp_path / "wrong.csv"
    pd.DataFrame({"foo": [1]}).to_csv(csv_path, index=False)
    config = write_config(f"""
version: 1
report: {{title: X, template: sales}}
sources:
  - {{name: data, type: csv, path: "{csv_path}"}}
exports:
  output_dir: "{tmp_path / "out"}"
  filename: x
""")
    with pytest.raises(Exception, match="required columns|missing"):
        run_report(config, formats=["html"])


def test_general_template_text_only_dataset(write_config, tmp_path):
    """#14: the general template must not fail on text-only data."""
    csv = tmp_path / "text.csv"
    csv.write_text("name,city\nAna,Berlin\nBen,Paris\n", encoding="utf-8")
    config = write_config(
        f"""
version: 1
report: {{title: Text Only, template: general}}
sources: [{{name: people, type: csv, path: {csv}}}]
exports: {{output_dir: {tmp_path / "out"}, filename: textonly, overwrite: true}}
"""
    )
    outputs = run_report(config, formats=["html"])
    assert "Text Only" in outputs[0].read_text(encoding="utf-8")
