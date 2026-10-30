"""PDF exporter built on ReportLab Platypus.

Unicode: ReportLab's built-in Helvetica only covers Latin-1, so we
register DejaVu Sans (freely licensed, shipped with Matplotlib) when
available and fall back to Helvetica otherwise. All user text is
XML-escaped before it reaches ReportLab's Paragraph parser, so report
content can never inject markup.
"""

from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Image,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from reportgen.errors import ExportError
from reportgen.reports.model import ReportDocument
from reportgen.utils.formatting import format_cell

_ACCENT = colors.HexColor("#2f4b7c")
_MUTED = colors.HexColor("#5a5a5a")
_RULE = colors.HexColor("#d8d8d8")
_FONTS_REGISTERED = False


def _register_fonts() -> tuple[str, str]:
    """Return (regular, bold) font names, preferring DejaVu Sans."""
    global _FONTS_REGISTERED
    if _FONTS_REGISTERED:
        return "DejaVuSans", "DejaVuSans-Bold"
    try:
        import matplotlib

        font_dir = Path(matplotlib.get_data_path()) / "fonts" / "ttf"
        regular = font_dir / "DejaVuSans.ttf"
        bold = font_dir / "DejaVuSans-Bold.ttf"
        if regular.exists() and bold.exists():
            pdfmetrics.registerFont(TTFont("DejaVuSans", str(regular)))
            pdfmetrics.registerFont(TTFont("DejaVuSans-Bold", str(bold)))
            pdfmetrics.registerFontFamily("DejaVuSans", normal="DejaVuSans", bold="DejaVuSans-Bold")
            _FONTS_REGISTERED = True
            return "DejaVuSans", "DejaVuSans-Bold"
    except Exception:  # fall back rather than fail the whole report
        pass
    return "Helvetica", "Helvetica-Bold"


def _styles(regular: str, bold: str) -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "rg-title",
            parent=base["Title"],
            fontName=bold,
            fontSize=22,
            leading=26,
            alignment=0,
            spaceAfter=4,
        ),
        "subtitle": ParagraphStyle(
            "rg-subtitle",
            parent=base["Normal"],
            fontName=regular,
            fontSize=11,
            leading=14,
            textColor=_MUTED,
            spaceAfter=8,
        ),
        "h2": ParagraphStyle(
            "rg-h2",
            parent=base["Heading2"],
            fontName=bold,
            fontSize=12.5,
            leading=16,
            spaceBefore=14,
            spaceAfter=6,
            textColor=colors.black,
            keepWithNext=1,  # never strand a heading at the foot of a page
        ),
        "body": ParagraphStyle(
            "rg-body",
            parent=base["Normal"],
            fontName=regular,
            fontSize=9.5,
            leading=13.5,
            spaceAfter=6,
        ),
        "small": ParagraphStyle(
            "rg-small",
            parent=base["Normal"],
            fontName=regular,
            fontSize=8,
            leading=10.5,
            textColor=_MUTED,
        ),
        "cell": ParagraphStyle(
            "rg-cell", parent=base["Normal"], fontName=regular, fontSize=7.8, leading=9.5
        ),
        "cellhead": ParagraphStyle(
            "rg-cellhead",
            parent=base["Normal"],
            fontName=bold,
            fontSize=7.8,
            leading=9.5,
            textColor=colors.white,
        ),
        "metriclabel": ParagraphStyle(
            "rg-mlabel",
            parent=base["Normal"],
            fontName=regular,
            fontSize=7.5,
            leading=9,
            textColor=_MUTED,
        ),
        "metricvalue": ParagraphStyle(
            "rg-mvalue", parent=base["Normal"], fontName=bold, fontSize=13, leading=16
        ),
    }


def _p(text: str, style: ParagraphStyle) -> Paragraph:
    return Paragraph(escape(str(text)), style)


