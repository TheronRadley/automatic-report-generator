# Configuration guide

A **report definition** is a YAML (or TOML/JSON) file, schema `version: 1`. Run `reportgen validate <file>` to check it — errors name the exact field. Unknown keys are rejected so typos cannot silently pass, and this applies inside processing steps and source `options` too: every step option is type-checked against that step's schema at load time. With source checks enabled (the default), `validate` performs a **complete build** (load, pipelines, metrics, chart rendering into a throwaway directory) — anything that would fail `generate` fails validation first; no report files are written. Relative `path`/`output_dir` values resolve against the definition file's directory, which makes definitions portable.

Secrets are never written literally: use `url_env: MY_VAR` or `${MY_VAR}` expansion inside strings. A missing variable is an error.

## Top-level structure

```yaml
version: 1                  # required, must be 1
report: {...}               # title, template, theme, summary...
sources: [...]              # at least one
parameters: {...}           # optional run parameters (dates, limits...)
processing: [...]           # optional (template defaults otherwise)
metrics: [...]              # optional (template defaults otherwise)
sections: [...]             # optional (template defaults otherwise)
exports: {...}
options: {...}
schedule: {...}             # optional; used by `jobs schedule`
```

## `report`

| Key | Meaning |
|---|---|
| `title`, `subtitle` | Report heading block |
| `author`, `organization` | Printed in headers/metadata |
| `template` | `business` \| `financial` \| `sales` \| `operations` \| `general` |
| `theme` | `default` \| `muted` \| `contrast` (chart/table colours) |
| `summary` | Executive summary. `{metric_name}` placeholders are replaced with formatted computed metrics; `{report_date}` also works. Unknown placeholders are errors. Omit for an automatic factual summary |
| `methodology` | Optional methodology section (placeholders allowed) |
| `footer` | Footer text (defaults to the title) |
| `currency` | Currency prefix for `currency` metrics and Excel number formats (default `$`; 1–4 chars, e.g. `€`, `£`, `DZD `) |
| `timezone` | IANA timezone (e.g. `Europe/Berlin`, default `UTC`). Generated-at timestamps, `{date}` filenames and date-token parameters resolve in this timezone |

## `sources`

```yaml
- name: sales               # dataset name used everywhere else
  type: csv                 # csv|tsv|excel|json|jsonl|sqlite|postgres|database
  path: ../data/sales.csv   # files; sqlite may also use path
  columns: [date, region]   # optional: keep only these (validated)
  expected_columns: [...]   # optional: fail if absent
  max_rows: 100000          # optional cap (records a warning when applied)
  options: {...}            # per-type, see docs/sources.md
  # databases only:
  url: "postgresql://reader@${DB_HOST}/app"   # or url_env: DATABASE_URL
  query: "SELECT ... WHERE region = :region" # read-only, single statement
  table: sales              # ...or a bare table name instead of query
  params: {region: "North"} # bind parameters — never string-format SQL
```

## `processing`

Ordered pipelines. Each reads `dataset` and writes `output` (default: replaces `dataset`). Steps run in order; pipelines can build on earlier outputs and `merge`/`append` other datasets by name.

```yaml
processing:
  - dataset: sales
    output: monthly
    steps:
      - {op: date_normalize, column: date, truncate: month}
      - {op: group_aggregate, group_by: [date], aggregations: {revenue: sum}}
```

