"""Run parameters, date tokens, substitution, dry run + manifest (#18/#20)."""

from __future__ import annotations

import json
from datetime import date

import pytest

from reportgen.errors import ConfigError
from reportgen.reports.builder import execute_report
from reportgen.reports.parameters import (
    resolve_date_token,
    resolve_parameters,
    substitute_params,
)


@pytest.mark.parametrize(
    ("token", "today", "expected"),
    [
        ("today", date(2026, 6, 15), "2026-06-15"),
        ("yesterday", date(2026, 6, 15), "2026-06-14"),
        ("month_start", date(2026, 6, 15), "2026-06-01"),
        ("month_end", date(2026, 6, 15), "2026-06-30"),
        ("previous_month_start", date(2026, 1, 10), "2025-12-01"),
        ("previous_month_end", date(2026, 3, 5), "2026-02-28"),
        ("year_start", date(2026, 6, 15), "2026-01-01"),
        ("quarter_start", date(2026, 5, 20), "2026-04-01"),
        ("days_ago:30", date(2026, 6, 15), "2026-05-16"),
        ("months_ago:2", date(2026, 6, 15), "2026-04-01"),
    ],
)
def test_date_tokens(token, today, expected):
    assert resolve_date_token(token, today) == expected


def test_non_token_strings_pass_through():
    resolved = resolve_parameters({"region": "EU", "from": "2026-01-01"}, today=date(2026, 6, 1))
    assert resolved == {"region": "EU", "from": "2026-01-01"}


def test_override_and_token_resolution():
    resolved = resolve_parameters(
        {"end": "month_start"}, overrides={"end": "month_end"}, today=date(2026, 6, 15)
    )
    assert resolved["end"] == "2026-06-30"


def test_substitution_preserves_types_and_errors():
    resolved = {"limit": 5, "from": "2026-01-01"}
    assert substitute_params("${params.limit}", resolved) == 5
    assert substitute_params("after ${params.from}", resolved) == "after 2026-01-01"
    with pytest.raises(ConfigError, match="undefined parameter"):
        substitute_params("${params.nope}", resolved)


@pytest.fixture
def dated_config(write_config, tmp_path):
    csv = tmp_path / "sales.csv"
    csv.write_text(
        "date,region,revenue\n2026-01-05,EU,10\n2026-01-20,EU,20\n2026-03-02,EU,30\n",
        encoding="utf-8",
    )
    return write_config(
        f"""
version: 1
report: {{title: Windowed, template: general}}
parameters: {{until: "2026-02-01"}}
sources: [{{name: d, type: csv, path: {csv}}}]
processing:
  - dataset: d
    output: windowed
    steps:
      - {{op: filter, column: date, operator: lt, value: "${{params.until}}"}}
metrics:
  - {{name: window_revenue, dataset: windowed, aggregation: sum, column: revenue}}
sections: [{{type: metrics, title: KPIs}}]
exports: {{output_dir: {tmp_path / "out"}, filename: "windowed-${{params.until}}", overwrite: true}}
"""
    )


def test_parameter_in_filter_and_filename(dated_config, tmp_path):
    result = execute_report(dated_config, formats=["html"])
    assert result.outputs[0].name == "windowed-2026-02-01.html"
    metric = {m.name: m.value for m in result.document.metrics}
    assert metric["window_revenue"] == 30.0  # 10 + 20 before Feb

    overridden = execute_report(dated_config, formats=["html"], parameters={"until": "2026-03-15"})
    metric = {m.name: m.value for m in overridden.document.metrics}
    assert metric["window_revenue"] == 60.0


def test_dry_run_writes_no_reports_but_builds(dated_config, tmp_path):
    result = execute_report(dated_config, formats=["html"], dry_run=True)
    assert result.outputs == []
    assert result.output_status[0]["status"] == "dry_run"
    assert not (tmp_path / "out" / "windowed-2026-02-01.html").exists()


def test_manifest_records_fingerprints_rows_and_status(dated_config, tmp_path):
    manifest_path = tmp_path / "run.json"
    execute_report(dated_config, formats=["html"], manifest_path=manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "success"
    assert manifest["config"]["fingerprint"].startswith("sha256:")
    assert manifest["inputs"][0]["fingerprint"].startswith("sha256:")
    assert manifest["datasets"]["d"] == 3
    assert manifest["datasets"]["windowed"] == 2
    assert manifest["outputs"][0]["status"] == "created"
    assert manifest["effective"]["parameters"]["until"] == "2026-02-01"


def test_timezone_in_generated_label(write_config, sales_csv, tmp_path):
    config = write_config(
        f"""
version: 1
report: {{title: TZ, template: general, timezone: Europe/Berlin}}
sources: [{{name: d, type: csv, path: {sales_csv}}}]
exports: {{output_dir: {tmp_path / "out"}, filename: tz, overwrite: true}}
"""
    )
    result = execute_report(config, formats=["html"])
    label = result.document.generated_label
    assert " UTC" not in label  # Berlin abbreviation (CET/CEST) instead
    assert result.document.timezone_name == "Europe/Berlin"
