"""Report definition loading and schema validation."""

from __future__ import annotations

import pytest

from reportgen.config.loader import load_config
from reportgen.errors import ConfigError

BASE = """
version: 1
report: {title: T, template: general}
sources:
  - {name: d, type: csv, path: data.csv}
"""


def test_minimal_config_loads(write_config):
    config, _ = load_config(write_config(BASE))
    assert config.report.title == "T"
    assert config.exports.formats == ["pdf", "html", "xlsx"]


def test_missing_file(write_config, tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")


def test_unknown_key_rejected(write_config):
    with pytest.raises(ConfigError, match="surprise"):
        load_config(write_config(BASE + "surprise: 1\n"))


def test_unsupported_version(write_config):
    with pytest.raises(ConfigError, match="version"):
        load_config(write_config(BASE.replace("version: 1", "version: 99")))


def test_duplicate_sources(write_config):
    text = BASE + "  - {name: d, type: csv, path: other.csv}\n"
    with pytest.raises(ConfigError, match="Duplicate"):
        load_config(write_config(text))


def test_metric_unknown_dataset(write_config):
    text = BASE + "metrics:\n  - {name: m, dataset: ghost, aggregation: sum, column: x}\n"
    with pytest.raises(ConfigError, match="unknown dataset"):
        load_config(write_config(text))


def test_database_source_requires_query(write_config):
    text = """
version: 1
sources:
  - {name: db, type: sqlite, path: data.db}
"""
    with pytest.raises(ConfigError, match="query"):
        load_config(write_config(text))


def test_postgres_requires_url(write_config):
    text = """
version: 1
sources:
  - {name: db, type: postgres, query: "SELECT 1"}
"""
    with pytest.raises(ConfigError, match="url"):
        load_config(write_config(text))


def test_invalid_yaml_syntax(write_config):
    with pytest.raises(ConfigError, match="Invalid YAML"):
        load_config(write_config("version: [unclosed\n"))


def test_unsafe_yaml_is_rejected(write_config):
    """yaml.safe_load must refuse arbitrary Python object construction."""
    payload = "version: !!python/object/apply:os.system ['echo pwned']\n"
    with pytest.raises(ConfigError):
        load_config(write_config(payload))


def test_toml_config(tmp_path):
    path = tmp_path / "report.toml"
    path.write_text(
        'version = 1\n[report]\ntitle = "T"\ntemplate = "general"\n'
        '[[sources]]\nname = "d"\ntype = "csv"\npath = "data.csv"\n',
        encoding="utf-8",
    )
    config, _ = load_config(path)
    assert config.sources[0].name == "d"


def test_json_config(tmp_path):
    path = tmp_path / "report.json"
    path.write_text(
        '{"version": 1, "sources": [{"name": "d", "type": "json", "path": "x.json"}]}',
        encoding="utf-8",
    )
    config, _ = load_config(path)
    assert config.sources[0].type == "json"


def test_unsupported_suffix(tmp_path):
    path = tmp_path / "report.ini"
    path.write_text("version=1", encoding="utf-8")
    with pytest.raises(ConfigError, match="Unsupported"):
        load_config(path)
