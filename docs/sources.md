# Data sources

Every source is declared in the report definition and produces a named dataset. All adapters share: `columns` (keep-only, validated), `expected_columns` (fail if absent), `max_rows`, duplicate/empty column-name rejection, and the per-file size limit (`options.max_input_mb`, default 200 MB).

## CSV / TSV (`type: csv` / `tsv`)

| `options` key | Default | Notes |
|---|---|---|
| `delimiter` | auto-detected (`, ; TAB \|`) | Set explicitly for unusual files |
| `encoding` | `utf-8-sig` | e.g. `latin-1`; decode failures name the encoding to fix |
| `header_row` | `0` | Zero-based header line |
| `skip_rows` | — | Rows to skip before the header |
| `na_values` | pandas defaults | Extra missing-value markers |
| `parse_dates` | — | Columns to parse as dates at load |
| `chunk_rows` | — | Read in chunks of N rows (bounds read-chunk memory; stops early at `max_rows`) |

`max_rows` is enforced *during* the read (pandas `nrows` / chunk stop), the delimiter sample is taken at the physical header position (`skip_rows + header_row`), and duplicate columns are checked against the original header — across all adapters, duplicates are errors, never silently renamed by pandas.

Malformed CSVs, empty files, and duplicate headers produce explicit errors. `.tsv`/type `tsv` defaults to tab.

## Excel (`type: excel`)

`.xlsx`/`.xlsm` via OpenPyXL.

| `options` key | Default | Notes |
|---|---|---|
| `sheet` | `0` (first) | Worksheet name or zero-based index; errors list available sheets |
| `header_row`, `skip_rows`, `parse_dates` | — | As CSV |
| `max_expanded_mb` | 500 | Upper bound on the uncompressed workbook size (decompression-bomb guard) |
| `max_cells` | 10,000,000 | Upper bound on worksheet cells |

Header validation runs against the original header row before any pandas renaming. A blank header cell above a column that contains data is an error; declared columns that are entirely empty are preserved. `max_rows` is applied during the read.

Dates and numbers keep their native types. Fully empty trailing rows/columns are dropped. Legacy `.xls` is not supported — save as `.xlsx`.

## JSON / JSON Lines (`type: json` / `jsonl`)

Accepts a JSON array of objects, a single object (one row), or — for `jsonl`, `.jsonl`, `.ndjson` — one object per line. Nested objects are flattened with dotted names (`address.city`).

| `options` key | Notes |
|---|---|
| `record_path` | Dotted path to the record array inside a document, e.g. `payload.orders`. If a document object contains record arrays and no path is given, the error names the candidate keys |
| `field_mapping` | `{old: new}` renames applied after flattening (unknown keys are errors) |
| `encoding` | Default `utf-8` |

Invalid JSON reports the exact line (JSONL) or parser message (JSON). JSONL is streamed line by line and stops at `max_rows`, so an over-limit file is reported without being loaded whole. Plain JSON documents are still parsed in one pass (the format has no line structure); the `max_input_mb` cap applies.

## SQLite (`type: sqlite`)

```yaml
- name: sales
  type: sqlite
  path: ../data/sales.sqlite
  query: "SELECT * FROM sales WHERE region = :region"
  params: {region: "North"}
  # or simply: table: sales
```

Files are opened `mode=ro` (read-only at the SQLite layer). The same applies to SQLite URLs given via `url`/`url_env`: read-only mode is forced and a missing file is an error — a report run can never create a database. Write-mode URL options (`mode=rw`, `mode=rwc`) are rejected. `options.timeout_seconds` (default 30) and `options.chunk_rows` apply, and result fetching is bounded by `max_rows`.

## PostgreSQL (`type: postgres`, or `type: database` with a PostgreSQL URL)

Requires the driver extra: `pip install "automatic-report-generator[postgres]"` (psycopg 3). Provide the SQLAlchemy URL via the environment — never commit credentials:

```bash
export REPORT_DB_URL="postgresql://report_reader@db.internal:5432/analytics"
```

```yaml
- name: orders
  type: postgres
  url_env: REPORT_DB_URL
  query: >
    SELECT order_date AS date, region, SUM(amount) AS revenue
    FROM orders WHERE order_date >= :from_date GROUP BY 1, 2
  params: {from_date: "2025-01-01"}
  options: {timeout_seconds: 30}
```

Read-only is enforced three ways: (1) queries are validated as a single `SELECT`/`WITH` statement with no write keywords or comments; (2) sessions run `SET TRANSACTION READ ONLY` + `statement_timeout`; (3) you should still grant the account only `SELECT` — validation is defence-in-depth, and a keyword inside a string literal will be conservatively rejected. Values are always bound parameters, and parameter **values are never shown** in errors or logs (only parameter names). Connection URLs are masked in every log line — userinfo *and* sensitive query/fragment parameters (`password=`, `token=`, ...). Unreachable servers fail with the masked URL and a sanitised driver message, after `timeout_seconds`. Fetching is bounded: at most `max_rows + 1` rows are retrieved, so an over-limit result errors instead of exhausting memory.

Only two database engines are supported: **SQLite and PostgreSQL**. `type: database` exists to address them by URL; any other SQLAlchemy dialect is rejected *before* connecting, because read-only enforcement is dialect-specific.

## Adding a connector

Adapters live in `src/reportgen/sources/`: implement `load_*(cfg, ...) -> LoadedDataset`, call `finalise_frame(...)` for the shared validation, register the type in `factory.py` (and the `SourceType` literal in `config/models.py`). The engine never changes.
