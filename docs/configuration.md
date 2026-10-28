# Configuration guide

A **report definition** is a YAML (or TOML/JSON) file, schema `version: 1`. Run `reportgen validate <file>` to check it — errors name the exact field. Unknown keys are rejected so typos cannot silently pass. Relative `path`/`output_dir` values resolve against the definition file's directory, which makes definitions portable.

Secrets are never written literally: use `url_env: MY_VAR` or `${MY_VAR}` expansion inside strings. A missing variable is an error.

## Top-level structure

```yaml
version: 1                  # required, must be 1
report: {...}               # title, template, theme, summary...
sources: [...]              # at least one
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
| `filter` | `column`, `operator`, `value` — or `conditions: [...]` + `logic: and\|or` | Operators: `== != > >= < <= in not_in contains is_null not_null between` (`value: [lo, hi]`). `contains` is a literal substring match, never regex |
| `sort` | `by: [...]`, `ascending: true\|false` | Stable sort |
| `dedupe` | `subset: [...]`, `keep: first\|last` | Alias `drop_duplicates` |
| `fill_missing` | `values: {col: v}` or `column`+`value` or `strategy: zero\|mean\|median\|ffill\|bfill` (+`columns`) | Filled values are explicit — the tool never invents data behind your back |
| `drop_missing` | `subset: [...]`, `how: any\|all` | |
| `convert_type` | `types: {col: int\|float\|string\|date\|bool\|category}`, `errors: coerce\|raise` | `coerce` → invalid values become missing |
| `date_normalize` | `column`/`columns`, `format:` (strptime), `truncate: month\|day` | Unparseable → missing |
| `calculate` | `column:` (new name), `expression: "quantity * unit_price"` | Arithmetic between columns/numbers only (`+ - * / // % **`). No function calls, attributes, or subscripts — enforced by an AST whitelist. Division by zero → missing |
| `group_aggregate` | `group_by: [...]`, `aggregations:`, `sort_by: [...]` | Aggregations as `{col: sum}`, `{col: [sum, mean]}` (→ `col_sum`, `col_mean`), or list form `[{column, aggregation, as}]`. Empty `group_by` → one summary row |
| `running_total` | `column`, `output`, `group_by` | Sort first if order matters |
| `pct_change` | `column`, `output`, `periods`, `group_by` | Fraction (0.08 = +8%) |
| `merge` | `other: dataset`, `on: [...]` (or `left_on`/`right_on`), `how: inner\|left\|right\|outer\|cross` | |
| `append` | `other: dataset` | Row concatenation |
| `limit` | `rows: N` | |
| `validate` | `required_columns`, `non_empty`, `min_rows` | Fails the run with a clear message |
| `describe` | `columns` (optional) | Summary-statistics table (count/mean/std/min/median/max/unique/missing) |

Aggregations everywhere: `count`, `sum`, `mean` (aliases `avg`, `average`), `min`, `max`, `median`, `std`, `nunique` (alias `unique_count`).

## `metrics`

Single figures shown in the metrics block and usable as `{placeholders}`:

```yaml
metrics:
  - {name: total_revenue, label: Total revenue, dataset: sales,
     aggregation: sum, column: revenue, format: currency, decimals: 2}
  - {name: orders, dataset: sales, aggregation: count, format: integer}  # no column = row count
```

Formats: `number`, `currency` ($), `percent` (expects a fraction), `integer`. Missing results render as `—`, never zero-filled silently (except `sum` of an all-missing column, which is genuinely 0).

## `sections`

Rendered in order and identically across formats (Excel always receives full tables; PDF/HTML display at most `max_rows`/`options.max_table_rows` and say so).

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

Chart fields: `type` (`line|bar|hbar|area|scatter|histogram|pie|donut|timeseries`), `x`, `y` (string or list), `group_by` (series split for line/area), `title`, `x_label`, `y_label`, `legend`, `width`, `height` (inches), `max_points` (deterministic downsampling above this). Rows with missing x/y are excluded from that chart and counted in the report's warnings.

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

CLI flags (`--format`, `--output-dir`, `--overwrite`) override `exports` per run.

## `schedule`

```yaml
schedule:
  cron: "0 6 1 * *"             # 06:00 on the 1st of each month
  description: Monthly sales pack
  timeout_seconds: 600
```

Informational metadata consumed by `reportgen jobs schedule` — the operating system's scheduler remains the executor (see docs/automation.md).
