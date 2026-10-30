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

from reportgen.errors import ProcessingError, ValidationError
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


def evaluate_expression(
    frame: pd.DataFrame, expression: str, notes: list[str] | None = None
) -> pd.Series:
    """Evaluate e.g. ``quantity * unit_price`` column-wise, safely.

    Referenced columns are coerced to numeric once, up front, so any
    values that cannot participate in arithmetic are counted and
    reported via ``notes`` instead of silently vanishing."""
    try:
        tree = ast.parse(str(expression), mode="eval")
    except SyntaxError as exc:
        raise ProcessingError(f"Invalid calculated expression {expression!r}: {exc.msg}") from exc
    columns: dict[str, pd.Series] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id not in columns:
            if node.id not in frame.columns:
                raise ProcessingError(
                    f"Calculated expression {expression!r} references unknown column "
                    f"'{node.id}'. Available: {', '.join(str(c) for c in frame.columns)}"
                )
            original = frame[node.id]
            coerced = pd.to_numeric(original, errors="coerce")
            lost = int((coerced.isna() & original.notna()).sum())
            if notes is not None and lost:
                notes.append(
                    f"calculate: {lost} value(s) in '{node.id}' are not numeric and "
                    "were treated as missing in the calculation."
                )
            columns[node.id] = coerced
    result = _eval_node(tree.body, columns, str(expression))
    if not isinstance(result, pd.Series):
        result = pd.Series([result] * len(frame), index=frame.index)
    return result


def _eval_node(node: ast.AST, columns: dict[str, pd.Series], expression: str) -> Any:
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        left = _eval_node(node.left, columns, expression)
        right = _eval_node(node.right, columns, expression)
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
        value = _eval_node(node.operand, columns, expression)
        return value if isinstance(node.op, ast.UAdd) else -value
    if isinstance(node, ast.Name):
        return columns[node.id]  # existence/coercion handled by evaluate_expression
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
    # Evaluate ONLY the requested comparison: building a dict of all six
    # would also run e.g. gt on a mixed-type column for a plain equality
    # filter and crash on unrelated comparisons.
    if op == "eq":
        mask = comparable.eq(value)
    elif op == "ne":
        mask = comparable.ne(value)
    elif op == "gt":
        mask = comparable.gt(value)
    elif op == "gte":
        mask = comparable.ge(value)
    elif op == "lt":
        mask = comparable.lt(value)
    else:
        mask = comparable.le(value)
    return mask.fillna(False)


def _apply_filter(frame: pd.DataFrame, params: dict[str, Any]) -> pd.DataFrame:
    conditions = params.get("conditions")
    if conditions is None:
        if "column" not in params:
            raise ProcessingError("Filter step needs 'column' or 'conditions'.")
        conditions = [{k: params[k] for k in ("column", "operator", "value") if k in params}]
    logic = str(params.get("logic", "and")).lower()
    if logic not in {"and", "or"}:
        raise ProcessingError(f"Filter step has invalid logic '{logic}'. Use 'and' or 'or'.")
    masks = [_condition_mask(frame, dict(c)) for c in conditions]
    if not masks:
        return frame
    combined = masks[0]
    for mask in masks[1:]:
        combined = (combined & mask) if logic == "and" else (combined | mask)
    return frame[combined].copy()


# ---------------------------------------------------------------------------
# Numeric coercion policy
# ---------------------------------------------------------------------------

_NUMERIC_AGGS = {"sum", "mean", "median", "std"}


def _coerce_numeric(series: pd.Series, context: str) -> pd.Series:
    """Coerce to numeric, refusing values that are not genuinely numeric.

    Numeric-looking strings ("10") convert; anything else is an
    explicit error naming example offenders, instead of silently
    dropping values (or, for grouped string sums, concatenating them).
    Genuinely missing values stay missing."""
    coerced = pd.to_numeric(series, errors="coerce")
    bad_mask = coerced.isna() & series.notna()
    if bool(bad_mask.any()):
        examples = series[bad_mask].astype(str).unique()[:3].tolist()
        raise ProcessingError(
            f"{context}: column '{series.name}' has {int(bad_mask.sum())} "
            f"value(s) that are not numeric (e.g. {examples}). Convert or "
            "clean the column first (e.g. with convert_type / filter)."
        )
    return coerced


