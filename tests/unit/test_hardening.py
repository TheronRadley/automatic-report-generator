"""Hardening regression tests: Unicode PDF, expression DoS guard,
unreachable PostgreSQL handling, published-file permissions."""

from __future__ import annotations

import os
import stat
import sys

import pandas as pd
import pytest

from reportgen.errors import ProcessingError, SourceError
from reportgen.processing.engine import apply_step
from reportgen.reports.builder import run_report


def test_huge_scalar_exponent_refused(sales_frame):
    with pytest.raises(ProcessingError, match="exponent"):
        apply_step(sales_frame, {"op": "calculate", "column": "x", "expression": "9 ** 9 ** 9"})


def test_unicode_pdf_and_permissions(write_config, sales_csv, tmp_path):
    config = write_config(f"""
version: 1
report:
  title: "Rapport des ventes — café €"
  subtitle: "Données synthétiques · résumé"
  template: general
sources:
  - {{name: d, type: csv, path: "{sales_csv}"}}
exports:
  output_dir: "{tmp_path / "o"}"
  filename: unicode
  overwrite: true
""")
    outputs = run_report(config, formats=["pdf", "html"])
    pdf = next(p for p in outputs if p.suffix == ".pdf")
    assert pdf.read_bytes().startswith(b"%PDF")
    assert pdf.stat().st_size > 3000
    html = next(p for p in outputs if p.suffix == ".html").read_text(encoding="utf-8")
    assert "café €" in html
    if sys.platform != "win32":
        mode = stat.S_IMODE(os.stat(pdf).st_mode)
        assert mode == 0o644


def test_unreachable_postgres_fails_cleanly(monkeypatch, tmp_path):
    pytest.importorskip("psycopg")
    from reportgen.config.models import SourceConfig
    from reportgen.sources.factory import load_dataset

    monkeypatch.setenv("RG_PG_UNREACHABLE", "postgresql://user:pw@127.0.0.1:1/dbname")
    cfg = SourceConfig(
        name="pg",
        type="postgres",
        url_env="RG_PG_UNREACHABLE",
        query="SELECT 1 AS one",
        options={"timeout_seconds": 2},
    )
    with pytest.raises(SourceError, match="Database query failed|Cannot create"):
        load_dataset(cfg, tmp_path)


def test_describe_on_text_only_frame_errors():
    frame = pd.DataFrame({"name": ["a", "b"]})
    with pytest.raises(ProcessingError, match="no numeric"):
        apply_step(frame, {"op": "describe"})
