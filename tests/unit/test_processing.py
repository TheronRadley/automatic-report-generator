"""Transformation engine correctness and safety."""

from __future__ import annotations

import math

import numpy as np
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


def test_grouped_string_numbers_sum_numerically_not_concatenated():
    """Regression: grouped sum of "10"+"20" used to produce "1020"."""
    frame = pd.DataFrame({"g": ["a", "a", "b"], "v": ["10", "20", "5"]})
    out = step(frame, {"op": "group_aggregate", "group_by": ["g"], "aggregations": {"v": "sum"}})
    totals = dict(zip(out["g"], out["v"], strict=True))
    assert totals == {"a": 30, "b": 5}
    # Metric aggregation now follows the identical policy.
    assert aggregate_value(frame, "sum", "v") == 35


def test_numeric_aggregation_rejects_non_numeric_values():
    frame = pd.DataFrame({"g": ["a", "a"], "v": ["10", "lots"]})
    with pytest.raises(ProcessingError, match="not numeric"):
        step(frame, {"op": "group_aggregate", "group_by": ["g"], "aggregations": {"v": "sum"}})
    with pytest.raises(ProcessingError, match="not numeric"):
        aggregate_value(frame, "mean", "v")


def test_integer_sum_does_not_overflow():
    big = 9_000_000_000_000_000_000  # > int64 max when doubled
    frame = pd.DataFrame(
        {"g": ["a", "a"], "v": pd.array([big // 2 + 7, big // 2 + 7], dtype="int64")}
    )
    out = step(frame, {"op": "group_aggregate", "group_by": ["g"], "aggregations": {"v": "sum"}})
    assert int(out.loc[0, "v"]) == big + 14
    assert aggregate_value(frame, "sum", "v") == float(big + 14)


def test_equality_filter_only_runs_equality_on_mixed_types():
    """Regression: all six comparisons were evaluated eagerly, so an
    equality filter crashed on gt/lt between int and str values."""
    frame = pd.DataFrame({"x": [1, "a", 2, "b"], "y": [1, 2, 3, 4]})
    out = step(frame, {"op": "filter", "column": "x", "operator": "==", "value": "a"})
    assert out["y"].tolist() == [2]


def test_filter_rejects_invalid_logic(sales_frame):
    with pytest.raises(ProcessingError, match="invalid logic"):
        step(
            sales_frame,
            {
                "op": "filter",
                "logic": "xor",
                "conditions": [{"column": "region", "operator": "==", "value": "North"}],
            },
        )


def test_pct_change_no_forward_fill_and_zero_denominator():
    frame = pd.DataFrame({"v": [10.0, None, 20.0, 0.0, 5.0]})
    notes: list[str] = []
    out = apply_step(frame, {"op": "pct_change", "column": "v"}, {}, notes=notes)
    changes = out["v_pct_change"]
    # Row 2 would be +100% if the missing row were forward-filled.
    assert pd.isna(changes.iloc[2])
    # 0 -> 5 has no meaningful percentage change: missing, not inf.
    assert pd.isna(changes.iloc[4])
    assert not np.isinf(changes.fillna(0)).any()
    assert any("zero" in note for note in notes)


def test_merge_growth_bound_and_cardinality_validation(sales_frame):
    dup = pd.DataFrame({"region": ["North"] * 4, "code": range(4)})
    with pytest.raises(ProcessingError, match="estimated"):
        apply_step(
            sales_frame,
            {"op": "merge", "other": "d", "on": ["region"], "max_result_rows": 5},
            {"d": dup},
        )
    with pytest.raises(ProcessingError, match="merge"):
        apply_step(
            sales_frame,
            {"op": "merge", "other": "d", "on": ["region"], "validate": "many_to_one"},
            {"d": dup},
        )


def test_data_quality_rules_with_visible_counts():
    frame = pd.DataFrame(
        {
            "id": [1, 2, 2, 3, 4],
            "score": [10.0, -5.0, 250.0, None, 40.0],
            "when": ["2025-01-01", "not-a-date", "2025-01-03", "2025-01-04", None],
        }
    )
    notes: list[str] = []
    out = apply_step(
        frame,
        {
            "op": "data_quality",
            "rules": [
                {"column": "when", "check": "type", "type": "date", "on_error": "coerce"},
                {"column": "score", "check": "range", "min": 0, "max": 100, "on_error": "clamp"},
                {"column": "id", "check": "unique", "on_error": "drop"},
                {"column": "score", "check": "not_null", "on_error": "drop"},
            ],
        },
        {},
        notes=notes,
    )
    assert out["id"].is_unique
    assert out["score"].between(0, 100).all()
    assert out["score"].notna().all()
    joined = " ".join(notes)
    assert "coerced" in joined and "clamped 2" in joined and "dropped" in joined
    with pytest.raises(Exception, match="Data quality"):
        apply_step(
            frame,
            {
                "op": "data_quality",
                "rules": [
                    {"column": "id", "check": "unique", "on_error": "fail"},
                ],
            },
            {},
        )


def test_coercion_notes_for_convert_and_dates():
    frame = pd.DataFrame({"d": ["2025-01-01", "junk"], "n": ["1.5", "NaN-ish"]})
    notes: list[str] = []
    step_notes = apply_pipeline(
        frame,
        [
            {"op": "date_normalize", "column": "d"},
            {"op": "convert_type", "types": {"n": "float"}},
        ],
        {},
        notes=notes,
    )
    assert step_notes is not None
    assert any("date_normalize" in n and "1 value" in n for n in notes)
    assert any("convert_type" in n for n in notes)


def test_validate_step(sales_frame):
    with pytest.raises(Exception, match="required columns"):
        step(sales_frame, {"op": "validate", "required_columns": ["ghost"]})