def _exact_int_series(series: pd.Series) -> pd.Series:
    """Promote fixed-width integers to Python ints so sums/cumsums are
    exact instead of silently overflowing int64."""
    if pd.api.types.is_integer_dtype(series) and not pd.api.types.is_bool_dtype(series):
        return series.astype(object)
    return series


# ---------------------------------------------------------------------------
# Individual steps
# ---------------------------------------------------------------------------


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value]


def _step_select(frame, params, _datasets, _notes=None):
    columns = _as_list(params.get("columns"))
    if not columns:
        raise ProcessingError("Step 'select' needs 'columns'.")
    require_columns(frame, columns, "Step 'select'")
    return frame[columns].copy()


def _step_rename(frame, params, _datasets, _notes=None):
    mapping = params.get("mapping") or params.get("columns") or {}
    if not isinstance(mapping, dict) or not mapping:
        raise ProcessingError("Step 'rename' needs 'mapping': {old: new}.")
    require_columns(frame, list(mapping), "Step 'rename'")
    return frame.rename(columns={str(k): str(v) for k, v in mapping.items()})


def _step_drop_columns(frame, params, _datasets, _notes=None):
    columns = _as_list(params.get("columns"))
    require_columns(frame, columns, "Step 'drop_columns'")
    return frame.drop(columns=columns)


def _step_sort(frame, params, _datasets, _notes=None):
    by = _as_list(params.get("by") or params.get("columns"))
    if not by:
        raise ProcessingError("Step 'sort' needs 'by'.")
    require_columns(frame, by, "Step 'sort'")
    ascending = params.get("ascending", True)
    return frame.sort_values(by=by, ascending=ascending, kind="stable").reset_index(drop=True)


def _step_dedupe(frame, params, _datasets, _notes=None):
    subset = _as_list(params.get("subset")) or None
    if subset:
        require_columns(frame, subset, "Step 'dedupe'")
    keep = params.get("keep", "first")
    return frame.drop_duplicates(subset=subset, keep=keep).reset_index(drop=True)


def _step_fill_missing(frame, params, _datasets, _notes=None):
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


def _step_drop_missing(frame, params, _datasets, _notes=None):
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


def _step_convert_type(frame, params, _datasets, notes=None):
    types = params.get("types") or {}
    if params.get("column") is not None:
        types = {str(params["column"]): str(params.get("to", "string"))}
    if not types:
        raise ProcessingError("Step 'convert_type' needs 'types': {column: type}.")
    require_columns(frame, list(types), "Step 'convert_type'")
    errors = str(params.get("errors", "coerce"))
    result = frame.copy()
    for col, target in types.items():
        before = result[str(col)]
        try:
            converted = _convert_series(before, str(target), errors)
        except ProcessingError:
            raise
        except Exception as exc:
            raise ProcessingError(f"Cannot convert column '{col}' to {target}: {exc}") from exc
        if notes is not None and errors == "coerce":
            coerced_count = int((converted.isna() & before.notna()).sum())
            if coerced_count:
                notes.append(
                    f"convert_type: {coerced_count} value(s) in '{col}' could not be "
                    f"converted to {target} and were set to missing."
                )
        result[str(col)] = converted
    return result


