"""Security-focused tests: paths, SQL, filenames, formula injection."""

from __future__ import annotations

import pytest

from reportgen.errors import ConfigError, ExportError, SecurityError, SourceError
from reportgen.utils.security import (
    excel_safe_value,
    expand_env,
    mask_url,
    resolve_output_path,
    sanitize_filename,
    validate_readonly_query,
    validate_table_name,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("report", "report"),
        ("../../etc/passwd", "passwd"),
        ("a/b/c", "c"),
        ("..\\..\\evil.pdf", "evil"),
        ("my report!.html", "my report"),
        ("...", "report"),
        ("sales-2025-01", "sales-2025-01"),
    ],
)
def test_sanitize_filename(raw, expected):
    assert sanitize_filename(raw) == expected


def test_sanitize_empty_filename():
    with pytest.raises(ExportError):
        sanitize_filename("  ")


def test_output_path_stays_inside(tmp_path):
    target = resolve_output_path(tmp_path, "../escape", ".pdf")
    assert target.parent == tmp_path.resolve()
    assert target.name == "escape.pdf"


@pytest.mark.parametrize(
    "query",
    [
        "SELECT * FROM sales",
        "select region, sum(revenue) from sales group by region;",
        "WITH t AS (SELECT 1 AS x) SELECT * FROM t",
    ],
)
def test_readonly_queries_allowed(query):
    assert validate_readonly_query(query)


@pytest.mark.parametrize(
    "query",
    [
        "DELETE FROM sales",
        "SELECT * FROM sales; DROP TABLE sales",
        "UPDATE sales SET revenue = 0",
        "SELECT * FROM sales -- comment",
        "INSERT INTO sales VALUES (1)",
        "PRAGMA writable_schema=1",
        "",
    ],
)
def test_write_queries_rejected(query):
    with pytest.raises((SecurityError, SourceError)):
        validate_readonly_query(query)


def test_table_name_validation():
    assert validate_table_name("sales_2025") == "sales_2025"
    with pytest.raises(SecurityError):
        validate_table_name("sales; DROP TABLE users")


@pytest.mark.parametrize("value", ["=SUM(A1:A9)", "+2+2", "-2+2", "@SUM(1)", "  =cmd"])
def test_excel_formula_injection_neutralised(value):
    assert str(excel_safe_value(value)).startswith("'")


def test_excel_normal_values_untouched():
    assert excel_safe_value("North") == "North"
    assert excel_safe_value(42) == 42


def test_mask_url_hides_password():
    masked = mask_url("postgresql://user:s3cret@db.example.com:5432/app")
    assert "s3cret" not in masked
    assert "***" in masked


def test_mask_url_redacts_query_credentials():
    masked = mask_url(
        "postgresql://db.example.com/app?user=reader&password=q-secret"
        "&sslpassword=tls-secret&application_name=reports&api_key=k-secret"
    )
    for secret in ("q-secret", "tls-secret", "k-secret"):
        assert secret not in masked
    assert "application_name=reports" in masked
    assert "user=reader" in masked


def test_expand_env(monkeypatch):
    monkeypatch.setenv("RG_TEST_SECRET", "value1")
    assert expand_env("prefix-${RG_TEST_SECRET}") == "prefix-value1"
    with pytest.raises(ConfigError):
        expand_env("${RG_DEFINITELY_MISSING_VAR}")
