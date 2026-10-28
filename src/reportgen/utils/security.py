"""Security helpers: path handling, filename sanitising, secret masking.

All external values (file paths, config values, filenames, cell values)
are treated as untrusted. These helpers are the single place where that
policy is enforced so it can be audited and tested.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from reportgen.errors import ConfigError, ExportError, SecurityError, SourceError

DEFAULT_MAX_INPUT_BYTES = 200 * 1024 * 1024  # 200 MiB
MAX_FILENAME_LENGTH = 120
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._ -]+")


def expand_env(value: str | None) -> str | None:
    """Expand ``${VAR}`` references from the environment.

    A missing variable is an error (not an empty string) so a report never
    silently runs with an empty credential or path.
    """
    if value is None:
        return None

    def _repl(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in os.environ:
            raise ConfigError(
                f"Environment variable '{name}' is referenced in the configuration "
                "but is not set. Set it or remove the reference."
            )
        return os.environ[name]

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", _repl, value)


def env_value(name: str | None) -> str | None:
    """Return the value of environment variable ``name`` or raise if unset."""
    if not name:
        return None
    if name not in os.environ:
        raise ConfigError(f"Environment variable '{name}' is not set.")
    return os.environ[name]


def resolve_input_path(raw: str, base_dir: Path) -> Path:
    """Resolve a data-source path relative to ``base_dir`` (the config dir).

    Absolute paths are allowed (users legitimately reference shared data
    folders), but the result must be an existing regular file.
    """
    if raw is None or not str(raw).strip():
        raise SourceError("A data source is missing its 'path'.")
    expanded = expand_env(str(raw)) or ""
    candidate = Path(expanded).expanduser()
    if not candidate.is_absolute():
        candidate = (base_dir / candidate).resolve()
    else:
        candidate = candidate.resolve()
    if not candidate.exists():
        raise SourceError(f"Data source file not found: {candidate}")
    if not candidate.is_file():
        raise SourceError(f"Data source path is not a file: {candidate}")
    return candidate


def check_input_size(path: Path, max_bytes: int = DEFAULT_MAX_INPUT_BYTES) -> None:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise SourceError(f"Cannot stat input file {path}: {exc}") from exc
    if size > max_bytes:
        raise SourceError(
            f"Input file {path.name} is {size / 1e6:.1f} MB, above the "
            f"{max_bytes / 1e6:.0f} MB limit. Raise 'options.max_input_mb' "
            "in the report definition if this is expected."
        )


def sanitize_filename(name: str) -> str:
    """Return a safe file stem: no separators, no traversal, bounded length."""
    if name is None or not str(name).strip():
        raise ExportError("Output filename must not be empty.")
    cleaned = str(name).strip()
    # Only the final component is kept: 'a/b' and '../x' both become a stem.
    cleaned = cleaned.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = _SAFE_FILENAME_RE.sub("-", cleaned)
    cleaned = cleaned.strip(". -_") or "report"
    # Strip a trailing known extension; exporters add the correct one.
    lowered = cleaned.lower()
    for ext in (".pdf", ".html", ".htm", ".xlsx", ".csv"):
        if lowered.endswith(ext):
            cleaned = cleaned[: -len(ext)]
            break
    return cleaned[:MAX_FILENAME_LENGTH].rstrip(". -") or "report"


def resolve_output_path(output_dir: Path, filename_stem: str, extension: str) -> Path:
    """Join output dir + sanitised stem and verify containment (anti-traversal)."""
    ext = extension if extension.startswith(".") else f".{extension}"
    stem = sanitize_filename(filename_stem)
    target_dir = output_dir.resolve()
    target = (target_dir / f"{stem}{ext}").resolve()
    if target.parent != target_dir:
        raise SecurityError(f"Refusing to write outside the output directory: {target}")
    return target


def ensure_output_dir(path: Path) -> Path:
    resolved = path.resolve()
    try:
        resolved.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ExportError(f"Cannot create output directory {resolved}: {exc}") from exc
    if not resolved.is_dir():
        raise ExportError(f"Output path is not a directory: {resolved}")
    return resolved


def mask_url(url: str | None) -> str:
    """Mask any password in a database URL for safe logging."""
    if not url:
        return "(no url)"
    try:
        parts = urlsplit(url)
        if parts.password:
            host = parts.hostname or ""
            if parts.port:
                host = f"{host}:{parts.port}"
            user = parts.username or ""
            netloc = f"{user}:***@{host}" if user else host
            return urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))
    except ValueError:
        return "(unparseable url — hidden)"
    return url


def excel_safe_value(value: object) -> object:
    """Neutralise spreadsheet formula injection.

    A text cell starting with ``= + - @`` (optionally after whitespace/tab)
    can be interpreted as a formula by Excel/Sheets. Prefix such values
    with a single quote so they are displayed literally.
    """
    if isinstance(value, str):
        stripped = value.lstrip(" \t\r\n")
        if stripped[:1] in ("=", "+", "-", "@"):
            return "'" + value
    return value


_READ_ONLY_RE = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)
_FORBIDDEN_SQL_RE = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|REPLACE|GRANT|REVOKE|"
    r"COPY|VACUUM|ATTACH|DETACH|PRAGMA|EXEC|EXECUTE|CALL)\b",
    re.IGNORECASE,
)


def validate_readonly_query(query: str) -> str:
    """Validate that a SQL string is a single read-only SELECT statement.

    This is defence-in-depth: database accounts should also be read-only.
    Parameterised values must be passed separately (SQLAlchemy bind params),
    never string-formatted into the query.
    """
    if query is None or not str(query).strip():
        raise SourceError("Database source requires a 'query' (or a 'table').")
    text = str(query).strip()
    # Remove a single trailing semicolon; any other ';' means multiple statements.
    if text.endswith(";"):
        text = text[:-1].rstrip()
    if ";" in text:
        raise SecurityError("Only a single SQL statement is allowed per source.")
    if not _READ_ONLY_RE.match(text):
        raise SecurityError("Only read-only SELECT / WITH queries are allowed.")
    if _FORBIDDEN_SQL_RE.search(text):
        raise SecurityError(
            "The query contains a forbidden keyword. Only read-only queries are allowed."
        )
    if "--" in text or "/*" in text:
        raise SecurityError("SQL comments are not allowed in report queries.")
    return text


def validate_table_name(table: str) -> str:
    """Validate a bare table name used with the ``table:`` shortcut."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", table or ""):
        raise SecurityError(f"Invalid table name: {table!r}")
    return table
