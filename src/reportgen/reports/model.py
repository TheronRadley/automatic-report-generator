"""Format-neutral report document.

The builder produces one :class:`ReportDocument`; every exporter
(PDF, HTML, Excel) renders the same document, so figures can never
diverge between formats.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pandas as pd


@dataclass
class MetricResult:
    name: str
    label: str
    value: float
    formatted: str
    number_format: str = "number"
    dataset: str = ""


@dataclass
class ReportSection:
    title: str
    kind: str  # text | metrics | table | chart
    text: str | None = None
    description: str | None = None
    # The FULL table. PDF/HTML cap display at render time (see
    # ``display_rows`` and ``ReportDocument.max_table_rows``); Excel
    # always writes every row.
    table: pd.DataFrame | None = None
    dataset_name: str | None = None
    chart_path: Path | None = None
    chart_title: str | None = None
    total_rows: int = 0
    display_rows: int | None = None

    def display_cap(self, document_cap: int) -> int:
        if self.display_rows is None:
            return document_cap
        return min(document_cap, self.display_rows)


@dataclass
class ReportDocument:
    title: str
    subtitle: str | None
    author: str | None
    organization: str | None
    template: str
    theme: str
    generated_at: datetime
    summary: str
    methodology: str | None
    footer: str | None
    metrics: list[MetricResult] = field(default_factory=list)
    sections: list[ReportSection] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    max_table_rows: int = 100

    @property
    def generated_label(self) -> str:
        return self.generated_at.strftime("%Y-%m-%d %H:%M UTC")

    def metric_map(self) -> dict[str, MetricResult]:
        return {m.name: m for m in self.metrics}
