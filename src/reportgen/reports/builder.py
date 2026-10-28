"""Build a :class:`ReportDocument` from a validated config, and run exports."""

from __future__ import annotations

import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from reportgen.charts.engine import render_chart
from reportgen.config.loader import load_config
from reportgen.config.models import (
    MetricConfig,
    PipelineConfig,
    ReportConfig,
    SectionConfig,
)
from reportgen.errors import ConfigError, ProcessingError
from reportgen.logging_config import get_logger
from reportgen.processing.engine import apply_pipeline
from reportgen.processing.validation import aggregate_value, validate_frame
from reportgen.reports.model import MetricResult, ReportDocument, ReportSection
from reportgen.reports.templates import get_template
from reportgen.sources.factory import load_dataset
from reportgen.utils.formatting import format_number
from reportgen.utils.security import ensure_output_dir

logger = get_logger("reportgen.reports.builder")
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _render_placeholders(text: str, metrics: dict[str, MetricResult], document_date: str) -> str:
    """Substitute ``{metric_name}`` with formatted metric values (no eval)."""

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in {"date", "datetime"}:
            return match.group(0)  # filename placeholders pass through untouched
        if key == "report_date":
            return document_date
        metric = metrics.get(key)
        if metric is None:
            raise ConfigError(
                f"Text references unknown metric '{{{key}}}'. Available metrics: "
                f"{', '.join(sorted(metrics)) or '(none defined)'}"
            )
        return metric.formatted

    return _PLACEHOLDER_RE.sub(_replace, text)


def _load_sources(
    config: ReportConfig, base_dir: Path
) -> tuple[dict[str, pd.DataFrame], list[str], list[str]]:
    frames: dict[str, pd.DataFrame] = {}
    descriptions: list[str] = []
    warnings: list[str] = []
    max_bytes = int(config.options.max_input_mb * 1024 * 1024)
    for source in config.sources:
        loaded = load_dataset(source, base_dir, max_input_bytes=max_bytes)
        if config.options.fail_on_empty:
            validate_frame(loaded.frame, non_empty=True, context=f"Source '{source.name}'")
        frames[source.name] = loaded.frame
        descriptions.append(f"{source.name}: {loaded.description} ({len(loaded.frame)} rows)")
        warnings.extend(loaded.warnings)
        logger.info("Loaded %s: %s rows", source.name, len(loaded.frame))
    return frames, descriptions, warnings


def _with_template_defaults(
    config: ReportConfig, frames: dict[str, pd.DataFrame]
) -> tuple[list[PipelineConfig], list[MetricConfig], list[SectionConfig]]:
    template = get_template(config.report.template)
    primary = config.sources[0].name
    frame = frames.get(primary)
    if template.required_columns and frame is not None:
        # Only enforce when the config leans on template defaults.
        if not config.processing or not config.metrics or not config.sections:
            validate_frame(
                frame,
                required_columns=template.required_columns,
                context=f"Template '{template.name}' dataset '{primary}'",
            )
    default_pipelines, default_metrics, default_sections = template.defaults(primary, frames)
    pipelines = config.processing or [PipelineConfig.model_validate(p) for p in default_pipelines]
    metrics = config.metrics or [MetricConfig.model_validate(m) for m in default_metrics]
    sections = config.sections or [SectionConfig.model_validate(s) for s in default_sections]
    return pipelines, metrics, sections


def _auto_summary(document_metrics: list[MetricResult], sources: list[str]) -> str:
    parts = [f"This report is based on {len(sources)} data source(s): {'; '.join(sources)}."]
    if document_metrics:
        figures = "; ".join(f"{m.label}: {m.formatted}" for m in document_metrics)
        parts.append(f"Key figures — {figures}.")
    return " ".join(parts)


