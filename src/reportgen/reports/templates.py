"""The five built-in report templates.

Each template documents the input schema it expects and provides
default processing pipelines, metrics, and sections. A minimal config
(``report.template`` + one source) therefore produces a complete
report; any default can be overridden by specifying ``processing``,
``metrics``, or ``sections`` explicitly in the report definition.

Users create custom templates by writing their own report definition
YAML (see docs/templates.md) — a "template" is simply a reusable
definition, optionally starting from ``reportgen init --template ...``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from reportgen.errors import ConfigError


@dataclass
class TemplateDef:
    name: str
    title: str
    description: str
    required_columns: list[str]
    schema_notes: str
    example_yaml: str
    themes: list[str] = field(default_factory=lambda: ["default", "muted", "contrast"])

    def defaults(
        self, primary: str, frames: dict[str, pd.DataFrame]
    ) -> tuple[list[dict], list[dict], list[dict]]:
        """Return (pipelines, metrics, sections) as plain dicts."""
        builder = _DEFAULT_BUILDERS[self.name]
        return builder(primary, frames)


def _sales_defaults(primary, _frames):
    pipelines = [
        {
            "dataset": primary,
            "output": "monthly_revenue",
            "steps": [
                {"op": "date_normalize", "column": "date", "truncate": "month"},
                {
                    "op": "group_aggregate",
                    "group_by": ["date"],
                    "aggregations": {"revenue": "sum", "quantity": "sum"},
                },
                {"op": "sort", "by": ["date"]},
            ],
        },
        {
            "dataset": primary,
            "output": "revenue_by_region",
            "steps": [
                {
                    "op": "group_aggregate",
                    "group_by": ["region"],
                    "aggregations": {"revenue": "sum", "quantity": "sum"},
                    "sort_by": ["revenue"],
                },
            ],
        },
        {
            "dataset": primary,
            "output": "revenue_by_product",
            "steps": [
                {
                    "op": "group_aggregate",
                    "group_by": ["product"],
                    "aggregations": {"revenue": "sum"},
                    "sort_by": ["revenue"],
                },
            ],
        },
    ]
    metrics = [
        {
            "name": "total_revenue",
            "label": "Total revenue",
            "dataset": primary,
            "aggregation": "sum",
            "column": "revenue",
            "format": "currency",
        },
        {
            "name": "total_orders",
            "label": "Orders",
            "dataset": primary,
            "aggregation": "count",
            "format": "integer",
        },
        {
            "name": "average_order_value",
            "label": "Average order value",
            "dataset": primary,
            "aggregation": "mean",
            "column": "revenue",
            "format": "currency",
        },
        {
            "name": "total_units",
            "label": "Units sold",
            "dataset": primary,
            "aggregation": "sum",
            "column": "quantity",
            "format": "integer",
        },
        {
            "name": "regions",
            "label": "Regions",
            "dataset": primary,
            "aggregation": "nunique",
            "column": "region",
            "format": "integer",
        },
    ]
    sections = [
        {"title": "Key metrics", "type": "metrics"},
        {
            "title": "Monthly revenue trend",
            "type": "chart",
            "dataset": "monthly_revenue",
            "chart": {
                "type": "timeseries",
                "x": "date",
                "y": "revenue",
                "title": "Revenue by month",
                "y_label": "Revenue",
            },
        },
        {
            "title": "Revenue by region",
            "type": "chart",
            "dataset": "revenue_by_region",
            "chart": {
                "type": "bar",
                "x": "region",
                "y": "revenue",
                "title": "Revenue by region",
                "y_label": "Revenue",
            },
        },
        {"title": "Regional detail", "type": "table", "dataset": "revenue_by_region"},
        {"title": "Product detail", "type": "table", "dataset": "revenue_by_product"},
        {
            "title": "Sales records",
            "type": "table",
            "dataset": primary,
            "max_rows": 25,
            "description": "First records of the source dataset (full data in the Excel export).",
        },
    ]
    return pipelines, metrics, sections


def _business_defaults(primary, _frames):
    pipelines = [
        {
            "dataset": primary,
            "steps": [
                {"op": "calculate", "column": "profit", "expression": "revenue - expenses"},
            ],
        },
        {
            "dataset": primary,
            "output": "monthly_performance",
            "steps": [
                {"op": "date_normalize", "column": "date", "truncate": "month"},
                {
                    "op": "group_aggregate",
                    "group_by": ["date"],
                    "aggregations": {"revenue": "sum", "expenses": "sum", "profit": "sum"},
                },
                {"op": "sort", "by": ["date"]},
            ],
        },
        {
            "dataset": primary,
            "output": "department_performance",
            "steps": [
                {
                    "op": "group_aggregate",
                    "group_by": ["department"],
                    "aggregations": {"revenue": "sum", "expenses": "sum", "profit": "sum"},
                    "sort_by": ["profit"],
                },
            ],
        },
    ]
    metrics = [
        {
            "name": "total_revenue",
            "label": "Total revenue",
            "dataset": primary,
            "aggregation": "sum",
            "column": "revenue",
            "format": "currency",
        },
        {
            "name": "total_expenses",
            "label": "Total expenses",
            "dataset": primary,
            "aggregation": "sum",
            "column": "expenses",
            "format": "currency",
        },
        {
            "name": "total_profit",
            "label": "Net profit",
            "dataset": primary,
            "aggregation": "sum",
            "column": "profit",
            "format": "currency",
        },
        {
            "name": "average_monthly_revenue",
            "label": "Average record revenue",
            "dataset": primary,
            "aggregation": "mean",
            "column": "revenue",
            "format": "currency",
        },
    ]
    sections = [
        {"title": "Key metrics", "type": "metrics"},
        {
            "title": "Revenue and expenses by month",
            "type": "chart",
            "dataset": "monthly_performance",
            "chart": {
                "type": "line",
                "x": "date",
                "y": ["revenue", "expenses"],
                "title": "Revenue vs expenses",
                "y_label": "Amount",
            },
        },
        {
            "title": "Profit by department",
            "type": "chart",
            "dataset": "department_performance",
            "chart": {
                "type": "bar",
                "x": "department",
                "y": "profit",
                "title": "Net profit by department",
                "y_label": "Profit",
            },
        },
        {"title": "Department detail", "type": "table", "dataset": "department_performance"},
        {"title": "Monthly detail", "type": "table", "dataset": "monthly_performance"},
    ]
    return pipelines, metrics, sections


def _financial_defaults(primary, _frames):
    pipelines = [
        {
            "dataset": primary,
            "output": "monthly_amounts",
            "steps": [
                {"op": "date_normalize", "column": "date", "truncate": "month"},
                {"op": "group_aggregate", "group_by": ["date"], "aggregations": {"amount": "sum"}},
                {"op": "sort", "by": ["date"]},
                {"op": "running_total", "column": "amount", "output": "cumulative_amount"},
            ],
        },
        {
            "dataset": primary,
            "output": "amounts_by_category",
            "steps": [
                {
                    "op": "group_aggregate",
                    "group_by": ["category"],
                    "aggregations": {"amount": ["sum", "count", "mean"]},
                    "sort_by": ["amount_sum"],
                },
            ],
        },
    ]
    metrics = [
        {
            "name": "net_amount",
            "label": "Net amount",
            "dataset": primary,
            "aggregation": "sum",
            "column": "amount",
            "format": "currency",
        },
        {
            "name": "transactions",
            "label": "Transactions",
            "dataset": primary,
            "aggregation": "count",
            "format": "integer",
        },
        {
            "name": "average_transaction",
            "label": "Average transaction",
            "dataset": primary,
            "aggregation": "mean",
            "column": "amount",
            "format": "currency",
        },
        {
            "name": "largest_transaction",
            "label": "Largest transaction",
            "dataset": primary,
            "aggregation": "max",
            "column": "amount",
            "format": "currency",
        },
        {
            "name": "categories",
            "label": "Categories",
            "dataset": primary,
            "aggregation": "nunique",
            "column": "category",
            "format": "integer",
        },
    ]
    sections = [
        {"title": "Key metrics", "type": "metrics"},
        {
            "title": "Monthly amounts",
            "type": "chart",
            "dataset": "monthly_amounts",
            "chart": {
                "type": "timeseries",
                "x": "date",
                "y": "amount",
                "title": "Net amount by month",
                "y_label": "Amount",
            },
        },
        {
            "title": "Cumulative balance",
            "type": "chart",
            "dataset": "monthly_amounts",
            "chart": {
                "type": "area",
                "x": "date",
                "y": "cumulative_amount",
                "title": "Cumulative amount",
                "y_label": "Cumulative",
            },
        },
        {"title": "Category detail", "type": "table", "dataset": "amounts_by_category"},
        {"title": "Transactions", "type": "table", "dataset": primary, "max_rows": 25},
    ]
    return pipelines, metrics, sections


def _operations_defaults(primary, _frames):
    pipelines = [
        {
            "dataset": primary,
            "output": "daily_output",
            "steps": [
                {"op": "date_normalize", "column": "date", "truncate": "day"},
                {
                    "op": "group_aggregate",
                    "group_by": ["date"],
                    "aggregations": {
                        "output_units": "sum",
                        "defects": "sum",
                        "downtime_hours": "sum",
                    },
                },
                {"op": "sort", "by": ["date"]},
            ],
        },
        {
            "dataset": primary,
            "output": "site_performance",
            "steps": [
                {
                    "op": "group_aggregate",
                    "group_by": ["site"],
                    "aggregations": {
                        "output_units": "sum",
                        "defects": "sum",
                        "downtime_hours": ["sum", "mean"],
                    },
                    "sort_by": ["output_units"],
                },
            ],
        },
    ]
    metrics = [
        {
            "name": "total_output",
            "label": "Total output (units)",
            "dataset": primary,
            "aggregation": "sum",
            "column": "output_units",
            "format": "integer",
        },
        {
            "name": "total_defects",
            "label": "Total defects",
            "dataset": primary,
            "aggregation": "sum",
            "column": "defects",
            "format": "integer",
        },
        {
            "name": "total_downtime",
            "label": "Downtime (hours)",
            "dataset": primary,
            "aggregation": "sum",
            "column": "downtime_hours",
            "format": "number",
        },
        {
            "name": "average_daily_output",
            "label": "Average output per record",
            "dataset": primary,
            "aggregation": "mean",
            "column": "output_units",
            "format": "number",
        },
        {
            "name": "sites",
            "label": "Sites",
            "dataset": primary,
            "aggregation": "nunique",
            "column": "site",
            "format": "integer",
        },
    ]
    sections = [
        {"title": "Key metrics", "type": "metrics"},
        {
            "title": "Daily output",
            "type": "chart",
            "dataset": "daily_output",
            "chart": {
                "type": "timeseries",
                "x": "date",
                "y": "output_units",
                "title": "Output by day",
                "y_label": "Units",
            },
        },
        {
            "title": "Output by site",
            "type": "chart",
            "dataset": "site_performance",
            "chart": {
                "type": "bar",
                "x": "site",
                "y": "output_units",
                "title": "Output by site",
                "y_label": "Units",
            },
        },
        {"title": "Site detail", "type": "table", "dataset": "site_performance"},
        {"title": "Daily detail", "type": "table", "dataset": "daily_output"},
    ]
    return pipelines, metrics, sections


def _general_defaults(primary, frames):
    frame = frames.get(primary, pd.DataFrame())
    numeric = [c for c in frame.columns if pd.api.types.is_numeric_dtype(frame[c])]
    pipelines = [
        {
            "dataset": primary,
            "output": "summary_statistics",
            "steps": [
                {"op": "describe"},
            ],
        },
    ]
    metrics = [
        {
            "name": "row_count",
            "label": "Records",
            "dataset": primary,
            "aggregation": "count",
            "format": "integer",
        },
    ]
    sections: list[dict[str, Any]] = [
        {"title": "Key metrics", "type": "metrics"},
        {
            "title": "Summary statistics",
            "type": "table",
            "dataset": "summary_statistics",
            "description": "Descriptive statistics for every numeric column.",
        },
    ]
    if numeric:
        sections.append(
            {
                "title": f"Distribution of {numeric[0]}",
                "type": "chart",
                "dataset": primary,
                "chart": {
                    "type": "histogram",
                    "x": numeric[0],
                    "title": f"Distribution of {numeric[0]}",
                },
            }
        )
        metrics.append(
            {
                "name": "first_numeric_mean",
                "label": f"Mean {numeric[0]}",
                "dataset": primary,
                "aggregation": "mean",
                "column": numeric[0],
                "format": "number",
            }
        )
    sections.append({"title": "Data", "type": "table", "dataset": primary, "max_rows": 50})
    return pipelines, metrics, sections


_DEFAULT_BUILDERS = {
    "sales": _sales_defaults,
    "business": _business_defaults,
    "financial": _financial_defaults,
    "operations": _operations_defaults,
    "general": _general_defaults,
}

# ---------------------------------------------------------------------------
# Example configurations (used by `reportgen init` and examples/configs/)
# Paths assume the config lives in examples/configs/ — adjust to taste.
# ---------------------------------------------------------------------------

_SALES_YAML = """\
# Sales analysis report — synthetic example data.
# Generate with: reportgen generate examples/configs/sales-report.yaml --all --overwrite
version: 1
report:
  title: Sales Analysis Report
  subtitle: Synthetic sales data — for demonstration only
  author: Example Analytics Team
  organization: Example Co
  template: sales
  theme: default
  summary: >-
    This report summarises {total_orders} synthetic orders totalling
    {total_revenue} in revenue across {regions} regions. The average
    order value was {average_order_value} with {total_units} units sold.
  methodology: >-
    Revenue is taken from the source file (quantity × unit price at order
    time). Monthly figures group orders by calendar month of the order date.
