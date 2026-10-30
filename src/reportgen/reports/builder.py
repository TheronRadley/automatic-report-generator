"""Build a :class:`ReportDocument` from a validated config, and run exports.

Execution model: ``build_report`` does everything except writing
export files (loading, template defaults, pipelines, metrics, charts),
collecting visible warnings for every coercion/reduction along the
way. ``execute_report`` adds exporting with per-format status,
partial-output propagation on failure, dry-run support, and an
optional run manifest. ``run_report`` remains the simple wrapper.
"""

from __future__ import annotations

import re
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from reportgen.charts.engine import render_chart
from reportgen.config.loader import load_config
from reportgen.config.models import (
    MetricConfig,
    PipelineConfig,
    ReportConfig,
    SectionConfig,
    StepConfig,
)
from reportgen.errors import ConfigError, ExportError, PartialExportError, ProcessingError
from reportgen.logging_config import get_logger
from reportgen.processing.engine import apply_pipeline
from reportgen.processing.validation import aggregate_value, validate_frame
from reportgen.reports.model import MetricResult, ReportDocument, ReportSection
from reportgen.reports.parameters import resolve_parameters, substitute_params
from reportgen.reports.templates import get_template
from reportgen.sources.factory import load_dataset
from reportgen.utils.formatting import format_number
from reportgen.utils.security import ensure_output_dir

logger = get_logger("reportgen.reports.builder")
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass
class ExecuteResult:
    outputs: list[Path]
    manifest: dict[str, Any]
    document: ReportDocument
    output_status: list[dict[str, Any]] = field(default_factory=list)


def _render_placeholders(
    text: str,
    metrics: dict[str, MetricResult],
    document_date: str,
    parameters: dict[str, Any] | None = None,
) -> str:
    """Substitute ``{metric_name}`` / ``{param_name}`` (no eval). Metrics
    win on a name clash; both namespaces are listed on error."""
    parameters = parameters or {}

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in {"date", "datetime"}:
            return match.group(0)  # filename placeholders pass through untouched
        if key == "report_date":
            return document_date
        metric = metrics.get(key)
        if metric is not None:
            return metric.formatted
        if key in parameters:
            return str(parameters[key])
        raise ConfigError(
            f"Text references unknown metric '{{{key}}}'. Available metrics: "
            f"{', '.join(sorted(metrics)) or '(none defined)'}; parameters: "
            f"{', '.join(sorted(parameters)) or '(none)'}"
        )

    return _PLACEHOLDER_RE.sub(_replace, text)


def _load_sources(
    config: ReportConfig, base_dir: Path, resolved_params: dict[str, Any]
) -> tuple[dict[str, pd.DataFrame], list[str], list[str], dict[str, int]]:
    frames: dict[str, pd.DataFrame] = {}
    descriptions: list[str] = []
    warnings: list[str] = []
    dataset_rows: dict[str, int] = {}
    max_bytes = int(config.options.max_input_mb * 1024 * 1024)
    for source in config.sources:
        effective = source
        substituted = substitute_params(dict(source.params), resolved_params)
        if substituted != dict(source.params):
            effective = source.model_copy(update={"params": substituted})
        loaded = load_dataset(effective, base_dir, max_input_bytes=max_bytes)
        if config.options.fail_on_empty:
            validate_frame(loaded.frame, non_empty=True, context=f"Source '{source.name}'")
        frames[source.name] = loaded.frame
        dataset_rows[source.name] = len(loaded.frame)
        descriptions.append(f"{source.name}: {loaded.description} ({len(loaded.frame)} rows)")
        warnings.extend(loaded.warnings)
        logger.info("Loaded %s: %s rows", source.name, len(loaded.frame))
    return frames, descriptions, warnings, dataset_rows


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