def build_report(config: ReportConfig, base_dir: Path, work_dir: Path) -> ReportDocument:
    """Execute loading, processing, metrics, charts — everything except export."""
    frames, descriptions, warnings = _load_sources(config, base_dir)
    pipelines, metric_configs, section_configs = _with_template_defaults(config, frames)

    datasets: dict[str, pd.DataFrame] = dict(frames)
    for pipeline in pipelines:
        if pipeline.dataset not in datasets:
            raise ProcessingError(
                f"Processing pipeline references unknown dataset '{pipeline.dataset}'. "
                f"Known: {', '.join(sorted(datasets))}"
            )
        output = pipeline.output or pipeline.dataset
        datasets[output] = apply_pipeline(datasets[pipeline.dataset], pipeline.steps, datasets)
        logger.info("Pipeline %s -> %s (%s rows)", pipeline.dataset, output, len(datasets[output]))

    metrics: list[MetricResult] = []
    for metric_cfg in metric_configs:
        if metric_cfg.dataset not in datasets:
            raise ConfigError(
                f"Metric '{metric_cfg.name}' uses unknown dataset '{metric_cfg.dataset}'."
            )
        value = aggregate_value(
            datasets[metric_cfg.dataset], metric_cfg.aggregation, metric_cfg.column
        )
        metrics.append(
            MetricResult(
                name=metric_cfg.name,
                label=metric_cfg.label or metric_cfg.name.replace("_", " ").title(),
                value=value,
                formatted=format_number(value, metric_cfg.format, metric_cfg.decimals),
                number_format=metric_cfg.format,
                dataset=metric_cfg.dataset,
            )
        )
    metric_map = {m.name: m for m in metrics}

    generated_at = datetime.now(UTC)
    report_date = generated_at.strftime("%Y-%m-%d")
    summary_text = config.report.summary
    if summary_text:
        summary = _render_placeholders(summary_text, metric_map, report_date)
    else:
        summary = _auto_summary(metrics, descriptions)

    work_dir.mkdir(parents=True, exist_ok=True)
    sections: list[ReportSection] = []
    chart_index = 0
    for section_cfg in section_configs:
        section = ReportSection(
            title=section_cfg.title,
            kind=section_cfg.type,
            description=section_cfg.description,
            dataset_name=section_cfg.dataset,
        )
        if section_cfg.type == "text":
            raw = section_cfg.text or section_cfg.description or ""
            section.text = _render_placeholders(raw, metric_map, report_date)
        elif section_cfg.type == "metrics":
            pass  # rendered from document.metrics by each exporter
        elif section_cfg.type in {"table", "chart"}:
            assert section_cfg.dataset is not None
            if section_cfg.dataset not in datasets:
                raise ConfigError(
                    f"Section '{section_cfg.title}' uses unknown dataset "
                    f"'{section_cfg.dataset}'. Known: {', '.join(sorted(datasets))}"
                )
            frame = datasets[section_cfg.dataset]
            if section_cfg.columns:
                missing = [c for c in section_cfg.columns if c not in frame.columns]
                if missing:
                    raise ConfigError(
                        f"Section '{section_cfg.title}' requests missing columns: "
                        f"{', '.join(missing)}"
                    )
                frame = frame[list(section_cfg.columns)]
            if section_cfg.type == "table":
                section.table = frame
                section.total_rows = len(frame)
                section.display_rows = section_cfg.max_rows
            else:
                assert section_cfg.chart is not None
                chart_index += 1
                chart_path = work_dir / f"chart_{chart_index}.png"
                result = render_chart(
                    frame,
                    section_cfg.chart,
                    chart_path,
                    theme_name=config.report.theme,
                    dpi=config.options.chart_dpi,
                )
                section.chart_path = result.path
                section.chart_title = result.title
                section.total_rows = result.rows_used
                if result.rows_dropped:
                    warnings.append(
                        f"Chart '{result.title}': {result.rows_dropped} row(s) with "
                        "missing values were excluded."
                    )
                if section_cfg.description:
                    section.text = _render_placeholders(
                        section_cfg.description, metric_map, report_date
                    )
        sections.append(section)

    methodology = config.report.methodology
    if methodology:
        methodology = _render_placeholders(methodology, metric_map, report_date)

    return ReportDocument(
        title=config.report.title,
        subtitle=config.report.subtitle,
        author=config.report.author,
        organization=config.report.organization,
        template=config.report.template,
        theme=config.report.theme,
        generated_at=generated_at,
        summary=summary,
        methodology=methodology,
        footer=config.report.footer,
        metrics=metrics,
        sections=sections,
        sources=descriptions,
        warnings=warnings,
        max_table_rows=config.options.max_table_rows,
    )


def run_report(
    config_path: str | Path,
    formats: list[str] | None = None,
    output_dir: str | Path | None = None,
    overwrite: bool | None = None,
) -> list[Path]:
    """Load a config, build the report, export every requested format.

    Formats are exported one at a time; if a later format fails, earlier
    completed files are preserved and the error propagates.
    """
    from reportgen.exporters import export_document  # local: avoids import cycle

    config, base_dir = load_config(config_path)
    chosen = formats or list(config.exports.formats)
    out_dir_raw = Path(output_dir) if output_dir else Path(config.exports.output_dir)
    if not out_dir_raw.is_absolute():
        out_dir_raw = base_dir / out_dir_raw
    out_dir = ensure_output_dir(out_dir_raw)
    do_overwrite = config.exports.overwrite if overwrite is None else overwrite

    now = datetime.now(UTC)
    filename = config.exports.filename.replace("{date}", now.strftime("%Y-%m-%d")).replace(
        "{datetime}", now.strftime("%Y%m%d-%H%M%S")
    )
    outputs: list[Path] = []
    with tempfile.TemporaryDirectory(prefix="reportgen-") as tmp:
        document = build_report(config, base_dir, Path(tmp))
        for fmt in chosen:
            path = export_document(document, fmt, out_dir, filename, overwrite=do_overwrite)
            outputs.append(path)
            logger.info("Wrote %s", path)
    return outputs
