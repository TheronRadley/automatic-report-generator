"""Safe publication: write to a temp file, then atomically replace.

Guarantees:
- an existing report is never overwritten unless ``overwrite=True``;
- a failed export never leaves a partial file at the final path;
- the final path always stays inside the chosen output directory.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from reportgen.errors import ExportError
from reportgen.reports.model import ReportDocument
from reportgen.utils.security import resolve_output_path

FORMATS = ("pdf", "html", "xlsx")
_EXTENSIONS = {"pdf": ".pdf", "html": ".html", "xlsx": ".xlsx"}


def export_document(
    document: ReportDocument,
    fmt: str,
    output_dir: Path,
    filename_stem: str,
    overwrite: bool = False,
) -> Path:
    fmt = fmt.lower().lstrip(".")
    if fmt not in FORMATS:
        raise ExportError(f"Unsupported export format '{fmt}'. Use pdf, html, or xlsx.")
    target = resolve_output_path(output_dir, filename_stem, _EXTENSIONS[fmt])
    if target.exists() and not overwrite:
        raise ExportError(
            f"Output file already exists: {target}. "
            "Re-run with --overwrite or choose a different filename/output_dir."
        )
    writers = _writers()
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.stem}-", suffix=_EXTENSIONS[fmt], dir=str(target.parent)
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        writers[fmt](document, tmp_path)
        if not tmp_path.exists() or tmp_path.stat().st_size == 0:
            raise ExportError(f"The {fmt} exporter produced no output.")
        # mkstemp creates 0600 files; reports are meant to be shared, so
        # publish with conventional 0644 permissions (no-op-ish on Windows).
        try:
            os.chmod(tmp_path, 0o644)
        except OSError:
            pass
        if overwrite:
            os.replace(tmp_path, target)  # atomic on POSIX and Windows (same dir)
        else:
            # Atomic no-clobber publish: hardlink fails if another run won
            # the race, so an existing report can never be replaced here.
            try:
                os.link(tmp_path, target)
            except FileExistsError as exc:
                raise ExportError(
                    f"Output file already exists: {target}. "
                    "Re-run with --overwrite or choose a different filename/output_dir."
                ) from exc
            tmp_path.unlink(missing_ok=True)
    except ExportError:
        tmp_path.unlink(missing_ok=True)
        raise
    except OSError as exc:
        tmp_path.unlink(missing_ok=True)
        raise ExportError(f"Cannot write report to {target}: {exc}") from exc
    except Exception as exc:
        tmp_path.unlink(missing_ok=True)
        raise ExportError(f"Failed to generate {fmt.upper()} report: {exc}") from exc
    return target


def _writers() -> dict:
    from reportgen.exporters.excel_exporter import write_excel
    from reportgen.exporters.html_exporter import write_html
    from reportgen.exporters.pdf_exporter import write_pdf

    return {"pdf": write_pdf, "html": write_html, "xlsx": write_excel}
