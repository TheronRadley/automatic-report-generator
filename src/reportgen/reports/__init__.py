"""Report document model, built-in templates, and the build pipeline."""

from reportgen.reports.builder import ExecuteResult, build_report, execute_report, run_report
from reportgen.reports.model import MetricResult, ReportDocument, ReportSection
from reportgen.reports.templates import TEMPLATES, TemplateDef, get_template

__all__ = [
    "MetricResult",
    "ReportDocument",
    "ReportSection",
    "TEMPLATES",
    "TemplateDef",
    "get_template",
    "build_report",
    "run_report",
    "execute_report",
    "ExecuteResult",
]