def _substitute_pipelines(
    pipelines: list[PipelineConfig], resolved_params: dict[str, Any]
) -> list[PipelineConfig]:
    """Apply ${params.x} substitution inside every step's parameters."""
    if not resolved_params:
        return pipelines
    substituted: list[PipelineConfig] = []
    for pipeline in pipelines:
        steps = [
            StepConfig.model_validate(
                {"op": step.op, **substitute_params(step.params, resolved_params)}
            )
            for step in pipeline.steps
        ]
        substituted.append(pipeline.model_copy(update={"steps": steps}))
    return substituted


def _auto_summary(document_metrics: list[MetricResult], sources: list[str]) -> str:
    parts = [f"This report is based on {len(sources)} data source(s): {'; '.join(sources)}."]
    if document_metrics:
        figures = "; ".join(f"{m.label}: {m.formatted}" for m in document_metrics)
        parts.append(f"Key figures — {figures}.")
    return " ".join(parts)


def build_report(
    config: ReportConfig,
    base_dir: Path,
    work_dir: Path,
    parameter_overrides: dict[str, str] | None = None,
) -> ReportDocument:
    """Execute loading, processing, metrics, charts — everything except export."""
    resolved_params = resolve_parameters(
        config.parameters, config.report.timezone, parameter_overrides
    )
    timezone = ZoneInfo(config.report.timezone)
    frames, descriptions, warnings, dataset_rows = _load_sources(config, base_dir, resolved_params)
    pipelines, metric_configs, section_configs = _with_template_defaults(config, frames)
    pipelines = _substitute_pipelines(pipelines, resolved_params)

    datasets: dict[str, pd.DataFrame] = dict(frames)
    for pipeline in pipelines:
        if pipeline.dataset not in datasets:
            raise ProcessingError(
                f"Processing pipeline references unknown dataset '{pipeline.dataset}'. "
                f"Known: {', '.join(sorted(datasets))}"
            )
        output = pipeline.output or pipeline.dataset
        datasets[output] = apply_pipeline(
            datasets[pipeline.dataset], pipeline.steps, datasets, notes=warnings
        )
        dataset_rows[output] = len(datasets[output])
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
                formatted=format_number(
                    value,
                    metric_cfg.format,
                    metric_cfg.decimals,
                    currency=config.report.currency,
                ),
                number_format=metric_cfg.format,
                dataset=metric_cfg.dataset,
                decimals=metric_cfg.decimals,
            )
        )
    metric_map = {m.name: m for m in metrics}

    generated_at = datetime.now(timezone)
    report_date = generated_at.strftime("%Y-%m-%d")
    summary_text = config.report.summary
    if summary_text:
        summary = _render_placeholders(summary_text, metric_map, report_date, resolved_params)
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
            section.text = _render_placeholders(raw, metric_map, report_date, resolved_params)
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
                warnings.extend(result.notes)
                if section_cfg.description:
                    section.text = _render_placeholders(
                        section_cfg.description, metric_map, report_date, resolved_params
                    )
        sections.append(section)

    methodology = config.report.methodology
    if methodology:
        methodology = _render_placeholders(methodology, metric_map, report_date, resolved_params)

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
        currency=config.report.currency,
        timezone_name=config.report.timezone,
        dataset_rows=dataset_rows,
        parameters={k: v for k, v in resolved_params.items()},
    )


