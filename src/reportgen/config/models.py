"""Pydantic models for report definitions (schema version 1).

A report definition is a YAML/TOML/JSON document. Unknown keys are
rejected so typos surface immediately with an actionable message.
Secrets must be referenced via ``*_env`` fields or ``${VAR}`` expansion,
never written literally into a committed file.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SourceType = Literal["csv", "tsv", "excel", "json", "jsonl", "sqlite", "postgres", "database"]
ExportFormat = Literal["pdf", "html", "xlsx"]
AggregationName = Literal[
    "count",
    "sum",
    "mean",
    "avg",
    "average",
    "min",
    "max",
    "median",
    "std",
    "nunique",
    "unique_count",
]
ChartType = Literal[
    "line",
    "bar",
    "hbar",
    "area",
    "scatter",
    "histogram",
    "pie",
    "donut",
    "timeseries",
]
SectionType = Literal["text", "metrics", "table", "chart"]
NumberFormat = Literal["number", "currency", "percent", "integer"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReportMeta(_Strict):
    title: str = "Untitled report"
    subtitle: str | None = None
    author: str | None = None
    organization: str | None = None
    template: str = "general"
    theme: str = "default"
    # Executive summary text. ``{metric_name}`` placeholders are replaced
    # with formatted computed metrics (plain substitution, no code eval).
    summary: str | None = None
    methodology: str | None = None
    footer: str | None = None


class SourceConfig(_Strict):
    name: str
    type: SourceType
    path: str | None = None
    # Database sources: url (may contain ${VAR}), or url_env naming the
    # environment variable that holds the full SQLAlchemy URL.
    url: str | None = None
    url_env: str | None = None
    query: str | None = None
    table: str | None = None
    # Bind parameters for the SQL query, e.g. {region: "North"}.
    params: dict[str, Any] = Field(default_factory=dict)
    columns: list[str] | None = None
    expected_columns: list[str] = Field(default_factory=list)
    max_rows: int | None = Field(default=None, gt=0)
    # Free-form per-type options (delimiter, encoding, sheet, record_path,
    # field_mapping, timeout_seconds, chunk_rows, ...). Documented in
    # docs/sources.md; validated by each adapter.
    options: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _name_is_identifier(cls, value: str) -> str:
        if not value.replace("_", "").replace("-", "").isalnum() or not value:
            raise ValueError(f"Source name must be a simple identifier, got {value!r}")
        return value

    @model_validator(mode="after")
    def _check_location(self) -> SourceConfig:
        file_types = {"csv", "tsv", "excel", "json", "jsonl", "sqlite"}
        if self.type in file_types and not self.path and not self.url:
            raise ValueError(f"Source '{self.name}' ({self.type}) requires 'path'.")
        if self.type in {"postgres", "database"} and not (self.url or self.url_env):
            raise ValueError(f"Source '{self.name}' ({self.type}) requires 'url' or 'url_env'.")
        if self.type in {"sqlite", "postgres", "database"} and not (self.query or self.table):
            raise ValueError(
                f"Database source '{self.name}' requires a read-only 'query' or a 'table'."
            )
        return self


class StepConfig(BaseModel):
    """One transformation step. Extra keys are the step's parameters."""

    model_config = ConfigDict(extra="allow")

    op: str

    @property
    def params(self) -> dict[str, Any]:
        data = self.model_dump()
        data.pop("op", None)
        return data


class PipelineConfig(_Strict):
    dataset: str
    # Name to store the result under; defaults to replacing ``dataset``.
    output: str | None = None
    steps: list[StepConfig] = Field(default_factory=list)


class MetricConfig(_Strict):
    name: str
    label: str | None = None
    dataset: str
    aggregation: AggregationName = "sum"
    column: str | None = None
    format: NumberFormat = "number"
    decimals: int = Field(default=2, ge=0, le=6)


class ChartConfig(_Strict):
    type: ChartType = "bar"
    title: str | None = None
    x: str | None = None
    y: str | list[str] | None = None
    # Optional grouping column rendered as separate series (line/bar).
    group_by: str | None = None
    x_label: str | None = None
    y_label: str | None = None
    legend: bool = True
    width: float = Field(default=8.0, gt=2, le=20)
    height: float = Field(default=4.2, gt=1.5, le=14)
    max_points: int = Field(default=5000, gt=10)


class SectionConfig(_Strict):
    title: str
    type: SectionType = "table"
    dataset: str | None = None
    text: str | None = None
    description: str | None = None
    columns: list[str] | None = None
    max_rows: int | None = Field(default=None, gt=0)
    chart: ChartConfig | None = None

    @model_validator(mode="after")
    def _check_section(self) -> SectionConfig:
        if self.type == "chart" and self.chart is None:
            raise ValueError(f"Section '{self.title}' is a chart but has no 'chart' config.")
        if self.type in {"table", "chart"} and not self.dataset:
            raise ValueError(f"Section '{self.title}' ({self.type}) requires 'dataset'.")
        return self


class ExportConfig(_Strict):
    output_dir: str = "reports"
    # May contain {date} and {datetime} placeholders.
    filename: str = "report"
    formats: list[ExportFormat] = Field(default_factory=lambda: ["pdf", "html", "xlsx"])
    overwrite: bool = False


class OptionsConfig(_Strict):
    max_input_mb: float = Field(default=200.0, gt=0)
    max_table_rows: int = Field(default=100, gt=0)
    chart_dpi: int = Field(default=200, ge=72, le=600)
    fail_on_empty: bool = True


class ScheduleConfig(_Strict):
    """Informational schedule metadata used by `reportgen jobs schedule`."""

    cron: str | None = None
    description: str | None = None
    timeout_seconds: int = Field(default=600, gt=0)


class ReportConfig(_Strict):
    version: int = 1
    report: ReportMeta = Field(default_factory=ReportMeta)
    sources: list[SourceConfig] = Field(min_length=1)
    processing: list[PipelineConfig] = Field(default_factory=list)
    metrics: list[MetricConfig] = Field(default_factory=list)
    sections: list[SectionConfig] = Field(default_factory=list)
    exports: ExportConfig = Field(default_factory=ExportConfig)
    options: OptionsConfig = Field(default_factory=OptionsConfig)
    schedule: ScheduleConfig | None = None

    @field_validator("version")
    @classmethod
    def _check_version(cls, value: int) -> int:
        if value != 1:
            raise ValueError(
                f"Unsupported config version {value}. This version of reportgen supports version 1."
            )
        return value

    @model_validator(mode="after")
    def _check_references(self) -> ReportConfig:
        names = [s.name for s in self.sources]
        if len(names) != len(set(names)):
            dupes = sorted({n for n in names if names.count(n) > 1})
            raise ValueError(f"Duplicate source names: {', '.join(dupes)}")
        known = set(names) | {p.output for p in self.processing if p.output}
        for metric in self.metrics:
            if metric.dataset not in known:
                raise ValueError(
                    f"Metric '{metric.name}' references unknown dataset "
                    f"'{metric.dataset}'. Known: {', '.join(sorted(known))}"
                )
        for section in self.sections:
            if section.dataset and section.dataset not in known:
                raise ValueError(
                    f"Section '{section.title}' references unknown dataset "
                    f"'{section.dataset}'. Known: {', '.join(sorted(known))}"
                )
        metric_names = [m.name for m in self.metrics]
        if len(metric_names) != len(set(metric_names)):
            raise ValueError("Metric names must be unique.")
        return self
