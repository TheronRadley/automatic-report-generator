"""Chart rendering for every supported chart type."""

from __future__ import annotations

import pandas as pd
import pytest

from reportgen.charts.engine import render_chart
from reportgen.config.models import ChartConfig
from reportgen.errors import ChartError

CASES = [
    ("line", {"x": "date", "y": "revenue"}),
    ("timeseries", {"x": "date", "y": "revenue"}),
    ("area", {"x": "date", "y": "revenue"}),
    ("bar", {"x": "region", "y": "revenue"}),
    ("hbar", {"x": "region", "y": "revenue"}),
    ("scatter", {"x": "quantity", "y": "revenue"}),
    ("histogram", {"x": "revenue"}),
    ("pie", {"x": "region", "y": "revenue"}),
    ("donut", {"x": "region", "y": "revenue"}),
]


@pytest.mark.parametrize("chart_type,axes", CASES)
def test_chart_types_render_png(tmp_path, chart_type, axes):
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(["2025-01-01", "2025-02-01", "2025-03-01", "2025-04-01"]),
            "region": ["North", "South", "North", "East"],
            "quantity": [1, 2, 3, 4],
            "revenue": [100.0, 240.0, 150.0, 400.0],
        }
    )
    output = tmp_path / f"{chart_type}.png"
    result = render_chart(frame, ChartConfig(type=chart_type, title="T", **axes), output)
    assert output.exists() and output.stat().st_size > 1000
    assert output.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert result.rows_used == 4


def test_missing_values_dropped_and_counted(tmp_path):
    frame = pd.DataFrame({"x": [1, 2, 3], "y": [1.0, None, 3.0]})
    result = render_chart(frame, ChartConfig(type="scatter", x="x", y="y"), tmp_path / "c.png")
    assert result.rows_dropped == 1
    assert result.rows_used == 2


def test_empty_chart_is_an_error(tmp_path):
    frame = pd.DataFrame({"x": [1], "y": [None]})
    with pytest.raises(ChartError, match="no plottable rows"):
        render_chart(frame, ChartConfig(type="line", x="x", y="y"), tmp_path / "c.png")


def test_pie_rejects_negative_values(tmp_path):
    frame = pd.DataFrame({"c": ["a", "b"], "v": [10.0, -5.0]})
    with pytest.raises(ChartError, match="negative"):
        render_chart(frame, ChartConfig(type="pie", x="c", y="v"), tmp_path / "c.png")


def test_unknown_chart_column(tmp_path):
    frame = pd.DataFrame({"x": [1], "y": [2]})
    with pytest.raises(Exception, match="missing column"):
        render_chart(frame, ChartConfig(type="bar", x="ghost", y="y"), tmp_path / "c.png")


def test_grouped_line_chart(tmp_path):
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(["2025-01-01", "2025-01-01", "2025-02-01", "2025-02-01"]),
            "region": ["N", "S", "N", "S"],
            "revenue": [1.0, 2.0, 3.0, 4.0],
        }
    )
    result = render_chart(
        frame,
        ChartConfig(type="line", x="date", y="revenue", group_by="region"),
        tmp_path / "g.png",
        theme_name="contrast",
    )
    assert result.path.exists()