sources:
  - name: sales
    type: csv
    path: ../data/synthetic_sales.csv
    expected_columns: [date, region, product, category, quantity, unit_price, revenue]
processing:
  - dataset: sales
    output: monthly_revenue
    steps:
      - op: date_normalize
        column: date
        truncate: month
      - op: group_aggregate
        group_by: [date]
        aggregations: {revenue: sum, quantity: sum}
      - op: sort
        by: [date]
  - dataset: sales
    output: revenue_by_region
    steps:
      - op: group_aggregate
        group_by: [region]
        aggregations: {revenue: sum, quantity: sum}
        sort_by: [revenue]
  - dataset: sales
    output: revenue_by_product
    steps:
      - op: group_aggregate
        group_by: [product]
        aggregations: {revenue: sum}
        sort_by: [revenue]
metrics:
  - {name: total_revenue, label: Total revenue, dataset: sales, aggregation: sum, column: revenue, format: currency}
  - {name: total_orders, label: Orders, dataset: sales, aggregation: count, format: integer}
  - {name: average_order_value, label: Average order value, dataset: sales, aggregation: mean, column: revenue, format: currency}
  - {name: total_units, label: Units sold, dataset: sales, aggregation: sum, column: quantity, format: integer}
  - {name: regions, label: Regions, dataset: sales, aggregation: nunique, column: region, format: integer}