def _step_date_normalize(frame, params, _datasets, notes=None):
    columns = _as_list(params.get("columns") or params.get("column"))
    if not columns:
        raise ProcessingError("Step 'date_normalize' needs 'column' or 'columns'.")
    require_columns(frame, columns, "Step 'date_normalize'")
    result = frame.copy()
    fmt = params.get("format")
    truncate = params.get("truncate")
    for col in columns:
        original = result[col]
        parsed = pd.to_datetime(original, format=fmt, errors="coerce")
        if notes is not None:
            unparsed = int((parsed.isna() & original.notna()).sum())
            if unparsed:
                notes.append(
                    f"date_normalize: {unparsed} value(s) in '{col}' were not "
                    "recognisable dates and were set to missing."
                )
        if truncate == "month":
            parsed = parsed.dt.to_period("M").dt.to_timestamp()
        elif truncate == "day":
            parsed = parsed.dt.normalize()
        result[col] = parsed
    return result


def _step_calculate(frame, params, _datasets, notes=None):
    column = params.get("column") or params.get("output")
    expression = params.get("expression")
    if not column or not expression:
        raise ProcessingError("Step 'calculate' needs 'column' (new name) and 'expression'.")
    result = frame.copy()
    values = evaluate_expression(result, str(expression), notes=notes)
    infinite = int(np.isinf(pd.to_numeric(values, errors="coerce")).sum())
    if notes is not None and infinite:
        notes.append(
            f"calculate '{column}': {infinite} result(s) were infinite "
            "(e.g. division by zero) and were set to missing."
        )
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


def _step_group_aggregate(frame, params, _datasets, _notes=None):
    group_by = _as_list(params.get("group_by"))
    specs = _aggregation_specs(params)
    require_columns(
        frame, group_by + [s[0] for s in specs if s[0] != "__count__"], "Step 'group_aggregate'"
    )
    # Unify with metric aggregation: numeric aggregations coerce
    # numeric-looking strings ("10" -> 10) and reject the rest, instead
    # of pandas' default string concatenation for grouped sums.
    frame = frame.copy()
    numeric_columns = {col for col, agg, _out in specs if agg in _NUMERIC_AGGS}
    extent_columns = {col for col, agg, _out in specs if agg in {"min", "max"}}
    for col in numeric_columns:
        frame[col] = _coerce_numeric(frame[col], "Step 'group_aggregate'")
    for col in extent_columns - numeric_columns:
        series = frame[col]
        if not pd.api.types.is_numeric_dtype(series):
            coerced = pd.to_numeric(series, errors="coerce")
            if not bool((coerced.isna() & series.notna()).any()):
                frame[col] = coerced  # numeric-looking strings compare numerically
    for col, agg, _out in specs:
        if agg == "sum":
            frame[col] = _exact_int_series(frame[col])
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


def _step_running_total(frame, params, _datasets, _notes=None):
    column = str(params.get("column", ""))
    require_columns(frame, [column], "Step 'running_total'")
    output = str(params.get("output") or f"{column}_running_total")
    group_by = _as_list(params.get("group_by"))
    result = frame.copy()
    numeric = _exact_int_series(_coerce_numeric(result[column], "Step 'running_total'"))
    if group_by:
        require_columns(result, group_by, "Step 'running_total'")
        result[output] = numeric.groupby([result[g] for g in group_by]).cumsum()
    else:
        result[output] = numeric.cumsum()
    return result


def _step_pct_change(frame, params, _datasets, notes=None):
    column = str(params.get("column", ""))
    require_columns(frame, [column], "Step 'pct_change'")
    output = str(params.get("output") or f"{column}_pct_change")
    periods = int(params.get("periods", 1))
    group_by = _as_list(params.get("group_by"))
    result = frame.copy()
    numeric = _coerce_numeric(result[column], "Step 'pct_change'")
    # fill_method=None: never forward-fill missing values before computing
    # a change (that would invent observations). A zero previous value has
    # no meaningful percentage change, so it becomes missing -- not inf.
    if group_by:
        require_columns(result, group_by, "Step 'pct_change'")
        grouped = numeric.groupby([result[g] for g in group_by])
        previous = grouped.shift(periods)
        change = grouped.pct_change(periods=periods, fill_method=None)
    else:
        previous = numeric.shift(periods)
        change = numeric.pct_change(periods=periods, fill_method=None)
    zero_denominators = int(((previous == 0) & numeric.notna()).sum())
    if notes is not None and zero_denominators:
        notes.append(
            f"pct_change '{column}': {zero_denominators} value(s) follow a zero, "
            "where a percentage change is undefined; they were set to missing."
        )
    result[output] = change.replace([np.inf, -np.inf], np.nan)
    return result


