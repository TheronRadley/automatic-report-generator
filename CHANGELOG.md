# Changelog

All notable changes to this project are documented here. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
