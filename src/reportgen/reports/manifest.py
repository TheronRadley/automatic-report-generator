"""Run manifest: a machine-readable record of exactly what produced a
report — config/input fingerprints, effective settings, row counts,
warnings, and per-output status. Written on request (``--manifest``)
for real runs, dry runs, and even failed runs (with partial output
status), so scheduled reporting is auditable after the fact."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from reportgen import __version__
from reportgen.config.models import ReportConfig
from reportgen.reports.model import ReportDocument
from reportgen.utils.security import mask_url, resolve_input_path

_CHUNK = 1024 * 1024


def file_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _text_fingerprint(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def source_fingerprints(config: ReportConfig, base_dir: Path) -> dict[str, str | None]:
    """Per-source fingerprints. Parameters are hashed, never stored."""
    fingerprints: dict[str, str | None] = {}
    for source in config.sources:
        try:
            if source.type in {"csv", "tsv", "excel", "json", "jsonl"} and source.path:
                fingerprints[source.name] = file_fingerprint(
                    resolve_input_path(source.path, base_dir)
                )
            elif source.type == "sqlite" and source.path:
                payload = file_fingerprint(resolve_input_path(source.path, base_dir))
                query_hash = _text_fingerprint(
                    json.dumps(
                        {"q": source.query or source.table, "p": source.params},
                        sort_keys=True,
                        default=str,
                    )
                )
                fingerprints[source.name] = f"{payload}+{query_hash}"
            else:
                location = mask_url(source.url) if source.url else f"env:{source.url_env}"
                fingerprints[source.name] = _text_fingerprint(
                    json.dumps(
                        {
                            "loc": location,
                            "q": source.query or source.table,
                            "p": source.params,
                        },
                        sort_keys=True,
                        default=str,
                    )
                )
        except Exception:
            fingerprints[source.name] = None  # manifest must never break a run
    return fingerprints


def build_manifest(
    *,
    config_path: Path,
    config: ReportConfig,
    base_dir: Path,
    document: ReportDocument | None,
    formats: list[str],
    output_dir: Path,
    filename: str,
    overwrite: bool,
    dry_run: bool,
    output_status: list[dict[str, Any]],
    started_at: str,
    duration_seconds: float,
    error: str | None = None,
) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "reportgen_version": __version__,
        "started_at": started_at,
        "duration_seconds": round(duration_seconds, 3),
        "dry_run": dry_run,
        "status": "failed" if error else ("dry_run" if dry_run else "success"),
        "error": error,
        "config": {
            "path": str(config_path),
            "fingerprint": (file_fingerprint(config_path) if Path(config_path).exists() else None),
            "schema_version": config.version,
        },
        "effective": {
            "title": config.report.title,
            "template": config.report.template,
            "theme": config.report.theme,
            "currency": config.report.currency,
            "timezone": config.report.timezone,
            "parameters": (document.parameters if document else {}),
            "formats": formats,
            "output_dir": str(output_dir),
            "filename": filename,
            "overwrite": overwrite,
            "options": config.options.model_dump(),
        },
        "inputs": [],
        "datasets": (document.dataset_rows if document else {}),
        "metrics": [],
        "warnings": (document.warnings if document else []),
        "outputs": output_status,
    }
    fingerprints = source_fingerprints(config, base_dir)
    descriptions = document.sources if document else []
    for index, source in enumerate(config.sources):
        manifest["inputs"].append(
            {
                "name": source.name,
                "type": source.type,
                "description": descriptions[index] if index < len(descriptions) else None,
                "rows": (document.dataset_rows.get(source.name) if document else None),
                "fingerprint": fingerprints.get(source.name),
            }
        )
    if document:
        manifest["metrics"] = [
            {
                "name": m.name,
                "label": m.label,
                "dataset": m.dataset,
                "value": m.value,
                "formatted": m.formatted,
            }
            for m in document.metrics
        ]
    return manifest


def write_manifest(path: Path, manifest: dict[str, Any]) -> Path:
    """Atomically write the manifest JSON (temp file + replace)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.stem}-", suffix=".json.tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(manifest, indent=2, default=str) + "\n")
        os.replace(tmp_name, target)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return target
