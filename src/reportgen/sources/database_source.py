"""Database adapter (SQLite + PostgreSQL) via SQLAlchemy.

Access is read-only by construction: queries are validated as single
SELECT statements, values are always bound parameters (never string
interpolation), SQLite files are opened with ``mode=ro``, and
PostgreSQL sessions request a read-only transaction. Credentials come
from environment variables and are masked in every log line.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from reportgen.config.models import SourceConfig
from reportgen.errors import SourceError
from reportgen.logging_config import get_logger
from reportgen.sources.base import LoadedDataset, finalise_frame
from reportgen.utils.security import (
    env_value,
    expand_env,
    mask_url,
    resolve_input_path,
    validate_readonly_query,
    validate_table_name,
)

logger = get_logger("reportgen.sources.database")


def _sqlite_url(cfg: SourceConfig, base_dir: Path) -> str:
    if cfg.url or cfg.url_env:
        raw = env_value(cfg.url_env) if cfg.url_env else expand_env(cfg.url)
        return raw or ""
    assert cfg.path is not None  # enforced by the config model
    db_path = resolve_input_path(cfg.path, base_dir)
    # mode=ro: the file cannot be modified even if a query slipped through.
    return f"sqlite:///file:{db_path}?mode=ro&uri=true"


def _database_url(cfg: SourceConfig, base_dir: Path) -> str:
    if cfg.type == "sqlite":
        return _sqlite_url(cfg, base_dir)
    raw = env_value(cfg.url_env) if cfg.url_env else expand_env(cfg.url)
    if not raw and cfg.path:  # allow a sqlite file via type=database
        return _sqlite_url(cfg, base_dir)
    if not raw:
        raise SourceError(f"Database source '{cfg.name}' has no connection URL.")
    url = make_url(raw)
    # Normalise bare postgres URLs onto the psycopg (v3) driver when present.
    if url.drivername in {"postgres", "postgresql"}:
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


def load_database(cfg: SourceConfig, base_dir: Path) -> LoadedDataset:
    url = _database_url(cfg, base_dir)
    query = _build_query(cfg)
    timeout = int(cfg.options.get("timeout_seconds", 30))
    logger.info("Connecting to %s (timeout %ss)", mask_url(url), timeout)

    connect_args: dict = {}
    parsed = make_url(url)
    if parsed.drivername.startswith("postgresql"):
        connect_args["connect_timeout"] = timeout
    elif parsed.drivername.startswith("sqlite"):
        connect_args["timeout"] = float(timeout)

    try:
        engine = create_engine(url, connect_args=connect_args, pool_pre_ping=True)
    except Exception as exc:
        raise SourceError(
            f"Cannot create database engine for source '{cfg.name}'. "
            "For PostgreSQL install the driver: pip install "
            "'automatic-report-generator[postgres]'. "
            f"Details: {type(exc).__name__}: {exc}"
        ) from exc

    chunk_rows = cfg.options.get("chunk_rows")
    try:
        with engine.connect() as connection:
            if parsed.drivername.startswith("postgresql"):
                connection.execute(text("SET TRANSACTION READ ONLY"))
                connection.execute(text(f"SET statement_timeout = {timeout * 1000}"))
            if chunk_rows:
                frames: list[pd.DataFrame] = []
                total = 0
                for chunk in pd.read_sql(
                    text(query),
                    connection,
                    params=dict(cfg.params),
                    chunksize=int(chunk_rows),
                ):
                    frames.append(chunk)
                    total += len(chunk)
                    if cfg.max_rows is not None and total >= cfg.max_rows:
                        break
                frame = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
            else:
                frame = pd.read_sql(text(query), connection, params=dict(cfg.params))
    except SourceError:
        raise
    except Exception as exc:
        raise SourceError(
            f"Database query failed for source '{cfg.name}' "
            f"({mask_url(url)}): {type(exc).__name__}: {exc}"
        ) from exc
    finally:
        engine.dispose()
    return finalise_frame(frame, cfg, f"Database {mask_url(url)}")
