# Automatic Report Generator

Turn CSV, Excel, JSON, and database data into professional PDF, HTML, and Excel reports — locally, reproducibly, from one YAML report definition.

`reportgen` is a command-line application (Python 3.11+) that loads structured data, validates and transforms it, computes metrics, renders charts, and exports the same report to PDF, HTML, and Excel. No account, no internet connection, and no external services are required.

The same sales report is also wired to SQLite, Excel, and JSON sources (`examples/configs/sales-from-*.yaml`) so every adapter can be tried end-to-end.

## Features

- **Sources:** CSV/TSV (delimiter auto-detection, configurable encoding, chunked reads), Excel `.xlsx` (multi-sheet), JSON & JSON Lines (nested `record_path` extraction, field mapping), SQLite and PostgreSQL (read-only, parameterised queries, credentials from environment variables).
- **Processing:** column select/rename, structured filtering, sorting, deduplication, missing-value treatment, type conversion, date normalisation, safe calculated columns (arithmetic only — no `eval`), grouping with count/sum/mean/min/max/median/std/unique-count, running totals, percentage change, multi-dataset merge/append, validation steps, summary statistics.
- **Reports:** five built-in templates (business performance, financial summary, sales analysis, operational metrics, general analysis), executive summaries with computed values, key metrics, tables, 9 chart types (line, bar, horizontal bar, area, scatter, histogram, pie, donut, time-series), methodology/source attribution, themes.
- **Exports:** print-ready multi-page PDF (headers/footers, page numbers, repeating table headers, DejaVu Sans Unicode), self-contained responsive HTML (embedded charts, no CDN, print styles), styled Excel workbooks (summary + per-section sheets, freeze panes, filters, native number/date types, embedded chart images). Multi-format export in one command, atomic no-clobber writes.
- **Automation:** saved jobs with per-run logs, run history, file-lock overlap prevention, subprocess timeouts, optional retention, and ready-to-paste cron / Windows Task Scheduler / launchd configuration.
- **Safety:** safe YAML loading, sandboxed autoescaped HTML templates, read-only SQL validation, path-traversal and spreadsheet-formula-injection protections, overwrite refusal, input size limits, credential masking in logs.

## Installation

Requires Python 3.11 or newer (3.12+ recommended).

```bash
git clone https://github.com/TheronRadley/automatic-report-generator.git
cd automatic-report-generator
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e .
```

For PostgreSQL support, install the driver extra:

```bash
pip install -e ".[postgres]"
```

Verify the install:

```bash
reportgen --help
reportgen version
```

## Quick start — the demo report

The repository ships a synthetic sales dataset (818 invented orders; clearly marked synthetic) and a complete report definition. After installation, one command generates PDF, HTML, and Excel versions:

```bash
reportgen generate examples/configs/sales-report.yaml --all
```

Output lands in `examples/reports/` as `sales-report-YYYY-MM-DD.pdf/.html/.xlsx`. Re-running on the same day refuses to overwrite; add `--overwrite` to replace. A pre-generated sample is committed as `examples/reports/sales-report-sample.*` so you can inspect the output before installing anything.

Inspect the input data first if you like:

```bash
reportgen sources inspect examples/data/synthetic_sales.csv
reportgen validate examples/configs/sales-report.yaml
```

## Basic usage

```bash
reportgen init --template sales --output my-report.yaml   # starter definition
reportgen validate my-report.yaml                          # schema + source checks
reportgen generate my-report.yaml                          # formats from the config
reportgen generate my-report.yaml --format pdf             # one format
reportgen generate my-report.yaml --all --json             # machine-readable result
reportgen templates list
reportgen templates show financial
reportgen jobs add monthly-sales my-report.yaml --overwrite
reportgen jobs run monthly-sales
reportgen jobs schedule monthly-sales
reportgen history list
```

A minimal report definition — the chosen template supplies processing, metrics, charts, and sections:

```yaml
version: 1
report:
  title: Sales Analysis Report
  template: sales
sources:
  - name: sales
    type: csv
    path: data/sales.csv       # relative to this file
    expected_columns: [date, region, product, quantity, unit_price, revenue]
exports:
  output_dir: reports
  filename: sales-{date}
  formats: [pdf, html, xlsx]
```

Any default can be overridden by adding explicit `processing:`, `metrics:`, or `sections:` blocks — see the [configuration guide](docs/configuration.md).

## Documentation

| Guide | Contents |
|---|---|
| [docs/configuration.md](docs/configuration.md) | Full report-definition schema, every processing step, filters, aggregations, charts |
| [docs/sources.md](docs/sources.md) | Per-source options, database credentials, PostgreSQL setup |
| [docs/templates.md](docs/templates.md) | The five templates, themes, writing custom templates |
| [docs/automation.md](docs/automation.md) | Jobs, cron, Task Scheduler, launchd, retention, logs |
| [docs/security.md](docs/security.md) | Threat model, protections, known limits |
| [docs/development.md](docs/development.md) | Architecture, dev setup, testing, releasing |
| [docs/benchmarks.md](docs/benchmarks.md) | Measured performance (regenerable) |

## Testing

```bash
pip install -e ".[dev,postgres]"
pytest
ruff check src tests examples benchmarks
ruff format --check src tests examples benchmarks
```

The suite (130+ tests) covers adapters, transformations, aggregations, charts, all three exporters, CLI commands, jobs, security edge cases, and end-to-end generation from the shipped examples with independently recomputed figures. The live PostgreSQL test is opt-in: set `REPORTGEN_TEST_PG_URL` and run `pytest -m postgres` (CI runs it against a PostgreSQL service container).

Regenerate example data deterministically with `python examples/generate_demo_data.py`, and benchmarks with `python benchmarks/run_benchmarks.py`.

## Known limitations

- Single-machine, in-memory processing: datasets are assembled as pandas DataFrames (chunked reads bound input memory, but the working set must fit in RAM). The default input limit is 200 MB per file, configurable via `options.max_input_mb`.
- Excel sources support `.xlsx`/`.xlsm` (not legacy `.xls`); entire worksheets are loaded into memory.
- PDF supports one bundled font family (DejaVu Sans via Matplotlib, with Helvetica fallback); right-to-left script shaping is not supported by ReportLab paragraph layout.
- Database read-only enforcement is defence-in-depth (query validation + read-only sessions); always use a least-privilege read-only database account as well.
- No graphical interface, email delivery, or cloud storage in v0.1.0 — the CLI and OS schedulers are the supported interfaces.
- Custom Jinja layout templates are not user-loadable by design (safety); customisation happens through report definitions and themes.

## License

MIT — see [LICENSE](LICENSE).
