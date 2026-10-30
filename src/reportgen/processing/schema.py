"""Typed schemas for processing steps.

Previously steps were free-form dicts: a misspelled parameter was
silently ignored and a wrong type failed deep inside pandas. Now each
``op`` declares its parameters, kinds, and requirements, and invalid
steps are rejected when the report definition is loaded — long before
generation starts. The engine still validates columns against real
data at run time.
"""

from __future__ import annotations

from typing import Any

# kind names understood by _check_kind
STEP_SPECS: dict[str, dict[str, Any]] = {
    "select": {"params": {"columns": "str_list"}, "required": ["columns"]},
    "rename": {
        "params": {"mapping": "dict", "columns": "dict"},
        "required_any": [("mapping", "columns")],
    },
    "drop_columns": {"params": {"columns": "str_list"}, "required": ["columns"]},
    "filter": {
        "params": {
            "column": "str",
            "operator": "str",
            "value": "any",
            "conditions": "list",
            "logic": "str",
        },
        "required_any": [("column", "conditions")],
    },
    "sort": {
        "params": {"by": "str_list", "columns": "str_list", "ascending": "bool_or_bool_list"},
        "required_any": [("by", "columns")],
    },
    "dedupe": {"params": {"subset": "str_list", "keep": "str"}},
    "fill_missing": {
        "params": {
            "values": "dict",
            "column": "str",
            "value": "any",
            "strategy": "str",
            "columns": "str_list",
        },
        "required_any": [("values", "column", "strategy")],
    },
    "drop_missing": {"params": {"subset": "str_list", "columns": "str_list", "how": "str"}},
    "convert_type": {
        "params": {"types": "dict", "column": "str", "to": "str", "errors": "str"},
        "required_any": [("types", "column")],
    },
    "date_normalize": {
        "params": {"column": "str", "columns": "str_list", "format": "str", "truncate": "str"},
        "required_any": [("column", "columns")],
    },
    "calculate": {
        "params": {"column": "str", "output": "str", "expression": "str"},
        "required": ["expression"],
        "required_any": [("column", "output")],
    },
    "group_aggregate": {
        "params": {
            "group_by": "str_list",
            "aggregations": "any",
            "aggregate": "any",
            "sort_by": "str_list",
        },
        "required_any": [("aggregations", "aggregate")],
    },
    "running_total": {
        "params": {"column": "str", "output": "str", "group_by": "str_list"},
        "required": ["column"],
    },
    "pct_change": {
        "params": {"column": "str", "output": "str", "periods": "int", "group_by": "str_list"},
        "required": ["column"],
    },
    "merge": {
        "params": {
            "other": "str",
            "dataset": "str",
            "on": "str_list",
            "left_on": "str_list",
            "right_on": "str_list",
            "how": "str",
            "validate": "str",
            "max_result_rows": "int",
        },
        "required_any": [("other", "dataset")],
    },
    "append": {
        "params": {"other": "str", "dataset": "str"},
        "required_any": [("other", "dataset")],
    },
    "limit": {"params": {"rows": "int", "n": "int"}, "required_any": [("rows", "n")]},
    "validate": {
        "params": {"required_columns": "str_list", "non_empty": "bool", "min_rows": "int"}
    },
    "describe": {"params": {"columns": "str_list"}},
    "data_quality": {"params": {"rules": "list"}, "required": ["rules"]},
}
ALIASES = {
    "drop_duplicates": "dedupe",
    "aggregate": "group_aggregate",
    "percentage_change": "pct_change",
    "concat": "append",
    "head": "limit",
    "quality": "data_quality",
}
for _alias, _canonical in ALIASES.items():
    STEP_SPECS[_alias] = STEP_SPECS[_canonical]

_CHOICES: dict[tuple[str, str], set[str]] = {
    ("filter", "logic"): {"and", "or"},
    ("date_normalize", "truncate"): {"month", "day"},
    ("drop_missing", "how"): {"any", "all"},
    ("merge", "how"): {"inner", "left", "right", "outer", "cross"},
    ("merge", "validate"): {"one_to_one", "one_to_many", "many_to_one", "many_to_many"},
    ("fill_missing", "strategy"): {"zero", "mean", "median", "ffill", "bfill"},
    ("convert_type", "errors"): {"coerce", "raise"},
}


