"""Transformation pipeline engine.

Steps are declarative dicts from the report definition. Nothing here
uses ``eval``/``exec``: filters are structured conditions and calculated
columns use a restricted AST evaluator that only permits arithmetic
between columns and numeric literals.
"""

from __future__ import annotations

import ast
import operator
from typing import Any

import numpy as np
import pandas as pd

from reportgen.errors import ProcessingError
from reportgen.processing.validation import (
    normalise_aggregation,
    require_columns,
    validate_frame,
)

AGGREGATIONS = ("count", "sum", "mean", "min", "max", "median", "std", "nunique")

_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}


# ---------------------------------------------------------------------------
# Safe calculated-column expressions
# ---------------------------------------------------------------------------


def evaluate_expression(frame: pd.DataFrame, expression: str) -> pd.Series:
    """Evaluate e.g. ``quantity * unit_price`` column-wise, safely."""
    try:
        tree = ast.parse(str(expression), mode="eval")
    except SyntaxError as exc:
        raise ProcessingError(f"Invalid calculated expression {expression!r}: {exc.msg}") from exc
    result = _eval_node(tree.body, frame, str(expression))
    if not isinstance(result, pd.Series):
        result = pd.Series([result] * len(frame), index=frame.index)
    return result


def _eval_node(node: ast.AST, frame: pd.DataFrame, expression: str) -> Any:
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        left = _eval_node(node.left, frame, expression)
        right = _eval_node(node.right, frame, expression)
        # Guard against giant-integer exponentiation (e.g. 9 ** 9 ** 9)
        # hanging the process; column arithmetic uses floats and overflows
        # to inf safely, but scalar ** with a huge exponent must be refused.
        if isinstance(node.op, ast.Pow) and not isinstance(right, pd.Series):
            if isinstance(right, (int, float)) and abs(right) > 1000:
                raise ProcessingError(
                    f"Calculated expression {expression!r} uses an exponent "
                    "larger than 1000, which is refused."
                )
        with np.errstate(divide="ignore", invalid="ignore"):
            return _BIN_OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _eval_node(node.operand, frame, expression)
        return value if isinstance(node.op, ast.UAdd) else -value
    if isinstance(node, ast.Name):
        if node.id not in frame.columns:
            raise ProcessingError(
                f"Calculated expression {expression!r} references unknown column "
                f"'{node.id}'. Available: {', '.join(str(c) for c in frame.columns)}"
            )
        return pd.to_numeric(frame[node.id], errors="coerce")
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    raise ProcessingError(
        f"Calculated expression {expression!r} contains a disallowed construct "
        f"({type(node).__name__}). Only arithmetic (+ - * / // % **) between "
        "columns and numbers is supported; function calls are not allowed."
    )


# ---------------------------------------------------------------------------
# Structured filtering
# ---------------------------------------------------------------------------

_OPERATOR_ALIASES = {
    "==": "eq",
    "=": "eq",
    "eq": "eq",
    "equals": "eq",
    "!=": "ne",
    "ne": "ne",
    "not_equals": "ne",
    ">": "gt",
    "gt": "gt",
    ">=": "gte",
    "gte": "gte",
    "<": "lt",
    "lt": "lt",
    "<=": "lte",
    "lte": "lte",
    "in": "in",
    "not_in": "not_in",
    "contains": "contains",
    "is_null": "is_null",
    "isnull": "is_null",
    "not_null": "not_null",
    "notnull": "not_null",
    "between": "between",
}


