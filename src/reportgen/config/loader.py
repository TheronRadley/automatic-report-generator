"""Load and validate report definitions from YAML, TOML, or JSON files.

YAML is parsed with ``yaml.safe_load`` only — arbitrary Python objects
can never be constructed from a config file.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError as PydanticValidationError

from reportgen.config.models import ReportConfig
from reportgen.errors import ConfigError

SUPPORTED_SUFFIXES = {".yaml", ".yml", ".toml", ".json"}


def _read_raw(path: Path) -> dict[str, Any]:
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise ConfigError(
            f"Unsupported config format '{suffix}'. Use .yaml, .yml, .toml, or .json."
        )
    try:
        if suffix in {".yaml", ".yml"}:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        elif suffix == ".toml":
            with path.open("rb") as handle:
                data = tomllib.load(handle)
        else:
            data = json.loads(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path.name}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {path.name}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON in {path.name}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"Cannot read config file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"Config file {path.name} must contain a mapping at the top level.")
    return data


def load_config(path: str | Path) -> tuple[ReportConfig, Path]:
    """Load a report definition. Returns ``(config, config_dir)``.

    Relative source/output paths are resolved against ``config_dir`` so a
    report definition is portable and reproducible from any working dir.
    """
    config_path = Path(path).expanduser()
    if not config_path.exists():
        raise ConfigError(f"Config file not found: {config_path}")
    if not config_path.is_file():
        raise ConfigError(f"Config path is not a file: {config_path}")
    raw = _read_raw(config_path)
    try:
        config = ReportConfig.model_validate(raw)
    except PydanticValidationError as exc:
        lines = []
        for err in exc.errors():
            location = ".".join(str(part) for part in err["loc"]) or "(root)"
            lines.append(f"  - {location}: {err['msg']}")
        raise ConfigError(
            f"Invalid report definition in {config_path.name}:\n" + "\n".join(lines)
        ) from exc
    return config, config_path.resolve().parent


def dump_example_config(template: str = "sales") -> str:
    """Return a complete, working example config for ``reportgen init``."""
    from reportgen.reports.templates import get_template

    return get_template(template).example_yaml
