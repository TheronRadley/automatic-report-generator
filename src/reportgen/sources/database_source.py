"""Database adapter (SQLite + PostgreSQL) via SQLAlchemy.

Access is read-only by construction:

- queries are validated as single SELECT statements and values are
  always bound parameters (never string interpolation);
- SQLite files are always opened with ``mode=ro`` — including when the
  connection comes from a URL — and a missing file is an error rather
  than an invitation to create a database;
- PostgreSQL sessions request a read-only transaction with a statement
  timeout;
- arbitrary SQLAlchemy dialects are *not* supported: only sqlite and
  postgresql URLs are accepted, because read-only/limits could not be
  enforced equivalently for unknown drivers.

Credentials come from environment variables. SQLAlchemy is created
with ``hide_parameters=True`` and driver errors are additionally
scrubbed of bound values and URL credentials before they can reach a
log line, error message, or report source label (all of which use the
query-redacting ``mask_url``).
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qsl

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from reportgen.config.models import SourceConfig
from reportgen.errors import SecurityError, SourceError
from reportgen.logging_config import get_logger
from reportgen.sources.base import LoadedDataset, finalise_frame
from reportgen.utils.security import (
    env_value,
    expand_env,
    mask_url,
    resolve_input_path,
    scrub_sensitive,
    validate_readonly_query,
    validate_table_name,
)

logger = get_logger("reportgen.sources.database")

_ALLOWED_DRIVERS = {"sqlite", "postgresql", "postgresql+psycopg", "postgres"}


def _raw_url(cfg: SourceConfig) -> str | None:
    if cfg.url_env:
        return env_value(cfg.url_env)
    if cfg.url:
        return expand_env(cfg.url)
    return None


def _sqlite_ro_url(db_path: Path) -> str:
    # mode=ro: the file cannot be created or modified even if a query
    # somehow slipped past validation.
    return f"sqlite:///file:{db_path}?mode=ro&uri=true"


def _sqlite_url_from_url(raw: str, base_dir: Path, source_name: str) -> str:
    """Normalise a user-supplied sqlite URL to a read-only file URL."""
    url = make_url(raw)
    database = url.database or ""
    if database.startswith("file:"):
        database = database[5:]
    if database in {"", ":memory:"}:
        raise SourceError(
            f"SQLite source '{source_name}' uses an in-memory/empty database URL, "
            "which cannot contain report data. Point it at an existing file."
        )
    raw_query = url.query
    query = (
        dict(raw_query)  # SQLAlchemy 2.x exposes an immutabledict
        if hasattr(raw_query, "items")
        else dict(parse_qsl(str(raw_query or "")))
    )
    mode = query.get("mode")
    if mode is not None and mode != "ro":
        raise SecurityError(
            f"SQLite source '{source_name}' requests mode={mode}; "
            "reportgen only opens SQLite databases read-only (mode=ro)."
        )
    db_path = Path(database)
    if not db_path.is_absolute():
        db_path = (base_dir / db_path).resolve()
    if not db_path.exists():
        raise SourceError(
            f"SQLite database not found: {db_path}. Read-only access never "
            "creates a missing database."
        )
    if not db_path.is_file():
        raise SourceError(f"SQLite database path is not a file: {db_path}")
    return _sqlite_ro_url(db_path)


def _database_url(cfg: SourceConfig, base_dir: Path) -> str:
    raw = _raw_url(cfg)
    if cfg.type == "sqlite" and raw is None:
        assert cfg.path is not None  # enforced by the config model
        return _sqlite_ro_url(resolve_input_path(cfg.path, base_dir))
    if raw is None and cfg.path:  # sqlite file given via path on any db type
        return _sqlite_ro_url(resolve_input_path(cfg.path, base_dir))
    if raw is None:
        raise SourceError(f"Database source '{cfg.name}' has no connection URL.")
    url = make_url(raw)
    driver = url.drivername
    if driver not in _ALLOWED_DRIVERS:
        raise SecurityError(
            f"Unsupported database driver '{driver}' for source '{cfg.name}'. "
            "Only SQLite and PostgreSQL are supported, because read-only "
            "enforcement cannot be guaranteed for arbitrary dialects. "
            "Use type: sqlite or type: postgres."
        )
    if cfg.type == "postgres" and not driver.startswith(("postgresql", "postgres")):
        raise SourceError(f"Source '{cfg.name}' is type postgres but its URL driver is '{driver}'.")
    if cfg.type == "sqlite" and driver != "sqlite":
        raise SourceError(f"Source '{cfg.name}' is type sqlite but its URL driver is '{driver}'.")
    if driver == "sqlite":
        return _sqlite_url_from_url(raw, base_dir, cfg.name)
    # Normalise bare postgres URLs onto the psycopg (v3) driver when present.
    if driver in {"postgres", "postgresql"}:
        try:
            import psycopg  # noqa: F401

            url = url.set(drivername="postgresql+psycopg")
        except ImportError:
            pass  # SQLAlchemy will raise an actionable error below.
    return url.render_as_string(hide_password=False)


def _build_query(cfg: SourceConfig) -> str:
    if cfg.query:
        return validate_readonly_query(cfg.query)
    table = validate_table_name(cfg.table or "")
    quoted = ".".join(f'"{part}"' for part in table.split("."))
    return f"SELECT * FROM {quoted}"


def _secrets_for(url: str, params: dict) -> list[str]:
    secrets: list[str] = []
    try:
        password = make_url(url).password
        if password:
            secrets.append(password)
    except Exception:
        pass
    # Only scrub distinctive string values; scrubbing short numbers would
    # mangle legitimate diagnostics (counts, codes).
    secrets.extend(str(v) for v in params.values() if isinstance(v, str) and len(v) >= 3)
    return secrets


def _sanitize_error(exc: Exception, url: str, params: dict) -> str:
    """A concise driver message with SQL text, parameters, and any known
    secret values removed."""
    orig = getattr(exc, "orig", None)
    text_value = str(orig) if orig is not None else str(exc)
    # SQLAlchemy formats appended [SQL: ...] / [parameters: ...] blocks.
    for marker in ("\n[SQL:", "[SQL:"):
        if marker in text_value:
            text_value = text_value.split(marker)[0]
    text_value = text_value.strip().splitlines()[0] if text_value.strip() else ""
    return scrub_sensitive(text_value, _secrets_for(url, params)) or type(exc).__name__


def load_database(cfg: SourceConfig, base_dir: Path) -> LoadedDataset:
    url = _database_url(cfg, base_dir)
    query = _build_query(cfg)
    timeout = int(cfg.options.get("timeout_seconds", 30))
    params = dict(cfg.params)
    logger.info("Connecting to %s (timeout %ss)", mask_url(url), timeout)

    connect_args: dict = {}
    parsed = make_url(url)
    if parsed.drivername.startswith("postgresql"):
        connect_args["connect_timeout"] = timeout
    elif parsed.drivername.startswith("sqlite"):
        connect_args["timeout"] = float(timeout)  # uri mode is in the URL query

    try:
        engine = create_engine(
            url, connect_args=connect_args, pool_pre_ping=True, hide_parameters=True
        )
    except Exception as exc:
        raise SourceError(
            f"Cannot create database engine for source '{cfg.name}'. "
            "For PostgreSQL install the driver: pip install "
            "'automatic-report-generator[postgres]'. "
            f"Details: {type(exc).__name__}: {_sanitize_error(exc, url, params)}"
        ) from None

    # Bound the transfer when a row limit is configured: fetch in chunks
    # and stop as soon as the limit (+1 row, so truncation is reported)
    # has been received instead of always materialising the full result.
    chunk_rows = cfg.options.get("chunk_rows")
    if cfg.max_rows is not None:
        chunk_rows = min(int(chunk_rows or 10_000), cfg.max_rows + 1)
    try:
        with engine.connect() as connection:
            if parsed.drivername.startswith("postgresql"):
                connection.execute(text("SET TRANSACTION READ ONLY"))
                connection.execute(
                    text("SELECT set_config('statement_timeout', :ms, true)"),
                    {"ms": str(timeout * 1000)},
                )
            if chunk_rows:
                frames: list[pd.DataFrame] = []
                total = 0
                target = cfg.max_rows + 1 if cfg.max_rows is not None else None
                for chunk in pd.read_sql(
                    text(query),
                    connection,
                    params=params,
                    chunksize=int(chunk_rows),
                ):
                    frames.append(chunk)
                    total += len(chunk)
                    if target is not None and total >= target:
                        break
                frame = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
            else:
                frame = pd.read_sql(text(query), connection, params=params)
    except SourceError:
        raise
    except Exception as exc:
        raise SourceError(
            f"Database query failed for source '{cfg.name}' "
            f"({mask_url(url)}): {type(exc).__name__}: {_sanitize_error(exc, url, params)}"
        ) from None
    finally:
        engine.dispose()
    return finalise_frame(frame, cfg, f"Database {mask_url(url)}")