sections:
  - {title: Key metrics, type: metrics}
  - title: Monthly revenue trend
    type: chart
    dataset: monthly_revenue
    chart: {type: timeseries, x: date, y: revenue, title: Revenue by month, y_label: Revenue}
  - title: Revenue by region
    type: chart
    dataset: revenue_by_region
    chart: {type: bar, x: region, y: revenue, title: Revenue by region, y_label: Revenue}
  - {title: Regional detail, type: table, dataset: revenue_by_region}
  - {title: Product detail, type: table, dataset: revenue_by_product}
  - {title: Sales records, type: table, dataset: sales, max_rows: 25}
exports:
  output_dir: ../reports
  filename: sales-report-{date}
  formats: [pdf, html, xlsx]
  overwrite: false
"""

_BUSINESS_YAML = """\
# Business performance report — synthetic example data.
version: 1
report:
  title: Business Performance Report
  subtitle: Synthetic departmental data — for demonstration only
  template: business
  theme: default
  summary: >-
    Revenue of {total_revenue} against expenses of {total_expenses}
    produced a net profit of {total_profit}.
sources:
  - name: performance
    type: csv
    path: ../data/business_performance.csv
    expected_columns: [date, department, revenue, expenses]
exports:
  output_dir: ../reports
  filename: business-performance-{date}
  formats: [pdf, html, xlsx]