def _condition_mask(frame: pd.DataFrame, condition: dict[str, Any]) -> pd.Series:
    column = condition.get("column")
    op_raw = str(condition.get("operator", "==")).lower()
    op = _OPERATOR_ALIASES.get(op_raw)
    if column is None or op is None:
        raise ProcessingError(
            f"Invalid filter condition {condition!r}. It needs 'column' and a "
            "supported 'operator' (==, !=, >, >=, <, <=, in, not_in, contains, "
            "is_null, not_null, between)."
        )
    require_columns(frame, [str(column)], "Filter")
    series = frame[str(column)]
    value = condition.get("value")
    if op == "is_null":
        return series.isna()
    if op == "not_null":
        return series.notna()
    if op == "between":
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise ProcessingError("Filter operator 'between' needs value: [low, high].")
        return series.between(value[0], value[1])
    if op in {"in", "not_in"}:
        if not isinstance(value, (list, tuple)):
            raise ProcessingError(f"Filter operator '{op_raw}' needs a list value.")
        mask = series.isin(list(value))
        return mask if op == "in" else ~mask
    if op == "contains":
        return series.astype(str).str.contains(str(value), regex=False, na=False)
    comparable = series
    # Coerce numeric comparisons so YAML strings vs numbers behave sensibly.
    if pd.api.types.is_numeric_dtype(series) and isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            pass
    ops = {
        "eq": comparable.eq(value),
        "ne": comparable.ne(value),
        "gt": comparable.gt(value),
        "gte": comparable.ge(value),
        "lt": comparable.lt(value),
        "lte": comparable.le(value),
    }
    return ops[op].fillna(False)


def _apply_filter(frame: pd.DataFrame, params: dict[str, Any]) -> pd.DataFrame:
    conditions = params.get("conditions")
    if conditions is None:
        if "column" not in params:
            raise ProcessingError("Filter step needs 'column' or 'conditions'.")
        conditions = [{k: params[k] for k in ("column", "operator", "value") if k in params}]
    logic = str(params.get("logic", "and")).lower()
    masks = [_condition_mask(frame, dict(c)) for c in conditions]
    if not masks:
        return frame
    combined = masks[0]
    for mask in masks[1:]:
        combined = (combined & mask) if logic == "and" else (combined | mask)
    return frame[combined].copy()


# ---------------------------------------------------------------------------
# Individual steps
# ---------------------------------------------------------------------------


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value]


def _step_select(frame, params, _datasets):
    columns = _as_list(params.get("columns"))
    if not columns:
        raise ProcessingError("Step 'select' needs 'columns'.")
    require_columns(frame, columns, "Step 'select'")
    return frame[columns].copy()


def _step_rename(frame, params, _datasets):
    mapping = params.get("mapping") or params.get("columns") or {}
    if not isinstance(mapping, dict) or not mapping:
        raise ProcessingError("Step 'rename' needs 'mapping': {old: new}.")
    require_columns(frame, list(mapping), "Step 'rename'")
    return frame.rename(columns={str(k): str(v) for k, v in mapping.items()})


def _step_drop_columns(frame, params, _datasets):
    columns = _as_list(params.get("columns"))
    require_columns(frame, columns, "Step 'drop_columns'")
    return frame.drop(columns=columns)


def _step_sort(frame, params, _datasets):
    by = _as_list(params.get("by") or params.get("columns"))
    if not by:
        raise ProcessingError("Step 'sort' needs 'by'.")
    require_columns(frame, by, "Step 'sort'")
    ascending = params.get("ascending", True)
    return frame.sort_values(by=by, ascending=ascending, kind="stable").reset_index(drop=True)


def _step_dedupe(frame, params, _datasets):
    subset = _as_list(params.get("subset")) or None
    if subset:
        require_columns(frame, subset, "Step 'dedupe'")
    keep = params.get("keep", "first")
    return frame.drop_duplicates(subset=subset, keep=keep).reset_index(drop=True)


