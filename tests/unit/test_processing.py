"""Transformation engine correctness and safety."""

from __future__ import annotations

import math

import pandas as pd
import pytest

from reportgen.errors import ProcessingError
from reportgen.processing.engine import apply_pipeline, apply_step
from reportgen.processing.validation import aggregate_value


def step(frame, spec, datasets=None):
    return apply_step(frame, spec, datasets or {})


def test_filter_and_sort(sales_frame):
    out = step(
        sales_frame, {"op": "filter", "column": "region", "operator": "==", "value": "North"}
    )
    assert len(out) == 2
    out = step(out, {"op": "sort", "by": ["revenue"], "ascending": False})
    assert out["revenue"].tolist() == [150.0, 100.0]


def test_filter_conditions_logic(sales_frame):
    out = step(
        sales_frame,
        {
            "op": "filter",
            "logic": "or",
            "conditions": [
                {"column": "region", "operator": "in", "value": ["East"]},
                {"column": "quantity", "operator": ">=", "value": 3},
            ],
        },
    )
    assert len(out) == 2  # East row (qty 4) + North qty-3 row; East counted once


@pytest.mark.parametrize(
    "op,value,expected",
    [
        ("between", [100, 180], 4),
        ("contains", "Lamp", 2),
        ("not_in", ["North", "South"], 1),
        (">", 150, 2),
    ],
)
def test_filter_operators(sales_frame, op, value, expected):
    column = "product" if op == "contains" else "revenue" if op != "not_in" else "region"
    out = step(sales_frame, {"op": "filter", "column": column, "operator": op, "value": value})
    assert len(out) == expected


def test_calculate_column(sales_frame):
    out = step(
        sales_frame, {"op": "calculate", "column": "check", "expression": "quantity * unit_price"}
    )
    assert out["check"].tolist() == out["revenue"].tolist()


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('echo hi')",
        "quantity.real",
        "open('/etc/passwd')",
        "[1, 2][0]",
    ],
)
def test_calculate_rejects_code_execution(sales_frame, expression):
    with pytest.raises(ProcessingError):
        step(sales_frame, {"op": "calculate", "column": "x", "expression": expression})


def test_calculate_division_by_zero_is_nan_not_crash(sales_frame):
    out = step(
        sales_frame,
        {"op": "calculate", "column": "x", "expression": "revenue / (quantity - quantity)"},
    )
    assert out["x"].isna().all()


def test_group_aggregate_all_functions(sales_frame):
    out = step(
        sales_frame,
        {
            "op": "group_aggregate",
            "group_by": ["region"],
            "aggregations": {"revenue": ["sum", "count", "mean", "min", "max", "median", "std"]},
        },
    )
    north = out[out["region"] == "North"].iloc[0]
    assert north["revenue_sum"] == 250.0
    assert north["revenue_count"] == 2
    assert north["revenue_mean"] == 125.0
    assert north["revenue_min"] == 100.0
    assert north["revenue_max"] == 150.0
    assert north["revenue_median"] == 125.0
    assert north["revenue_std"] == pytest.approx(35.3553, rel=1e-3)


def test_group_aggregate_single_keeps_name_and_unique_count(sales_frame):
    out = step(
        sales_frame,
        {
            "op": "group_aggregate",
            "group_by": ["region"],
            "aggregations": [{"column": "product", "aggregation": "nunique", "as": "products"}],
        },
    )
    south = out[out["region"] == "South"].iloc[0]
    assert south["products"] == 1


def test_aggregate_value_helpers(sales_frame):
    assert aggregate_value(sales_frame, "sum", "revenue") == 890.0
    assert aggregate_value(sales_frame, "average", "revenue") == pytest.approx(178.0)
    assert aggregate_value(sales_frame, "count", None) == 5
    assert aggregate_value(sales_frame, "unique_count", "region") == 3
    assert math.isnan(aggregate_value(sales_frame.assign(x=None), "mean", "x"))


def test_rename_select_dedupe(sales_frame):
    out = apply_pipeline(
        pd.concat([sales_frame, sales_frame]),
        [
            {"op": "rename", "mapping": {"region": "area"}},
            {"op": "dedupe"},
            {"op": "select", "columns": ["area", "revenue"]},
        ],
    )
    assert list(out.columns) == ["area", "revenue"]
    assert len(out) == 5


def test_missing_value_treatment():
    frame = pd.DataFrame({"a": [1.0, None, 3.0], "b": ["x", None, "z"]})
    filled = step(frame, {"op": "fill_missing", "values": {"a": 0, "b": "n/a"}})
    assert filled.loc[1, "a"] == 0 and filled.loc[1, "b"] == "n/a"
    mean_filled = step(frame, {"op": "fill_missing", "strategy": "mean", "columns": ["a"]})
    assert mean_filled.loc[1, "a"] == 2.0
    dropped = step(frame, {"op": "drop_missing", "subset": ["a"]})
    assert len(dropped) == 2


def test_convert_type_and_dates(sales_frame):
    out = step(sales_frame, {"op": "convert_type", "types": {"date": "date", "quantity": "float"}})
    assert pd.api.types.is_datetime64_any_dtype(out["date"])
    monthly = step(sales_frame, {"op": "date_normalize", "column": "date", "truncate": "month"})
    assert monthly["date"].dt.day.eq(1).all()


def test_running_total_and_pct_change(sales_frame):
    ordered = step(sales_frame, {"op": "sort", "by": ["date"]})
    out = step(ordered, {"op": "running_total", "column": "revenue"})
    assert out["revenue_running_total"].iloc[-1] == 890.0
    out = step(out, {"op": "pct_change", "column": "revenue"})
    # 100 -> 180 is +80%
    assert out["revenue_pct_change"].iloc[1] == pytest.approx(0.8)


def test_merge_and_append(sales_frame):
    targets = pd.DataFrame({"region": ["North", "South"], "target": [300, 600]})
    merged = apply_step(
        sales_frame, {"op": "merge", "other": "t", "on": ["region"], "how": "left"}, {"t": targets}
    )
    assert merged[merged["region"] == "North"]["target"].eq(300).all()
    appended = apply_step(sales_frame, {"op": "append", "other": "s"}, {"s": sales_frame})
    assert len(appended) == 10


def test_describe_step(sales_frame):
    out = step(sales_frame, {"op": "describe"})
    revenue = out[out["column"] == "revenue"].iloc[0]
    assert revenue["count"] == 5
    assert revenue["mean"] == pytest.approx(178.0)


def test_unknown_step_and_missing_column_errors(sales_frame):
    with pytest.raises(ProcessingError, match="Unknown processing step"):
        step(sales_frame, {"op": "explode"})
    with pytest.raises(ProcessingError, match="missing column"):
        step(sales_frame, {"op": "sort", "by": ["nope"]})
    with pytest.raises(ProcessingError, match="Unsupported aggregation"):
        step(
            sales_frame,
            {"op": "group_aggregate", "group_by": ["region"], "aggregations": {"revenue": "mode"}},
        )


def test_validate_step(sales_frame):
    with pytest.raises(Exception, match="required columns"):
        step(sales_frame, {"op": "validate", "required_columns": ["ghost"]})
