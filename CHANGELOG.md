# Changelog

All notable changes to this project are documented here. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-10-30

Correctness, security, and auditability release. Every confirmed bug below has a regression test.

### Added

- Run parameters (`parameters:` + `${params.name}` in source params, processing steps, filenames; `{name}` in text) with run-time date tokens (`previous_month_start`, `days_ago:30`, ...) and `--param` overrides on `generate` and `jobs add` — one definition now serves every scheduled period.
- `report.currency` (default `$`) and `report.timezone` (default `UTC`) — generated-at timestamps, `{date}` filenames, and date tokens resolve in the reporting timezone.
- `generate --dry-run`: full build (including chart rendering) with no files written; prints row counts, metrics, warnings, and would-be paths.
- `generate --manifest PATH`: run manifest with config/input SHA-256 fingerprints, effective settings, per-dataset row counts, metrics, warnings, and per-output status — written for dry runs and failed runs too. Jobs store a manifest next to each run log.
- `data_quality` processing step (`not_null`, `type`, `range`, `unique` with `on_error: fail|drop|coerce|clamp`) and `merge` cardinality validation (`validate:`). Handled violations are counted in report warnings.
- Typed schemas for every processing step and source `options` — unknown keys, wrong types, missing required parameters, and bad filter operators fail at load time.
- Example: `examples/configs/monthly-sales-window.yaml`.

### Fixed (security / data loss)

- Job retention can no longer delete the newest report when a filename repeats (files of retained runs are protected).
- Database parameter values are hidden from exceptions/logs; credentials in URL query/fragment parameters are redacted everywhere.
- Job names are validated; lock/log paths are contained (path traversal and scheduler command injection blocked).
- SQLite URLs are forced read-only (a missing database file is never created); only SQLite/PostgreSQL dialects are accepted.
- Job locking is liveness-based (PID/host/token + heartbeat) instead of age-only; job-store updates are serialised with unique temp files; locks are released even when history writes fail.

### Fixed (accuracy / reliability)

- Numeric aggregation is unified: grouped and scalar sums agree, numeric text coerces consistently, non-numeric values are rejected explicitly, integer sums no longer overflow.
- Filters evaluate only the selected comparison; invalid `logic` is rejected. `pct_change` no longer implicitly forward-fills; zero denominators produce missing + a warning, never ±∞.
- Charts aggregate categorical data before reducing (pie overflow becomes “Other”), disclose every reduction, never exceed `max_points`, never turn numeric axes into dates, draw grouped bars side-by-side, and honour `group_by` for scatter; impossible combinations error instead of being ignored.
- CSV/Excel headers are checked as-written at their physical row (after skipped rows) before any pandas renaming; declared all-empty columns are preserved.
- `max_rows` is enforced during loading for CSV/Excel/JSONL/databases; JSONL and delimiter sampling stream; Excel expansion/cells and merge row-growth are bounded before materialising.
- `reportgen validate` now runs the full build, so broken pipelines/charts fail before generation; template-default dataset names resolve during reference checks.
- General template works on text-only datasets; SQLite accepts `url_env`-only configs; `sources inspect` handles filenames with spaces.
- Excel exports include text sections and methodology and honour metric `decimals`/currency. PDF charts fit both page dimensions; oversized cells are truncated with a note instead of failing the export.
- Failure JSON and job history list files created before a later format failed (`PartialExportError.outputs`).

### Changed

- Silent coercions/truncations became explicit, warned policies throughout (processing notes and chart notes appear in the report's warnings and in the manifest).
- Documentation corrected: bounded-memory, read-only, dialect, and export-parity claims now match tested behaviour.

## [0.1.0] - 2026-10-28

Initial release.

### Added

- `reportgen` CLI: `init`, `validate`, `generate` (single/multi-format, `--json`, `--overwrite`), `sources inspect|types`, `templates list|show`, `jobs add|list|run|remove|schedule`, `history list`, `version`; global `--verbose`/`--quiet`; clean non-zero exits without stack traces for user errors.
- Data sources: CSV/TSV with delimiter detection and chunked reads, Excel `.xlsx` multi-sheet, JSON/JSON Lines with `record_path` extraction and field mapping, SQLite and PostgreSQL with read-only parameterised queries and environment-variable credentials.
- Processing pipeline: select/rename/drop, structured filters, sort, dedupe, missing-value strategies, type conversion, date normalisation, AST-whitelist calculated columns, group aggregations (count/sum/mean/min/max/median/std/unique count), running totals, percentage change, merge/append, validation and describe steps.
- Five templates (business, financial, sales, operations, general) with documented schemas, defaults, themes, and working example configurations; synthetic example datasets for each.
- Charts: line, bar, horizontal bar, area, scatter, histogram, pie, donut, time-series with consistent themes and missing-data accounting.
- Exporters: paginated PDF (headers/footers/page numbers, DejaVu Sans), self-contained HTML (sandboxed Jinja, embedded charts, print CSS), styled Excel (summary + section sheets, native types, embedded charts); atomic no-clobber publication for all formats.
- Automation: saved jobs with subprocess execution, per-run logs, run history, locking, timeouts, opt-in retention, and cron/Task Scheduler/launchd guidance.
- Security controls: safe YAML, read-only SQL validation, path-traversal and formula-injection protections, input limits, credential masking (see docs/security.md).
- Test suite (130+ tests), cross-platform CI with PostgreSQL service leg, packaging verification, dependency auditing, and measured benchmarks (docs/benchmarks.md).