"""

_FINANCIAL_YAML = """\
# Financial summary report — synthetic example data.
version: 1
report:
  title: Financial Summary Report
  subtitle: Synthetic ledger data — for demonstration only
  template: financial
  theme: muted
  summary: >-
    {transactions} transactions across {categories} categories netted
    {net_amount}. The average transaction was {average_transaction}.
sources:
  - name: ledger
    type: csv
    path: ../data/financial_summary.csv
    expected_columns: [date, category, account, amount]
exports:
  output_dir: ../reports
  filename: financial-summary-{date}
  formats: [pdf, html, xlsx]
"""

_OPERATIONS_YAML = """\
# Operational metrics report — synthetic example data.
version: 1
report:
  title: Operational Metrics Report
  subtitle: Synthetic production data — for demonstration only
  template: operations
  theme: default
  summary: >-
    {sites} sites produced {total_output} units with {total_defects}
    defects and {total_downtime} hours of downtime.
sources:
  - name: operations
    type: csv
    path: ../data/operations.csv
    expected_columns: [date, site, output_units, defects, downtime_hours]
exports:
  output_dir: ../reports
  filename: operations-{date}
  formats: [pdf, html, xlsx]
"""

_GENERAL_YAML = """\
# General-purpose data analysis — works with any tabular dataset.
version: 1
report:
  title: Data Analysis Report
  subtitle: Synthetic sales data — for demonstration only
  template: general
  theme: default