def execute_report(
    config_path: str | Path,
    formats: list[str] | None = None,
    output_dir: str | Path | None = None,
    overwrite: bool | None = None,
    parameters: dict[str, str] | None = None,
    dry_run: bool = False,
    manifest_path: str | Path | None = None,
) -> ExecuteResult:
    """Build and (unless dry-run) export a report, tracking per-format
    status. On a mid-list export failure the raised
    :class:`PartialExportError` carries the files already created, and
    the manifest (if requested) is still written."""
    from reportgen.exporters import export_document  # local: avoids import cycle
    from reportgen.exporters.base import _EXTENSIONS
    from reportgen.reports.manifest import build_manifest, write_manifest
    from reportgen.utils.security import resolve_output_path

    started = time.perf_counter()
    started_at = datetime.now(UTC).isoformat()
    config, base_dir = load_config(config_path)
    chosen = formats or list(config.exports.formats)
    out_dir_raw = Path(output_dir) if output_dir else Path(config.exports.output_dir)
    if not out_dir_raw.is_absolute():
        out_dir_raw = base_dir / out_dir_raw
    out_dir = ensure_output_dir(out_dir_raw) if not dry_run else out_dir_raw.resolve()
    do_overwrite = config.exports.overwrite if overwrite is None else overwrite

    resolved = resolve_parameters(config.parameters, config.report.timezone, parameters)
    now = datetime.now(ZoneInfo(config.report.timezone))
    filename = config.exports.filename.replace("{date}", now.strftime("%Y-%m-%d")).replace(
        "{datetime}", now.strftime("%Y%m%d-%H%M%S")
    )
    filename = str(substitute_params(filename, resolved))

    outputs: list[Path] = []
    output_status: list[dict[str, Any]] = []
    manifest_target = Path(manifest_path) if manifest_path else None
    if manifest_target is not None and not manifest_target.is_absolute():
        manifest_target = (Path.cwd() / manifest_target).resolve()
    document: ReportDocument | None = None
    failure: Exception | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="reportgen-") as tmp:
            document = build_report(config, base_dir, Path(tmp), parameter_overrides=parameters)
            for fmt in chosen:
                fmt_key = fmt.lower().lstrip(".")
                status: dict[str, Any] = {"format": fmt_key, "path": None, "status": "pending"}
                output_status.append(status)
                if dry_run:
                    extension = _EXTENSIONS.get(fmt_key, f".{fmt_key}")
                    would_be = resolve_output_path(out_dir, filename, extension)
                    status.update(path=str(would_be), status="dry_run")
                    continue
                try:
                    path = export_document(
                        document, fmt_key, out_dir, filename, overwrite=do_overwrite
                    )
                except ExportError as exc:
                    status.update(status="failed", error=str(exc))
                    raise PartialExportError(
                        f"{exc} ({len(outputs)} file(s) were already created and have been kept.)",
                        outputs=list(outputs),
                    ) from exc
                outputs.append(path)
                status.update(path=str(path), status="created")
                logger.info("Wrote %s", path)
    except Exception as exc:
        failure = exc

    manifest = build_manifest(
        config_path=Path(config_path),
        config=config,
        base_dir=base_dir,
        document=document,
        formats=list(chosen),
        output_dir=out_dir,
        filename=filename,
        overwrite=do_overwrite,
        dry_run=dry_run,
        output_status=output_status,
        started_at=started_at,
        duration_seconds=time.perf_counter() - started,
        error=str(failure) if failure else None,
    )
    if manifest_target is not None:
        try:
            write_manifest(manifest_target, manifest)
        except OSError as exc:
            logger.warning("Could not write manifest to %s: %s", manifest_target, exc)
            if failure is None:
                raise ExportError(
                    f"Report generated but the manifest could not be written "
                    f"to {manifest_target}: {exc}"
                ) from exc
    if failure is not None:
        raise failure
    assert document is not None
    return ExecuteResult(
        outputs=outputs, manifest=manifest, document=document, output_status=output_status
    )


def run_report(
    config_path: str | Path,
    formats: list[str] | None = None,
    output_dir: str | Path | None = None,
    overwrite: bool | None = None,
    parameters: dict[str, str] | None = None,
) -> list[Path]:
    """Load a config, build the report, export every requested format.

    Formats are exported one at a time; if a later format fails, earlier
    completed files are preserved and are listed on the raised
    :class:`PartialExportError`."""
    result = execute_report(
        config_path,
        formats=formats,
        output_dir=output_dir,
        overwrite=overwrite,
        parameters=parameters,
    )
    return result.outputs