def _metrics_table(document: ReportDocument, styles: dict, width: float) -> Table:
    per_row = 3
    cells: list = []
    for metric in document.metrics:
        cells.append(
            [
                _p(metric.label.upper(), styles["metriclabel"]),
                _p(metric.formatted, styles["metricvalue"]),
            ]
        )
    rows = []
    for start in range(0, len(cells), per_row):
        chunk = cells[start : start + per_row]
        labels = [c[0] for c in chunk] + [""] * (per_row - len(chunk))
        values = [c[1] for c in chunk] + [""] * (per_row - len(chunk))
        rows.extend([labels, values])
    table = Table(rows, colWidths=[width / per_row] * per_row)
    table.setStyle(
        TableStyle(
            [
                ("LINEABOVE", (0, 0), (-1, -1), 0.6, _RULE),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return table


_MAX_CELL_CHARS = 500


def _pdf_cell(value, style) -> tuple[Paragraph, bool]:
    """Format one cell, truncating very long text for the PDF display.

    A table row cannot split across pages in ReportLab, so an untruncated
    multi-thousand-character cell would make the whole export fail with
    'flowable too large'. The Excel export keeps full values; truncation
    here is flagged to the caller so it can be disclosed."""
    text = format_cell(value)
    truncated = len(text) > _MAX_CELL_CHARS
    if truncated:
        text = text[:_MAX_CELL_CHARS].rstrip() + " …"
    return _p(text, style), truncated


def _data_table(frame, styles: dict, width: float, max_rows: int) -> tuple[Table, bool]:
    shown = frame.head(max_rows)
    headers = [_p(str(col), styles["cellhead"]) for col in frame.columns]
    body = []
    any_truncated = False
    for row in shown.itertuples(index=False):
        cells = []
        for value in row:
            paragraph, truncated = _pdf_cell(value, styles["cell"])
            any_truncated = any_truncated or truncated
            cells.append(paragraph)
        body.append(cells)
    col_width = width / max(1, len(frame.columns))
    table = Table([headers, *body], colWidths=[col_width] * len(frame.columns), repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), _ACCENT),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7f7f7")]),
                ("GRID", (0, 0), (-1, -1), 0.4, _RULE),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return table, any_truncated


def write_pdf(document: ReportDocument, path: Path) -> None:
    regular, bold = _register_fonts()
    styles = _styles(regular, bold)
    page_w, page_h = A4
    usable_width = page_w - 36 * mm
    # A chart must fit within the text frame in BOTH dimensions or
    # ReportLab refuses the flowable. Leave room for the heading.
    max_image_height = page_h - 38 * mm - 24

    def _header_footer(canvas, doc) -> None:
        canvas.saveState()
        canvas.setFont(regular, 7.5)
        canvas.setFillColor(_MUTED)
        header_text = document.organization or document.title
        canvas.drawString(18 * mm, A4[1] - 12 * mm, str(header_text)[:90])
        canvas.drawRightString(A4[0] - 18 * mm, A4[1] - 12 * mm, document.generated_label)
        canvas.setStrokeColor(_RULE)
        canvas.line(18 * mm, A4[1] - 14 * mm, A4[0] - 18 * mm, A4[1] - 14 * mm)
        footer_text = document.footer or document.title
        canvas.drawString(18 * mm, 12 * mm, str(footer_text)[:90])
        canvas.drawRightString(A4[0] - 18 * mm, 12 * mm, f"Page {doc.page}")
        canvas.restoreState()

    story: list = []
    story.append(_p(document.title, styles["title"]))
    if document.subtitle:
        story.append(_p(document.subtitle, styles["subtitle"]))
    meta_bits = [f"Generated {document.generated_label}"]
    if document.author:
        meta_bits.append(document.author)
    if document.organization:
        meta_bits.append(document.organization)
    story.append(_p(" · ".join(meta_bits), styles["small"]))
    story.append(Spacer(1, 6))

    story.append(_p("Executive summary", styles["h2"]))
    story.append(_p(document.summary, styles["body"]))

    long_report = len(document.sections) >= 5
    for index, section in enumerate(document.sections):
        if long_report and index > 0 and section.kind in {"chart", "table"}:
            # Keep charts/tables from being stranded at a page foot.
            story.append(Spacer(1, 2))
        story.append(_p(section.title, styles["h2"]))
        if section.kind == "metrics":
            if document.metrics:
                story.append(_metrics_table(document, styles, usable_width))
            else:
                story.append(_p("No metrics were defined for this report.", styles["body"]))
        elif section.kind == "text":
            story.append(_p(section.text or "", styles["body"]))
        elif section.kind == "chart":
            if section.text:
                story.append(_p(section.text, styles["body"]))
            elif section.description:
                story.append(_p(section.description, styles["body"]))
            if section.chart_path and section.chart_path.exists():
                image = Image(str(section.chart_path))
                ratio = min(
                    usable_width / image.imageWidth,
                    max_image_height / image.imageHeight,
                )
                image.drawWidth = image.imageWidth * ratio
                image.drawHeight = image.imageHeight * ratio
                story.append(image)
            else:
                story.append(_p("(Chart image unavailable.)", styles["small"]))
        elif section.kind == "table":
            if section.description:
                story.append(_p(section.description, styles["body"]))
            if section.table is not None and not section.table.empty:
                cap = section.display_cap(document.max_table_rows)
                shown = min(cap, len(section.table))
                table, truncated_cells = _data_table(section.table, styles, usable_width, cap)
                story.append(table)
                if truncated_cells:
                    story.append(
                        _p(
                            "Long cell values are truncated in this PDF; "
                            "the Excel export contains full values.",
                            styles["small"],
                        )
                    )
                if len(section.table) > shown:
                    story.append(
                        _p(
                            f"Showing {shown} of {len(section.table)} rows. "
                            "The Excel export contains the full table.",
                            styles["small"],
                        )
                    )
            else:
                story.append(_p("(No rows.)", styles["small"]))

    if document.methodology:
        story.append(_p("Methodology", styles["h2"]))
        story.append(_p(document.methodology, styles["body"]))

    story.append(PageBreak())
    story.append(_p("Sources and reproducibility", styles["h2"]))
    for source in document.sources:
        story.append(_p(f"• {source}", styles["small"]))
    if document.warnings:
        story.append(_p("Warnings", styles["h2"]))
        for warning in document.warnings:
            story.append(_p(f"• {warning}", styles["small"]))
    story.append(Spacer(1, 6))
    story.append(
        _p(
            "Generated by Automatic Report Generator. Re-run the same report "
            "definition against the same inputs to reproduce this report.",
            styles["small"],
        )
    )

    try:
        doc = SimpleDocTemplate(
            str(path),
            pagesize=A4,
            leftMargin=18 * mm,
            rightMargin=18 * mm,
            topMargin=20 * mm,
            bottomMargin=18 * mm,
            title=document.title,
            author=document.author or "Automatic Report Generator",
        )
        doc.build(story, onFirstPage=_header_footer, onLaterPages=_header_footer)
    except Exception as exc:
        raise ExportError(f"PDF rendering failed: {exc}") from exc