def _step_merge(frame, params, datasets, _notes=None):
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
    validate = params.get("validate")
    if validate is not None:
        if str(validate) not in {"one_to_one", "one_to_many", "many_to_one", "many_to_many"}:
            raise ProcessingError(
                "Step 'merge' validate must be one_to_one, one_to_many, "
                "many_to_one, or many_to_many."
            )
        kwargs["validate"] = str(validate)
    max_rows = int(params.get("max_result_rows", 1_000_000))
    if max_rows <= 0:
        raise ProcessingError("Step 'merge' max_result_rows must be positive.")
    estimated = _estimate_merge_rows(frame, other, kwargs, how)
    if estimated > max_rows:
        raise ProcessingError(
            f"Step 'merge' would produce an estimated {estimated:,} rows "
            f"(limit {max_rows:,}) from {len(frame):,} x {len(other):,} input rows. "
            "This usually means duplicated join keys are fanning out. Dedupe or "
            "aggregate first, set validate: to check cardinality, or raise "
            "max_result_rows if the expansion is intended."
        )
    try:
        merged = frame.merge(other, **kwargs)
    except Exception as exc:
        raise ProcessingError(f"Step 'merge' failed: {exc}") from exc
    if len(merged) > max_rows:
        raise ProcessingError(
            f"Step 'merge' produced {len(merged):,} rows, above max_result_rows "
            f"({max_rows:,}). Raise the limit only if this expansion is intended."
        )
    return merged


def _estimate_merge_rows(left, right, kwargs, how) -> int:
    """Worst-case output rows from join-key multiplicities, computed on
    per-key counts (small) rather than by materialising the merge."""
    if how == "cross":
        return len(left) * len(right)
    left_keys = kwargs.get("on") or kwargs.get("left_on") or []
    right_keys = kwargs.get("on") or kwargs.get("right_on") or []
    if not left_keys:
        return len(left) * max(1, len(right))
    left_counts = left.groupby(list(left_keys), dropna=False).size().rename("_lc").reset_index()
    right_counts = right.groupby(list(right_keys), dropna=False).size().rename("_rc").reset_index()
    right_counts = right_counts.rename(columns=dict(zip(right_keys, left_keys, strict=False)))
    both = left_counts.merge(right_counts, on=list(left_keys), how="outer")
    lc = both["_lc"].fillna(0).astype("int64")
    rc = both["_rc"].fillna(0).astype("int64")
    if how == "inner":
        return int((lc * rc).sum())
    if how == "left":
        return int((lc * rc.where(rc > 0, 1)).sum())
    if how == "right":
        return int((lc.where(lc > 0, 1) * rc).sum())
    matched = (lc * rc).where((lc > 0) & (rc > 0), 0)
    unmatched = (lc + rc).where((lc == 0) | (rc == 0), 0)
    return int((matched + unmatched).sum())


def _step_append(frame, params, datasets, _notes=None):
    other_name = params.get("other") or params.get("dataset")
    if not other_name or str(other_name) not in datasets:
        raise ProcessingError(f"Step 'append' references unknown dataset '{other_name}'.")
    return pd.concat([frame, datasets[str(other_name)]], ignore_index=True)


def _step_limit(frame, params, _datasets, _notes=None):
    rows = int(params.get("rows", params.get("n", 0)))
    if rows <= 0:
        raise ProcessingError("Step 'limit' needs a positive 'rows'.")
    return frame.head(rows).copy()


