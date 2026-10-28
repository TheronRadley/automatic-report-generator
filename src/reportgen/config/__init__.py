"""Report definition loading and validation."""

from reportgen.config.loader import dump_example_config, load_config
from reportgen.config.models import ReportConfig

__all__ = ["ReportConfig", "load_config", "dump_example_config"]
