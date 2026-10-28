"""HTML exporter: sandboxed Jinja2, autoescaped, charts embedded as
base64 data URIs so the file is fully self-contained (no CDN)."""

from __future__ import annotations

import base64
from pathlib import Path

from jinja2.sandbox import SandboxedEnvironment

from reportgen.errors import ExportError
from reportgen.reports.model import ReportDocument
from reportgen.utils.formatting import format_cell

_TEMPLATE_DIR = Path(__file__).parent / "templates"


def _chart_data_uri(path: Path | None) -> str:
    if path is None or not path.exists():
        return ""
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _section_context(section, max_rows: int) -> dict:
    context = {
        "title": section.title,
        "kind": section.kind,
        "text": section.text,
        "description": section.description,
        "chart_title": section.chart_title or "",
        "chart_data_uri": _chart_data_uri(section.chart_path),
        "columns": [],
        "rows": [],
        "truncated": False,
        "shown_rows": 0,
        "total_rows": section.total_rows,
    }
    if section.table is not None:
        frame = section.table
        shown = frame.head(section.display_cap(max_rows))
        context["columns"] = [str(c) for c in frame.columns]
        context["rows"] = [
            [format_cell(value) for value in row] for row in shown.itertuples(index=False)
        ]
        context["shown_rows"] = len(shown)
        context["total_rows"] = len(frame)
        context["truncated"] = len(frame) > len(shown)
    return context


def write_html(document: ReportDocument, path: Path) -> None:
    # Belt & braces: template text is read from disk but every user value
    # is passed as data and autoescaped; the sandbox blocks attribute
    # access tricks even in a hypothetical customised template.
    from jinja2 import FileSystemLoader, select_autoescape

    env = SandboxedEnvironment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "j2"]),
    )
    try:
        template = env.get_template("report.html.j2")
        html = template.render(
            title=document.title,
            subtitle=document.subtitle,
            author=document.author,
            organization=document.organization,
            template=document.template,
            generated_at=document.generated_label,
            summary=document.summary,
            methodology=document.methodology,
            footer=document.footer,
            metrics=[{"label": m.label, "formatted": m.formatted} for m in document.metrics],
            sections=[_section_context(s, document.max_table_rows) for s in document.sections],
            sources=document.sources,
            warnings=document.warnings,
        )
    except Exception as exc:
        raise ExportError(f"HTML rendering failed: {exc}") from exc
    path.write_text(html, encoding="utf-8")