def _step_validate(frame, params, _datasets, _notes=None):
    validate_frame(
        frame,
        required_columns=_as_list(params.get("required_columns")) or None,
        non_empty=bool(params.get("non_empty", False)),
        min_rows=params.get("min_rows"),
        context="Dataset validation",
    )
    return frame


def _step_describe(frame, params, _datasets, _notes=None):
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


def _step_data_quality(frame, params, _datasets, notes=None):
    """Explicit data-quality rules with visible violation counts.

    Each rule: {column|columns, check: not_null|type|range|unique,
    on_error: fail|drop|coerce (range also accepts clamp), plus
    check-specific keys (type:, min:/max:)}. Handled violations are
    counted into ``notes`` so they surface in the report's warnings —
    nothing is silently dropped or coerced."""
    rules = params.get("rules")
    if not isinstance(rules, list) or not rules:
        raise ProcessingError("Step 'data_quality' needs a non-empty 'rules' list.")
    result = frame.copy()
    for rule in rules:
        if not isinstance(rule, dict):
            raise ProcessingError(f"Data-quality rules must be mappings, got {rule!r}.")
        check = str(rule.get("check") or rule.get("rule") or "")
        on_error = str(rule.get("on_error", "fail")).lower()
        if on_error not in {"fail", "drop", "coerce", "clamp"}:
            raise ProcessingError(
                f"Data-quality rule has invalid on_error '{on_error}'. "
                "Use fail, drop, or coerce (clamp for range)."
            )
        columns = _as_list(rule.get("columns") or rule.get("column"))
        if not columns:
            raise ProcessingError(f"Data-quality rule {rule!r} needs 'column'.")
        require_columns(result, columns, "Step 'data_quality'")
        label = str(rule.get("name") or f"{check}({', '.join(columns)})")

        def _fail(count: int, examples: str, _label: str = label) -> None:
            raise ValidationError(
                f"Data quality rule '{_label}' failed: {count} violating "
                f"value(s)/row(s) (e.g. {examples}). Fix the data or set "
                "on_error: drop/coerce to handle them explicitly."
            )

        if check == "not_null":
            if on_error in {"coerce", "clamp"}:
                raise ProcessingError("not_null rules support on_error: fail or drop.")
            bad = result[columns].isna().any(axis=1)
            count = int(bad.sum())
            if count and on_error == "fail":
                _fail(count, f"missing in {columns}")
            if count:
                result = result[~bad].reset_index(drop=True)
                if notes is not None:
                    notes.append(f"Data quality '{label}': dropped {count} row(s).")
        elif check == "unique":
            if on_error in {"coerce", "clamp"}:
                raise ProcessingError("unique rules support on_error: fail or drop.")
            dup_mask = result.duplicated(subset=columns, keep=False)
            extra = result.duplicated(subset=columns, keep="first")
            count = int(dup_mask.sum())
            if count and on_error == "fail":
                sample = result.loc[dup_mask, columns].head(2).to_dict("records")
                _fail(count, str(sample))
            if count:
                result = result[~extra].reset_index(drop=True)
                if notes is not None:
                    notes.append(
                        f"Data quality '{label}': dropped {int(extra.sum())} "
                        "duplicate row(s) (kept first occurrence)."
                    )
        elif check == "type":
            if len(columns) != 1:
                raise ProcessingError("type rules take exactly one column.")
            column = columns[0]
            target = str(rule.get("as") or rule.get("type") or "")
            series = result[column]
            if target in {"number", "numeric", "float", "int", "integer"}:
                converted = pd.to_numeric(series, errors="coerce")
                bad = converted.isna() & series.notna()
                coercion = converted
            elif target in {"date", "datetime"}:
                converted = pd.to_datetime(series, errors="coerce")
                bad = converted.isna() & series.notna()
                coercion = converted
            elif target in {"string", "str", "text"}:
                bad = series.notna() & ~series.map(lambda v: isinstance(v, str))
                coercion = series.astype("string")
            elif target in {"bool", "boolean"}:
                bad = series.notna() & ~series.map(
                    lambda v: isinstance(v, (bool, np.bool_)) or str(v).strip().lower() in _BOOL_MAP
                )
                coercion = series.map(
                    lambda v: (
                        _BOOL_MAP.get(str(v).strip().lower(), v)
                        if pd.notna(v) and not isinstance(v, (bool, np.bool_))
                        else v
                    )
                )
                coercion = coercion.where(~bad, pd.NA).astype("boolean")
            else:
                raise ProcessingError(
                    f"Data-quality type rule needs type: number|date|string|bool, got {target!r}."
                )
            count = int(bad.sum())
            if count and on_error == "fail":
                _fail(count, ", ".join(series[bad].astype(str).unique()[:3].tolist()))
            if count and on_error == "drop":
                result = result[~bad].reset_index(drop=True)
                if notes is not None:
                    notes.append(f"Data quality '{label}': dropped {count} row(s).")
            elif on_error == "coerce":
                result[column] = coercion
                if count and notes is not None:
                    notes.append(
                        f"Data quality '{label}': coerced the column to {target}; "
                        f"{count} value(s) became missing."
                    )
        elif check == "range":
            if len(columns) != 1:
                raise ProcessingError("range rules take exactly one column.")
            column = columns[0]
            low, high = rule.get("min"), rule.get("max")
            if low is None and high is None:
                raise ProcessingError("range rules need 'min' and/or 'max'.")
            series = result[column]
            numeric = pd.to_numeric(series, errors="coerce")
            non_numeric = numeric.isna() & series.notna()
            out_of_range = pd.Series(False, index=result.index)
            if low is not None:
                out_of_range |= numeric < float(low)
            if high is not None:
                out_of_range |= numeric > float(high)
            bad = (out_of_range | non_numeric).fillna(False)
            count = int(bad.sum())
            if count and on_error == "fail":
                _fail(count, ", ".join(series[bad].astype(str).unique()[:3].tolist()))
            if count and on_error == "drop":
                result = result[~bad].reset_index(drop=True)
                if notes is not None:
                    notes.append(f"Data quality '{label}': dropped {count} row(s).")
            elif count and on_error in {"coerce", "clamp"}:
                clamped = numeric.clip(
                    lower=float(low) if low is not None else None,
                    upper=float(high) if high is not None else None,
                )
                result[column] = clamped
                if notes is not None:
                    notes.append(
                        f"Data quality '{label}': clamped {int(out_of_range.sum())} "
                        f"value(s) to [{low}, {high}]; {int(non_numeric.sum())} "
                        "non-numeric value(s) became missing."
                    )
        else:
            raise ProcessingError(
                f"Unknown data-quality check '{check}'. Use not_null, type, range, or unique."
            )
    return result


_STEPS = {
    "select": _step_select,
    "rename": _step_rename,
    "drop_columns": _step_drop_columns,
    "filter": lambda f, p, d, n=None: _apply_filter(f, p),
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
    "data_quality": _step_data_quality,
    "quality": _step_data_quality,
}


def apply_step(
    frame: pd.DataFrame,
    step: dict[str, Any] | Any,
    datasets: dict[str, pd.DataFrame] | None = None,
    notes: list[str] | None = None,
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
        return handler(frame, params, datasets or {}, notes)
    except ProcessingError:
        raise
    except Exception as exc:
        raise ProcessingError(f"Step '{op}' failed: {exc}") from exc


def apply_pipeline(
    frame: pd.DataFrame,
    steps: list[Any],
    datasets: dict[str, pd.DataFrame] | None = None,
    notes: list[str] | None = None,
) -> pd.DataFrame:
    result = frame
    for step in steps:
        result = apply_step(result, step, datasets, notes=notes)
    return result