sources:
  - name: data
    type: csv
    path: ../data/synthetic_sales.csv
exports:
  output_dir: ../reports
  filename: data-analysis-{date}
  formats: [pdf, html, xlsx]
"""

TEMPLATES: dict[str, TemplateDef] = {
    "business": TemplateDef(
        name="business",
        title="Business performance report",
        description="Revenue, expenses, and profit by department and month.",
        required_columns=["date", "department", "revenue", "expenses"],
        schema_notes="One row per department-period. 'profit' is calculated "
        "automatically as revenue - expenses.",
        example_yaml=_BUSINESS_YAML,
    ),
    "financial": TemplateDef(
        name="financial",
        title="Financial summary report",
        description="Ledger-style summary with monthly and cumulative amounts.",
        required_columns=["date", "category", "amount"],
        schema_notes="One row per transaction; negative amounts represent outflows.",
        example_yaml=_FINANCIAL_YAML,
    ),
    "sales": TemplateDef(
        name="sales",
        title="Sales analysis report",
        description="Revenue trends, regional and product breakdowns, order metrics.",
        required_columns=["date", "region", "product", "quantity", "unit_price", "revenue"],
        schema_notes="One row per order line. 'revenue' should equal quantity × unit_price.",
        example_yaml=_SALES_YAML,
    ),
    "operations": TemplateDef(
        name="operations",
        title="Operational metrics report",
        description="Output, defects, and downtime by site and day.",
        required_columns=["date", "site", "output_units", "defects", "downtime_hours"],
        schema_notes="One row per site-day.",
        example_yaml=_OPERATIONS_YAML,
    ),
    "general": TemplateDef(
        name="general",
        title="General-purpose data analysis report",
        description="Summary statistics, distributions, and a data table for any dataset.",
        required_columns=[],
        schema_notes="Any tabular dataset. Numeric columns are summarised automatically.",
        example_yaml=_GENERAL_YAML,
    ),
}


def get_template(name: str) -> TemplateDef:
    template = TEMPLATES.get(str(name).lower())
    if template is None:
        raise ConfigError(
            f"Unknown template '{name}'. Available templates: {', '.join(sorted(TEMPLATES))}. "
            "Run 'reportgen templates list' for details."
        )
    return template
