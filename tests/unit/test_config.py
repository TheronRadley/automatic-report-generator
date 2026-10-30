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


# ---------------------------------------------------------------- typed schemas (#13)


def test_step_unknown_parameter_rejected(write_config):
    text = (
        BASE
        + """
processing:
  - dataset: d
    steps:
      - {op: sort, columns: [a], ascendingg: false}
"""
    )
    with pytest.raises(ConfigError, match="ascendingg"):
        load_config(write_config(text))


def test_step_wrong_parameter_type_rejected(write_config):
    text = (
        BASE
        + """
processing:
  - dataset: d
    steps:
      - {op: sort, columns: [a], ascending: "yes"}
"""
    )
    with pytest.raises(ConfigError, match="ascending"):
        load_config(write_config(text))


def test_step_missing_required_parameter_rejected(write_config):
    text = (
        BASE
        + """
processing:
  - dataset: d
    steps:
      - {op: drop_columns}
"""
    )
    with pytest.raises(ConfigError, match="requires parameter 'columns'"):
        load_config(write_config(text))


def test_unknown_filter_operator_rejected_at_load(write_config):
    text = (
        BASE
        + """
processing:
  - dataset: d
    steps:
      - {op: filter, conditions: [{column: a, near: 3}]}
"""
    )
    with pytest.raises(ConfigError, match="near"):
        load_config(write_config(text))


def test_unknown_source_option_rejected(write_config):
    text = """
version: 1
report: {title: T, template: general}
sources:
  - {name: d, type: csv, path: data.csv, options: {sep: ;}}
"""
    with pytest.raises(ConfigError, match="sep"):
        load_config(write_config(text))


def test_out_of_bounds_source_option_rejected(write_config):
    text = """
version: 1
report: {title: T, template: general}
sources:
  - {name: d, type: csv, path: data.csv, options: {max_rows: 0}}
"""
    with pytest.raises(ConfigError, match="max_rows"):
        load_config(write_config(text))


def test_currency_and_timezone_validated(write_config):
    bad_tz = """
version: 1
report: {title: T, template: general, timezone: Mars/Olympus}
sources: [{name: d, type: csv, path: data.csv}]
"""
    with pytest.raises(ConfigError, match="timezone"):
        load_config(write_config(bad_tz))
    ok = """
version: 1
report: {title: T, template: general, timezone: Europe/Berlin, currency: "€"}
sources: [{name: d, type: csv, path: data.csv}]
"""
    config, _ = load_config(write_config(ok, "ok.yaml"))
    assert config.report.timezone == "Europe/Berlin"
    assert config.report.currency == "€"


def test_parameters_must_be_simple_scalars(write_config):
    text = BASE + "parameters: {nested: {a: 1}}\n"
    with pytest.raises(ConfigError, match="scalar"):
        load_config(write_config(text))


def test_template_default_outputs_resolve_references(write_config):
    # No processing section: the sales template's default pipelines
    # create monthly_summary/regional_summary, so referencing them in
    # an explicit section must validate at load time (#13).
    text = """
version: 1
report: {title: Sales, template: sales}
sources:
  - {name: sales, type: csv, path: sales.csv}
metrics:
  - {name: total_revenue, dataset: sales, aggregation: sum, column: revenue}
sections:
  - {type: table, title: Monthly, dataset: monthly_revenue}
"""
    config, _ = load_config(write_config(text))
    assert config.sections[0].dataset == "monthly_revenue"


def test_unknown_dataset_with_template_defaults_still_rejected(write_config):
    text = """
version: 1
report: {title: Sales, template: sales}
sources:
  - {name: sales, type: csv, path: sales.csv}
sections:
  - {type: table, title: Nope, dataset: never_created}
"""
    with pytest.raises(ConfigError, match="never_created"):
        load_config(write_config(text))