def _check_kind(op: str, name: str, value: Any, kind: str) -> None:
    ok = True
    if kind == "str":
        ok = isinstance(value, str)
    elif kind == "str_list":
        ok = isinstance(value, str) or (
            isinstance(value, list) and all(isinstance(v, str) for v in value)
        )
    elif kind == "int":
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif kind == "bool":
        ok = isinstance(value, bool)
    elif kind == "bool_or_bool_list":
        ok = isinstance(value, bool) or (
            isinstance(value, list) and all(isinstance(v, bool) for v in value)
        )
    elif kind == "dict":
        ok = isinstance(value, dict)
    elif kind == "list":
        ok = isinstance(value, list)
    if not ok:
        raise ValueError(
            f"Step '{op}' parameter '{name}' must be {kind}, "
            f"got {type(value).__name__} ({value!r})."
        )


def validate_step_params(op: str, params: dict[str, Any]) -> None:
    """Raise ValueError (wrapped by pydantic) for any schema violation."""
    spec = STEP_SPECS.get(op)
    if spec is None:
        raise ValueError(
            f"Unknown processing step '{op}'. Supported: {', '.join(sorted(STEP_SPECS))}."
        )
    allowed: dict[str, str] = spec["params"]
    unknown = sorted(set(params) - set(allowed))
    if unknown:
        raise ValueError(
            f"Step '{op}' has unknown parameter(s): {', '.join(unknown)}. "
            f"Allowed: {', '.join(sorted(allowed))}."
        )
    for name in spec.get("required", []):
        if name not in params:
            raise ValueError(f"Step '{op}' requires parameter '{name}'.")
    for group in spec.get("required_any", []):
        if not any(name in params for name in group):
            raise ValueError(f"Step '{op}' requires one of: {', '.join(group)}.")
    for name, value in params.items():
        _check_kind(op, name, value, allowed[name])
        choices = _CHOICES.get((op, name)) or _CHOICES.get((ALIASES.get(op, op), name))
        if choices is not None and str(value).lower() not in choices:
            raise ValueError(
                f"Step '{op}' parameter '{name}' must be one of "
                f"{', '.join(sorted(choices))}, got {value!r}."
            )
    if op in {"data_quality", "quality"}:
        _validate_quality_rules(params.get("rules") or [])
    if op == "filter" and isinstance(params.get("conditions"), list):
        for condition in params["conditions"]:
            if not isinstance(condition, dict):
                raise ValueError("Filter conditions must be mappings with column/operator/value.")
            extra = sorted(set(condition) - {"column", "operator", "value"})
            if extra:
                raise ValueError(f"Filter condition has unknown key(s): {', '.join(extra)}.")


def _validate_quality_rules(rules: list) -> None:
    allowed_keys = {
        "column",
        "columns",
        "check",
        "rule",
        "on_error",
        "type",
        "as",
        "min",
        "max",
        "name",
    }
    for rule in rules:
        if not isinstance(rule, dict):
            raise ValueError("Data-quality rules must be mappings.")
        extra = sorted(set(rule) - allowed_keys)
        if extra:
            raise ValueError(f"Data-quality rule has unknown key(s): {', '.join(extra)}.")
        check = rule.get("check") or rule.get("rule")
        if check not in {"not_null", "type", "range", "unique"}:
            raise ValueError(
                f"Unknown data-quality check {check!r}. Use not_null, type, range, or unique."
            )
        if str(rule.get("on_error", "fail")).lower() not in {"fail", "drop", "coerce", "clamp"}:
            raise ValueError("Data-quality on_error must be fail, drop, coerce, or clamp.")
        if check == "type":
            if "as" not in rule and ("type" not in rule or rule.get("type") == "type"):
                # {check: type} without as:/type:<target> has no target type
                raise ValueError(
                    "A 'type' data-quality rule needs its target type, e.g. {check: type, as: int}."
                )