| Step (`op`) | Parameters | Notes |
|---|---|---|
| `select` | `columns: [...]` | |
| `rename` | `mapping: {old: new}` | |
| `drop_columns` | `columns: [...]` | |
| `filter` | `column`, `operator`, `value` — or `conditions: [...]` + `logic: and\|or` | Operators: `== != > >= < <= in not_in contains is_null not_null between` (`value: [lo, hi]`). `contains` is a literal substring match, never regex. Only the requested comparison is evaluated; an invalid `logic` is an error |
| `sort` | `by: [...]`, `ascending: true\|false` | Stable sort |
| `dedupe` | `subset: [...]`, `keep: first\|last` | Alias `drop_duplicates` |
| `fill_missing` | `values: {col: v}` or `column`+`value` or `strategy: zero\|mean\|median\|ffill\|bfill` (+`columns`) | Filled values are explicit — the tool never invents data behind your back |
| `drop_missing` | `subset: [...]`, `how: any\|all` | |
| `convert_type` | `types: {col: int\|float\|string\|date\|bool\|category}`, `errors: coerce\|raise` | `coerce` → invalid values become missing |
| `date_normalize` | `column`/`columns`, `format:` (strptime), `truncate: month\|day` | Unparseable → missing |
| `calculate` | `column:` (new name), `expression: "quantity * unit_price"` | Arithmetic between columns/numbers only (`+ - * / // % **`). No function calls, attributes, or subscripts — enforced by an AST whitelist. Division by zero → missing |
| `group_aggregate` | `group_by: [...]`, `aggregations:`, `sort_by: [...]` | Aggregations as `{col: sum}`, `{col: [sum, mean]}` (→ `col_sum`, `col_mean`), or list form `[{column, aggregation, as}]`. Empty `group_by` → one summary row |
| `running_total` | `column`, `output`, `group_by` | Sort first if order matters |
| `pct_change` | `column`, `output`, `periods`, `group_by` | Fraction (0.08 = +8%). No implicit forward-fill: a missing/zero denominator yields a missing result and a report warning — never ±∞ |
| `merge` | `other: dataset`, `on: [...]` (or `left_on`/`right_on`), `how: inner\|left\|right\|outer\|cross`, `validate: one_to_one\|one_to_many\|many_to_one`, `max_result_rows: N` | Row growth is predicted from key multiplicity *before* joining; results above the limit (default max(1M, 10× inputs)) fail instead of exhausting memory. `validate` turns an unexpected key fan-out into an error |
| `append` | `other: dataset` | Row concatenation |
| `limit` | `rows: N` | |
| `validate` | `required_columns`, `non_empty`, `min_rows` | Fails the run with a clear message |
| `describe` | `columns` (optional) | Summary-statistics table (count/mean/std/min/median/max/unique/missing) |
| `data_quality` | `rules: [{column(s), check, on_error, ...}]` | Checks: `not_null`, `type` (`as: int\|float\|date\|bool\|string`), `range` (`min:`/`max:`), `unique`. `on_error`: `fail` (default), `drop` violating rows, `coerce` invalid values to missing, `clamp` (range only). Handled violations are counted in the report's warnings — nothing is silently discarded |

Aggregations everywhere: `count`, `sum`, `mean` (aliases `avg`, `average`), `min`, `max`, `median`, `std`, `nunique` (alias `unique_count`).

## `metrics`

Single figures shown in the metrics block and usable as `{placeholders}`:

```yaml
metrics:
  - {name: total_revenue, label: Total revenue, dataset: sales,
     aggregation: sum, column: revenue, format: currency, decimals: 2}
  - {name: orders, dataset: sales, aggregation: count, format: integer}  # no column = row count
```

Formats: `number`, `currency` (prefix from `report.currency`, default `$`), `percent` (expects a fraction), `integer`. `decimals` is honoured in every format, including Excel number formats. Numeric aggregations coerce numeric-looking text consistently ("10"+"20" = 30 everywhere) and **reject** non-numeric values with a clear error instead of concatenating or skipping them; lossy coercions in `convert_type`/`date_normalize`/`calculate` are counted in the warnings. Missing results render as `—`, never zero-filled silently (except `sum` of an all-missing column, which is genuinely 0).

## `sections`

Rendered in order with the same content in every format — including text sections and methodology, which also appear in Excel (Excel always receives full tables; PDF/HTML display at most `max_rows`/`options.max_table_rows` and say so; very long PDF cells are truncated with a note rather than failing the export).

```yaml
sections:
  - {title: Key metrics, type: metrics}
  - {title: Notes, type: text, text: "Revenue reached {total_revenue}."}
  - title: Trend
    type: chart
    dataset: monthly
    chart: {type: timeseries, x: date, y: revenue, title: Revenue by month}
  - {title: Detail, type: table, dataset: monthly, columns: [date, revenue], max_rows: 50}
```

