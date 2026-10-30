"""CLI surface: every documented command works with sane exit codes."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from reportgen.cli.app import app

runner = CliRunner()


def _json_line(output: str) -> dict:
    """Extract the JSON object a --json command printed (logs go to stderr,
    which the test runner merges into output; pretty-printed JSON spans
    several lines, so parse from the last top-level '{' that parses)."""
    decoder = json.JSONDecoder()
    starts = [
        index
        for index, char in enumerate(output)
        if char == "{" and (index == 0 or output[index - 1] == "\n")
    ]
    for index in reversed(starts):
        try:
            parsed, _ = decoder.raw_decode(output[index:])
        except json.JSONDecodeError:
            continue
        return parsed
    raise AssertionError(f"No JSON object in output:\n{output}")


def test_help_and_version():
    assert runner.invoke(app, ["--help"]).exit_code == 0
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0 and "0.2.0" in result.output
    result = runner.invoke(app, ["version", "--json"])
    assert json.loads(result.output)["version"] == "0.2.0"


def test_templates_commands():
    result = runner.invoke(app, ["templates", "list"])
    assert result.exit_code == 0 and "sales" in result.output
    result = runner.invoke(app, ["templates", "list", "--json"])
    assert len(json.loads(result.output)["templates"]) == 5
    result = runner.invoke(app, ["templates", "show", "sales"])
    assert result.exit_code == 0 and "revenue" in result.output
    result = runner.invoke(app, ["templates", "show", "nope"])
    assert result.exit_code != 0


def test_init_and_validate(tmp_path):
    target = tmp_path / "starter.yaml"
    result = runner.invoke(app, ["init", "--template", "sales", "--output", str(target)])
    assert result.exit_code == 0 and target.exists()
    # Second init without --force must refuse.
    assert runner.invoke(app, ["init", "--output", str(target)]).exit_code != 0
    # The starter points at example data that does not exist here:
    # validate must fail cleanly (no stack trace), config-only must pass.
    assert runner.invoke(app, ["validate", str(target)]).exit_code != 0
    ok = runner.invoke(app, ["validate", str(target), "--no-check-sources"])
    assert ok.exit_code == 0
    assert "Traceback" not in ok.output


def test_generate_and_inspect(minimal_sales_config, sales_csv):
    result = runner.invoke(app, ["validate", str(minimal_sales_config)])
    assert result.exit_code == 0 and "Valid" in result.output
    result = runner.invoke(
        app, ["generate", str(minimal_sales_config), "--format", "html", "--json"]
    )
    assert result.exit_code == 0
    payload = json.loads(result.output.strip().splitlines()[-1])
    assert payload["outputs"][0].endswith(".html")

    result = runner.invoke(app, ["sources", "inspect", str(sales_csv)])
    assert result.exit_code == 0 and "revenue" in result.output
    result = runner.invoke(app, ["sources", "inspect", str(sales_csv), "--json"])
    assert json.loads(result.output)["rows"] == 5
    assert runner.invoke(app, ["sources", "inspect", "/nope.csv"]).exit_code != 0
    assert runner.invoke(app, ["sources", "types"]).exit_code == 0


@pytest.mark.parametrize("fmt", ["pdf", "html", "xlsx"])
def test_generate_each_format(minimal_sales_config, fmt):
    result = runner.invoke(app, ["generate", str(minimal_sales_config), "--format", fmt, "--json"])
    assert result.exit_code == 0
    assert json.loads(result.output.strip().splitlines()[-1])["outputs"][0].endswith(f".{fmt}")


def test_jobs_and_history_flow(minimal_sales_config):
    assert (
        runner.invoke(
            app,
            ["jobs", "add", "demo", str(minimal_sales_config), "--format", "html", "--overwrite"],
        ).exit_code
        == 0
    )
    listing = runner.invoke(app, ["jobs", "list"])
    assert listing.exit_code == 0 and "demo" in listing.output
    assert runner.invoke(app, ["jobs", "schedule", "demo"]).exit_code == 0
    assert runner.invoke(app, ["jobs", "run", "demo"]).exit_code == 0
    history = runner.invoke(app, ["history", "list", "--json"])
    assert json.loads(history.output)["history"][0]["status"] == "success"
    assert runner.invoke(app, ["jobs", "remove", "demo"]).exit_code == 0
    assert runner.invoke(app, ["jobs", "run", "demo"]).exit_code != 0


def test_user_errors_have_no_traceback():
    result = runner.invoke(app, ["generate", "/definitely/missing.yaml"])
    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "Error" in result.output


def test_quiet_and_verbose_flags(minimal_sales_config):
    result = runner.invoke(
        app, ["--quiet", "generate", str(minimal_sales_config), "--format", "html"]
    )
    assert result.exit_code == 0


# ---------------------------------------------------- fixes #13/#17/#18/#20


def test_validate_catches_broken_pipeline(write_config, sales_csv, tmp_path):
    config = write_config(
        f"""
version: 1
report: {{title: Broken, template: general}}
sources: [{{name: d, type: csv, path: {sales_csv}}}]
processing:
  - dataset: d
    steps:
      - {{op: filter, column: does_not_exist, operator: eq, value: 1}}
"""
    )
    result = runner.invoke(app, ["validate", str(config)])
    assert result.exit_code != 0
    assert "does_not_exist" in result.output


def test_validate_catches_bad_chart(write_config, sales_csv, tmp_path):
    config = write_config(
        f"""
version: 1
report: {{title: BadChart, template: general}}
sources: [{{name: d, type: csv, path: {sales_csv}}}]
sections:
  - type: chart
    title: Broken
    dataset: d
    chart: {{type: line, x: date, y: nonexistent}}
"""
    )
    result = runner.invoke(app, ["validate", str(config), "--json"])
    assert result.exit_code != 0
    assert "nonexistent" in result.output


def test_generate_dry_run_and_manifest(write_config, sales_csv, tmp_path):
    out = tmp_path / "out"
    config = write_config(
        f"""
version: 1
report: {{title: Dry, template: general}}
sources: [{{name: d, type: csv, path: {sales_csv}}}]
exports: {{output_dir: {out}, filename: dryrun, overwrite: true}}
"""
    )
    manifest = tmp_path / "manifest.json"
    result = runner.invoke(
        app,
        ["generate", str(config), "--dry-run", "--manifest", str(manifest), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = _json_line(result.output)
    assert payload["dry_run"] is True
    assert payload["outputs"] == []
    saved = json.loads(manifest.read_text(encoding="utf-8"))
    assert saved["status"] == "dry_run"
    assert not (out / "dryrun.html").exists()


def test_generate_json_reports_partial_outputs(write_config, sales_csv, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "partial.pdf").write_text("existing")
    config = write_config(
        f"""
version: 1
report: {{title: Partial, template: general}}
sources: [{{name: d, type: csv, path: {sales_csv}}}]
exports: {{output_dir: {out}, filename: partial}}
"""
    )
    result = runner.invoke(
        app,
        ["generate", str(config), "--format", "html", "--format", "pdf", "--json"],
    )
    assert result.exit_code != 0
    payload = _json_line(result.output)
    assert payload["ok"] is False
    assert [p.split("/")[-1] for p in payload["outputs"]] == ["partial.html"]


def test_cli_param_requires_key_value(minimal_sales_config):
    result = runner.invoke(app, ["generate", str(minimal_sales_config), "--param", "nope"])
    assert result.exit_code != 0
    assert "KEY=VALUE" in result.output