def _step_fill_missing(frame, params, _datasets):
    result = frame.copy()
    values = params.get("values") or {}
    if params.get("column") is not None:
        values = {str(params["column"]): params.get("value")}
    strategy = params.get("strategy")
    if strategy:
        columns = _as_list(params.get("columns")) or list(result.columns)
        require_columns(result, columns, "Step 'fill_missing'")
        for col in columns:
            if strategy == "zero":
                result[col] = result[col].fillna(0)
            elif strategy in {"mean", "median"}:
                numeric = pd.to_numeric(result[col], errors="coerce")
                fill = numeric.mean() if strategy == "mean" else numeric.median()
                result[col] = numeric.fillna(fill)
            elif strategy == "ffill":
                result[col] = result[col].ffill()
            elif strategy == "bfill":
                result[col] = result[col].bfill()
            else:
                raise ProcessingError(
                    f"Unknown fill_missing strategy '{strategy}'. "
                    "Use zero, mean, median, ffill, or bfill."
                )
        return result
    if not values:
        raise ProcessingError(
            "Step 'fill_missing' needs 'values', 'column'+'value', or 'strategy'."
        )
    require_columns(result, list(values), "Step 'fill_missing'")
    return result.fillna(value={str(k): v for k, v in values.items()})


def _step_drop_missing(frame, params, _datasets):
    subset = _as_list(params.get("subset") or params.get("columns")) or None
    if subset:
        require_columns(frame, subset, "Step 'drop_missing'")
    return frame.dropna(subset=subset, how=str(params.get("how", "any"))).reset_index(drop=True)


_BOOL_MAP = {"true": True, "false": False, "yes": True, "no": False, "1": True, "0": False}


def _convert_series(series: pd.Series, target: str, errors: str) -> pd.Series:
    target = target.lower()
    if target in {"int", "integer"}:
        numeric = pd.to_numeric(series, errors="coerce" if errors == "coerce" else "raise")
        return numeric.astype("Int64")
    if target in {"float", "number", "numeric"}:
        return pd.to_numeric(series, errors="coerce" if errors == "coerce" else "raise")
    if target in {"str", "string", "text"}:
        return series.astype("string")
    if target in {"date", "datetime"}:
        return pd.to_datetime(series, errors="coerce" if errors == "coerce" else "raise")
    if target in {"bool", "boolean"}:
        return series.map(
            lambda v: _BOOL_MAP.get(str(v).strip().lower(), v) if pd.notna(v) else v
        ).astype("boolean")
    if target == "category":
        return series.astype("category")
    raise ProcessingError(
        f"Unknown target type '{target}'. Use int, float, string, date, bool, or category."
    )


def _step_convert_type(frame, params, _datasets):
    types = params.get("types") or {}
    if params.get("column") is not None:
        types = {str(params["column"]): str(params.get("to", "string"))}
    if not types:
        raise ProcessingError("Step 'convert_type' needs 'types': {column: type}.")
    require_columns(frame, list(types), "Step 'convert_type'")
    errors = str(params.get("errors", "coerce"))
    result = frame.copy()
    for col, target in types.items():
        try:
            result[str(col)] = _convert_series(result[str(col)], str(target), errors)
        except ProcessingError:
            raise
        except Exception as exc:
            raise ProcessingError(f"Cannot convert column '{col}' to {target}: {exc}") from exc
    return result


def _step_date_normalize(frame, params, _datasets):
    columns = _as_list(params.get("columns") or params.get("column"))
    if not columns:
        raise ProcessingError("Step 'date_normalize' needs 'column' or 'columns'.")
    require_columns(frame, columns, "Step 'date_normalize'")
    result = frame.copy()
    fmt = params.get("format")
    truncate = params.get("truncate")
    for col in columns:
        parsed = pd.to_datetime(result[col], format=fmt, errors="coerce")
        if truncate == "month":
            parsed = parsed.dt.to_period("M").dt.to_timestamp()
        elif truncate == "day":
            parsed = parsed.dt.normalize()
        result[col] = parsed
    return result


def _step_calculate(frame, params, _datasets):
    column = params.get("column") or params.get("output")
    expression = params.get("expression")
    if not column or not expression:
        raise ProcessingError("Step 'calculate' needs 'column' (new name) and 'expression'.")
    result = frame.copy()
    values = evaluate_expression(result, str(expression))
    result[str(column)] = values.replace([np.inf, -np.inf], np.nan)
    return result


