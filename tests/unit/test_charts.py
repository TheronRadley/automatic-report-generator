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


def test_pie_aggregates_full_data_and_keeps_other(tmp_path):
    """Regression: slices must come from full-data aggregation (not a
    pre-sampled subset) and overflow categories become 'Other'."""
    from reportgen.charts.engine import _prepare

    frame = pd.DataFrame(
        {
            "cat": [f"c{i}" for i in range(15)] + ["c0", "c1"],
            "v": [100.0] * 15 + [50.0, 50.0],
        }
    )
    spec = ChartConfig(type="pie", x="cat", y="v", title="Share", max_points=11)
    data, rows_used, _dropped, _y, notes = _prepare(frame, spec)
    assert rows_used == 17  # max_points must not pre-sample categorical data
    totals = dict(zip(data["cat"].astype(str), data["v"], strict=True))
    assert totals["c0"] == 150.0 and totals["c1"] == 150.0  # aggregated
    assert "Other" in totals
    assert sum(totals.values()) == frame["v"].sum()
    assert any("Other" in note for note in notes)


def test_bar_category_limit_is_disclosed(tmp_path):
    frame = pd.DataFrame({"cat": [f"c{i}" for i in range(40)], "v": [1.0] * 40})
    result = render_chart(
        frame, ChartConfig(type="bar", x="cat", y="v", title="B"), tmp_path / "b.png"
    )
    assert any("top 30 of 40" in note for note in result.notes)


def test_downsample_never_exceeds_max_points(tmp_path):
    from reportgen.charts.engine import _prepare

    frame = pd.DataFrame({"x": range(10_001), "y": [1.0] * 10_001})
    spec = ChartConfig(type="line", x="x", y="y", max_points=5000)
    data, _used, _d, _y, notes = _prepare(frame, spec)
    assert len(data) <= 5000
    assert any("displays" in note for note in notes)


def test_numeric_axis_is_not_converted_to_dates(tmp_path):
    from reportgen.charts.engine import _prepare

    frame = pd.DataFrame({"year": [2019, 2020, 2021, 2022], "v": [1.0, 2.0, 3.0, 4.0]})
    spec = ChartConfig(type="timeseries", x="year", y="v")
    data, *_ = _prepare(frame, spec)
    assert pd.api.types.is_numeric_dtype(data["year"])


def test_grouped_bar_offsets_do_not_overlap():
    from reportgen.charts.engine import _grouped_bar_offsets

    width, offsets = _grouped_bar_offsets(3)
    assert len(set(offsets)) == 3
    assert all(abs(offsets[i] - offsets[j]) >= width for i in range(3) for j in range(i + 1, 3))


def test_grouped_bar_and_scatter_render(tmp_path):
    frame = pd.DataFrame(
        {
            "cat": ["A", "A", "B", "B"],
            "grp": ["x", "y", "x", "y"],
            "v": [1.0, 2.0, 3.0, 4.0],
            "n": [1, 2, 3, 4],
        }
    )
    for spec in (
        ChartConfig(type="bar", x="cat", y="v", group_by="grp"),
        ChartConfig(type="hbar", x="cat", y="v", group_by="grp"),
        ChartConfig(type="scatter", x="n", y="v", group_by="grp"),
        ChartConfig(type="bar", x="cat", y=["v", "n"]),
    ):
        result = render_chart(frame, spec, tmp_path / f"{spec.type}-{spec.group_by}.png")
        assert result.path.exists()


def test_ignored_series_combinations_are_errors(tmp_path):
    frame = pd.DataFrame({"c": ["a"], "v": [1.0], "w": [2.0], "g": ["x"]})
    with pytest.raises(ChartError, match="exactly one"):
        render_chart(frame, ChartConfig(type="pie", x="c", y=["v", "w"]), tmp_path / "x.png")
    with pytest.raises(ChartError, match="not both"):
        render_chart(
            frame, ChartConfig(type="line", x="c", y=["v", "w"], group_by="g"), tmp_path / "x.png"
        )
    with pytest.raises(ChartError, match="do not support"):
        render_chart(frame, ChartConfig(type="pie", x="c", y="v", group_by="g"), tmp_path / "x.png")


def test_line_group_limit_is_disclosed(tmp_path):
    rows = [(f"g{i}", d, float(i + d)) for i in range(10) for d in range(3)]
    frame = pd.DataFrame(rows, columns=["g", "x", "v"])
    result = render_chart(
        frame,
        ChartConfig(type="line", x="x", y="v", group_by="g", title="G"),
        tmp_path / "g.png",
    )
    assert any("shows 8 of 10 groups" in note for note in result.notes)


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
