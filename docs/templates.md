# Templates

A template supplies default processing pipelines, metrics, and sections for a documented input schema. Select one with `report.template`; override any piece by writing explicit `processing:`, `metrics:`, or `sections:` blocks (each block, when present, fully replaces the template's corresponding default). Browse them with `reportgen templates list` / `reportgen templates show <name>`, and start from one with `reportgen init --template <name>`.

If the primary dataset is missing a template's required columns, generation stops with the exact missing names — templates never guess.

## The five templates

### `sales` — Sales analysis report
Required: `date, region, product, quantity, unit_price, revenue` (one row per order line).
Builds monthly revenue (time-series chart), revenue by region (bar chart + table), revenue by product, and metrics: total revenue, orders, average order value, units sold, region count. Example: `examples/configs/sales-report.yaml`.

### `business` — Business performance report
Required: `date, department, revenue, expenses`. Calculates `profit = revenue - expenses`, monthly revenue-vs-expenses line chart, profit-by-department bar chart, department/month tables, revenue/expense/profit metrics. Example: `examples/configs/business-performance.yaml`.

### `financial` — Financial summary report
Required: `date, category, amount` (negative = outflow). Monthly amounts + running cumulative balance (area chart), per-category sum/count/mean, net/average/largest/category metrics. Example: `examples/configs/financial-summary.yaml`.

### `operations` — Operational metrics report
Required: `date, site, output_units, defects, downtime_hours`. Daily output time-series, output-by-site bar chart, site table (output, defects, downtime sum/mean), output/defect/downtime metrics. Example: `examples/configs/operations-report.yaml`.

### `general` — General-purpose data analysis
Any tabular dataset. Summary-statistics table for every numeric column (`describe` step), a histogram of the first numeric column, record-count/mean metrics, and a data table. Example: `examples/configs/data-analysis.yaml`.

## Themes

`report.theme`: `default` (deep blue editorial), `muted` (soft greys/greens), `contrast` (high-contrast). Themes control chart palettes and table accents consistently across PDF, HTML, and Excel. Layout uses restrained, print-friendly editorial styling in all themes.

## Creating a custom template

A custom template is simply a reusable report definition:

1. `reportgen init --template sales --output templates/my-template.yaml` (or start blank with `template: general`).
2. Replace the source paths with your standard layout, and write explicit `processing` / `metrics` / `sections` blocks — these *are* your template's structure. Headings, metrics, charts, and themes are all editable.
3. Document the input schema your template expects with `expected_columns` on each source so misuse fails fast with a clear message.
4. Reuse it per report by copying the file and changing `report.title`, source `path`s, and `exports` — or keep one canonical file and generate directly from it (paths resolve relative to the file, so it can live in a shared folder).
5. Register recurring ones as jobs: `reportgen jobs add monthly-sales templates/my-template.yaml`.

For a code-level built-in template, add a `TemplateDef` + defaults builder in `src/reportgen/reports/templates.py` (see docs/development.md); user-loadable Jinja layouts are intentionally not supported, for the safety reasons in docs/security.md.