Chart fields: `type` (`line|bar|hbar|area|scatter|histogram|pie|donut|timeseries`), `x`, `y` (string or list), `group_by` (series split, including scatter), `title`, `x_label`, `y_label`, `legend`, `width`, `height` (inches), `max_points` (hard cap: deterministic downsampling never draws more points than this, and the reduction is disclosed). Rows with missing x/y are excluded from that chart and counted in the report's warnings.

Accuracy policies (also printed in warnings when they apply): categorical charts (bar/pie/donut) **aggregate first** — duplicate categories are summed before any reduction; pies with >12 slices keep the top 11 and combine the rest as “Other”; bars show the top 30 categories by total; grouped/series charts show at most 8 groups. Numeric axes stay numeric (a column of years is never reinterpreted as dates). Combining multiple `y` columns with `group_by`, or `group_by` on area/histogram/pie, is an error rather than silently ignored.

## `exports` and `options`

```yaml
exports:
  output_dir: ../reports
  filename: sales-{date}        # {date}=YYYY-MM-DD, {datetime}=YYYYMMDD-HHMMSS
  formats: [pdf, html, xlsx]
  overwrite: false              # default: refuse to replace existing files
options:
  max_input_mb: 200             # per-file input limit
  max_table_rows: 100           # PDF/HTML display cap
  chart_dpi: 200
  fail_on_empty: true           # empty source = error, not an empty report
```

CLI flags (`--format`, `--output-dir`, `--overwrite`) override `exports` per run. `exports.filename` also accepts `${params.<name>}` (see below). If a multi-format run fails part-way, the files already created are kept and are listed in the error (and in `--json` output as `outputs`), so retries/cleanup are safe.

### Dry run and manifest

`reportgen generate <file> --dry-run` performs the full build — including chart rendering — but writes no report files; it prints dataset row counts, computed metrics, warnings, and the paths it *would* write. Add `--manifest run.json` (to either command form) to record the run: config and input file fingerprints (SHA-256; for databases, a hash of masked location + query + parameter names/values — values themselves never stored), effective settings (formats, currency, timezone, resolved parameters...), per-dataset row counts, metrics, warnings, and per-output status (`created`/`failed`/`dry_run`). The manifest is written even when generation fails.

## `parameters`

Reusable values resolved at run time — the mechanism that makes one definition cover every month's scheduled run:

```yaml
parameters:
  from_date: previous_month_start
  to_date: previous_month_end
  region: North
sources:
  - name: orders
    type: postgres
    url_env: REPORT_DB_URL
    query: "SELECT ... WHERE order_date BETWEEN :from_date AND :to_date AND region = :region"
    params: {from_date: "${params.from_date}", to_date: "${params.to_date}", region: "${params.region}"}
processing:
  - dataset: orders
    steps: [{op: filter, column: order_date, operator: ">=", value: "${params.from_date}"}]
exports: {filename: "orders-${params.from_date}-${params.to_date}"}
```

Parameter values are scalars. Strings matching a **date token** resolve against “today” in `report.timezone`: `today`, `yesterday`, `tomorrow`, `month_start`, `month_end`, `previous_month_start`, `previous_month_end`, `next_month_start`, `year_start`, `year_end`, `quarter_start`, `days_ago:N`, `days_from_now:N`, `months_ago:N` (the 1st of that month). Reference parameters as `${params.name}` in source `params`, processing-step values, and `exports.filename`; `{name}` also works in summary/text placeholders. A whole-value reference keeps its type (a numeric parameter stays numeric). Override per run with `reportgen generate … --param from_date=2026-01-01`, or pin overrides on a job with `jobs add --param …` (tokens still resolve per run). Referencing an undefined parameter is an error listing the defined ones.

## `schedule`

```yaml
schedule:
  cron: "0 6 1 * *"             # 06:00 on the 1st of each month
  description: Monthly sales pack
  timeout_seconds: 600
```

Informational metadata consumed by `reportgen jobs schedule` — the operating system's scheduler remains the executor (see docs/automation.md).