def _aggregation_specs(params: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Normalise aggregation config to (column, canonical_agg, output_name)."""
    raw = params.get("aggregations") or params.get("aggregate") or {}
    specs: list[tuple[str, str, str]] = []
    if isinstance(raw, dict):
        for column, aggs in raw.items():
            agg_list = [aggs] if isinstance(aggs, str) else list(aggs)
            for agg in agg_list:
                canonical = normalise_aggregation(str(agg))
                output = str(column) if len(agg_list) == 1 else f"{column}_{canonical}"
                specs.append((str(column), canonical, output))
    elif isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict) or "column" not in item:
                raise ProcessingError("Aggregation entries need 'column' and 'aggregation'.")
            canonical = normalise_aggregation(str(item.get("aggregation", "sum")))
            output = str(item.get("as") or item["column"])
            specs.append((str(item["column"]), canonical, output))
    if not specs:
        raise ProcessingError("Step 'group_aggregate' needs 'aggregations'.")
    return specs


def _step_group_aggregate(frame, params, _datasets):
    group_by = _as_list(params.get("group_by"))
    specs = _aggregation_specs(params)
    require_columns(
        frame, group_by + [s[0] for s in specs if s[0] != "__count__"], "Step 'group_aggregate'"
    )
    named = {output: (column, agg) for column, agg, output in specs}
    if group_by:
        grouped = frame.groupby(group_by, dropna=False, sort=True)
        result = grouped.agg(**_named_agg(named)).reset_index()
    else:
        row = {}
        for output, (column, agg) in named.items():
            series = frame[column]
            row[output] = getattr(series, agg)() if hasattr(series, agg) else np.nan
        result = pd.DataFrame([row])
    sort_by = _as_list(params.get("sort_by"))
    if sort_by:
        require_columns(result, sort_by, "Step 'group_aggregate' sort")
        result = result.sort_values(by=sort_by, kind="stable").reset_index(drop=True)
    return result


def _named_agg(named: dict[str, tuple[str, str]]) -> dict[str, Any]:
    # Plain (column, aggfunc) tuples are accepted by DataFrameGroupBy.agg.
    return dict(named)


def _step_running_total(frame, params, _datasets):
    column = str(params.get("column", ""))
    require_columns(frame, [column], "Step 'running_total'")
    output = str(params.get("output") or f"{column}_running_total")
    group_by = _as_list(params.get("group_by"))
    result = frame.copy()
    numeric = pd.to_numeric(result[column], errors="coerce")
    if group_by:
        require_columns(result, group_by, "Step 'running_total'")
        result[output] = numeric.groupby([result[g] for g in group_by]).cumsum()
    else:
        result[output] = numeric.cumsum()
    return result


def _step_pct_change(frame, params, _datasets):
    column = str(params.get("column", ""))
    require_columns(frame, [column], "Step 'pct_change'")
    output = str(params.get("output") or f"{column}_pct_change")
    periods = int(params.get("periods", 1))
    group_by = _as_list(params.get("group_by"))
    result = frame.copy()
    numeric = pd.to_numeric(result[column], errors="coerce")
    if group_by:
        require_columns(result, group_by, "Step 'pct_change'")
        result[output] = numeric.groupby([result[g] for g in group_by]).pct_change(periods=periods)
    else:
        result[output] = numeric.pct_change(periods=periods)
    return result


def _step_merge(frame, params, datasets):
    other_name = params.get("other") or params.get("dataset")
    if not other_name or str(other_name) not in datasets:
        raise ProcessingError(
            f"Step 'merge' references unknown dataset '{other_name}'. "
            f"Known: {', '.join(sorted(datasets))}"
        )
    other = datasets[str(other_name)]
    how = str(params.get("how", "inner"))
    if how not in {"inner", "left", "right", "outer", "cross"}:
        raise ProcessingError(f"Unsupported merge type '{how}'.")
    on = _as_list(params.get("on"))
    kwargs: dict[str, Any] = {"how": how}
    if on:
        require_columns(frame, on, "Step 'merge' (left)")
        require_columns(other, on, "Step 'merge' (right)")
        kwargs["on"] = on
    elif params.get("left_on") and params.get("right_on"):
        kwargs["left_on"] = _as_list(params["left_on"])
        kwargs["right_on"] = _as_list(params["right_on"])
    elif how != "cross":
        raise ProcessingError("Step 'merge' needs 'on' (or 'left_on'/'right_on').")
    return frame.merge(other, **kwargs)


def _step_append(frame, params, datasets):
    other_name = params.get("other") or params.get("dataset")
    if not other_name or str(other_name) not in datasets:
        raise ProcessingError(f"Step 'append' references unknown dataset '{other_name}'.")
    return pd.concat([frame, datasets[str(other_name)]], ignore_index=True)


def _step_limit(frame, params, _datasets):
    rows = int(params.get("rows", params.get("n", 0)))
    if rows <= 0:
        raise ProcessingError("Step 'limit' needs a positive 'rows'.")
    return frame.head(rows).copy()


def _step_validate(frame, params, _datasets):
    validate_frame(
        frame,
        required_columns=_as_list(params.get("required_columns")) or None,
        non_empty=bool(params.get("non_empty", False)),
        min_rows=params.get("min_rows"),
        context="Dataset validation",
    )
    return frame


def _step_describe(frame, params, _datasets):
    columns = _as_list(params.get("columns")) or [
        c for c in frame.columns if pd.api.types.is_numeric_dtype(frame[c])
    ]
    if not columns:
        raise ProcessingError("Step 'describe' found no numeric columns.")
    rows: list[dict[str, Any]] = []
    for col in columns:
        series = pd.to_numeric(frame[col], errors="coerce")
        rows.append(
            {
                "column": col,
                "count": int(series.count()),
                "mean": series.mean(),
                "std": series.std(),
                "min": series.min(),
                "median": series.median(),
                "max": series.max(),
                "unique": int(series.nunique(dropna=True)),
                "missing": int(series.isna().sum()),
            }
        )
    return pd.DataFrame(rows)


_STEPS = {
    "select": _step_select,
    "rename": _step_rename,
    "drop_columns": _step_drop_columns,
    "filter": lambda f, p, d: _apply_filter(f, p),
    "sort": _step_sort,
    "dedupe": _step_dedupe,
    "drop_duplicates": _step_dedupe,
    "fill_missing": _step_fill_missing,
    "drop_missing": _step_drop_missing,
    "convert_type": _step_convert_type,
    "date_normalize": _step_date_normalize,
    "calculate": _step_calculate,
    "group_aggregate": _step_group_aggregate,
    "aggregate": _step_group_aggregate,
    "running_total": _step_running_total,
    "pct_change": _step_pct_change,
    "percentage_change": _step_pct_change,
    "merge": _step_merge,
    "append": _step_append,
    "concat": _step_append,
    "limit": _step_limit,
    "head": _step_limit,
    "validate": _step_validate,
    "describe": _step_describe,
}


def apply_step(
    frame: pd.DataFrame,
    step: dict[str, Any] | Any,
    datasets: dict[str, pd.DataFrame] | None = None,
) -> pd.DataFrame:
    op = step.op if hasattr(step, "op") else step.get("op")  # type: ignore[union-attr]
    params = (
        step.params
        if hasattr(step, "params")
        else {  # type: ignore[union-attr]
            k: v
            for k, v in dict(step).items()
            if k != "op"  # type: ignore[arg-type]
        }
    )
    handler = _STEPS.get(str(op))
    if handler is None:
        raise ProcessingError(
            f"Unknown processing step '{op}'. Supported steps: {', '.join(sorted(set(_STEPS)))}"
        )
    try:
        return handler(frame, params, datasets or {})
    except ProcessingError:
        raise
    except Exception as exc:
        raise ProcessingError(f"Step '{op}' failed: {exc}") from exc


def apply_pipeline(
    frame: pd.DataFrame,
    steps: list[Any],
    datasets: dict[str, pd.DataFrame] | None = None,
) -> pd.DataFrame:
    result = frame
    for step in steps:
        result = apply_step(result, step, datasets)
    return result
