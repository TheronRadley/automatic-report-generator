"""Reusable chart rendering.

Accuracy rules:

- Charts never invent data: rows with missing x/y values are dropped
  for that chart only, and the count is reported back.
- Categorical charts (bar/pie/donut) are **aggregated before** any
  reduction, so totals and percentages always reflect the full data.
  Pie/donut slices beyond the display limit are combined into an
  explicit "Other" slice; omitted bar categories and line groups are
  disclosed via ``ChartResult.notes`` (surfaced as report warnings).
- Point-based charts are downsampled to at most ``max_points`` drawn
  points (exactly), with the reduction disclosed in ``notes``.
- A numeric x axis is never reinterpreted as dates.
- Grouping/extra series are either drawn or rejected with a clear
  error — never silently ignored.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # headless: no display required
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.ticker as mticker  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from reportgen.config.models import ChartConfig  # noqa: E402
from reportgen.errors import ChartError  # noqa: E402
from reportgen.processing.validation import require_columns  # noqa: E402

THEMES: dict[str, dict] = {
    "default": {
        "palette": ["#2f4b7c", "#665191", "#a0512d", "#487a57", "#7a6a53", "#5b7d99"],
        "grid": "#d9d9d9",
        "text": "#1a1a1a",
        "background": "white",
    },
    "muted": {
        "palette": ["#6b7f94", "#8a9a7b", "#b08d6e", "#7d7d8c", "#5f7d7d", "#9486a0"],
        "grid": "#e3e3e3",
        "text": "#262626",
        "background": "white",
    },
    "contrast": {
        "palette": ["#003f7f", "#b3001b", "#1b7a3d", "#5b2a86", "#b35c00", "#0f766e"],
        "grid": "#c9c9c9",
        "text": "#000000",
        "background": "white",
    },
}

MAX_CATEGORIES = 30
MAX_PIE_SLICES = 12
MAX_GROUPS = 8
_CATEGORICAL = {"bar", "hbar", "pie", "donut"}
_SINGLE_Y = {"pie", "donut", "scatter", "histogram"}


@dataclass
class ChartResult:
    path: Path
    title: str
    rows_used: int  # source rows represented (after missing-value drops)
    rows_dropped: int
    notes: list[str] = field(default_factory=list)  # visible reduction disclosures


def _theme(name: str) -> dict:
    return THEMES.get(name, THEMES["default"])


def _grouped_bar_offsets(n_series: int) -> tuple[float, list[float]]:
    """(bar width, per-series centre offsets) so grouped bars sit side
    by side instead of overlapping at the same position."""
    total = 0.8
    width = total / max(1, n_series)
    start = -total / 2 + width / 2
    return width, [start + i * width for i in range(n_series)]


def _validate_spec(spec: ChartConfig) -> list[str]:
    y_columns = [] if spec.y is None else [spec.y] if isinstance(spec.y, str) else list(spec.y)
    if spec.type == "histogram":
        if not (spec.x or y_columns):
            raise ChartError("Histogram charts need 'x' (the numeric column).")
    elif spec.type in _CATEGORICAL or spec.type == "scatter":
        if not spec.x or not y_columns:
            raise ChartError(f"{spec.type} charts need 'x' and 'y'.")
    else:
        if not spec.x or not y_columns:
            raise ChartError(f"{spec.type} charts need 'x' and 'y'.")
    if spec.type in _SINGLE_Y and len(y_columns) > 1:
        raise ChartError(
            f"{spec.type} charts take exactly one 'y' column, got {y_columns}. "
            "Use separate chart sections for additional series."
        )
    if spec.group_by and spec.type in {"pie", "donut", "histogram"}:
        raise ChartError(f"{spec.type} charts do not support 'group_by'.")
    if spec.group_by and len(y_columns) > 1:
        raise ChartError(
            "Charts support either multiple 'y' series or one 'y' split by "
            "'group_by', not both at once."
        )
    return y_columns


def _maybe_parse_dates(data: pd.DataFrame, spec: ChartConfig) -> pd.DataFrame:
    """Parse a *text/datetime* x column to dates for time-oriented charts.
    Numeric columns are numeric axes and are never reinterpreted."""
    if spec.type not in {"timeseries", "line", "area"} or not spec.x:
        return data
    series = data[spec.x]
    if pd.api.types.is_datetime64_any_dtype(series):
        return data
    if pd.api.types.is_numeric_dtype(series):
        return data
    parsed = pd.to_datetime(series, errors="coerce")
    if parsed.notna().mean() > 0.8:
        data = data.copy()
        data[spec.x] = parsed
    return data


def _aggregate_categories(
    data: pd.DataFrame, spec: ChartConfig, y_columns: list[str], notes: list[str]
) -> pd.DataFrame:
    """Aggregate full data by category (first-appearance order), then
    apply disclosed display reductions."""
    keys = [spec.x] + ([spec.group_by] if spec.group_by else [])
    aggregated = (
        data.groupby(keys, dropna=False, sort=False)[y_columns].sum(numeric_only=True).reset_index()
    )
    title = spec.title or spec.type
    if spec.type in {"pie", "donut"}:
        y_col = y_columns[0]
        if (aggregated[y_col] < 0).any():
            raise ChartError("Pie/donut charts cannot represent negative values.")
        if len(aggregated) > MAX_PIE_SLICES:
            ordered = aggregated.sort_values(y_col, ascending=False)
            kept = ordered.head(MAX_PIE_SLICES - 1)
            rest = ordered.iloc[MAX_PIE_SLICES - 1 :]
            other_value = float(rest[y_col].sum())
            other = pd.DataFrame([{spec.x: "Other", y_col: other_value}])
            aggregated = pd.concat([kept, other], ignore_index=True)
            notes.append(
                f"Chart '{title}': {len(rest)} smaller categories are combined "
                f"into 'Other' so percentages cover all data."
            )
        return aggregated
    # bar / hbar: cap distinct x categories, keeping the largest.
    categories = aggregated[spec.x].dropna().unique().tolist()
    if len(categories) > MAX_CATEGORIES:
        totals = aggregated.groupby(spec.x, dropna=False)[y_columns[0]].sum()
        top = totals.sort_values(ascending=False).head(MAX_CATEGORIES).index
        aggregated = aggregated[aggregated[spec.x].isin(top)]
        notes.append(
            f"Chart '{title}': shows the top {MAX_CATEGORIES} of "
            f"{len(categories)} categories by {y_columns[0]}; "
            f"{len(categories) - MAX_CATEGORIES} categories are omitted."
        )
    return aggregated


def _downsample(data: pd.DataFrame, spec: ChartConfig, notes: list[str]) -> pd.DataFrame:
    if spec.type in _CATEGORICAL or spec.type == "histogram":
        return data
    if len(data) <= spec.max_points:
        return data
    step = math.ceil(len(data) / spec.max_points)
    sampled = data.iloc[::step]
    notes.append(
        f"Chart '{spec.title or spec.type}': displays {len(sampled):,} of "
        f"{len(data):,} points (max_points={spec.max_points:,}); "
        "tables and metrics still use all data."
    )
    return sampled


def _prepare(
    frame: pd.DataFrame, spec: ChartConfig
) -> tuple[pd.DataFrame, int, int, list[str], list[str]]:
    """Validate, clean, aggregate/sample. Returns (data, rows_used,
    rows_dropped, y_columns, notes)."""
    notes: list[str] = []
    y_columns = _validate_spec(spec)
    if spec.type == "histogram":
        needed = [spec.x or y_columns[0]]
    else:
        needed = [c for c in [spec.x, *y_columns, spec.group_by] if c]
    require_columns(frame, needed, f"Chart '{spec.title or spec.type}'")
    data = frame.copy()
    before = len(data)
    data = data.dropna(subset=needed)
    for col in y_columns:
        data[col] = pd.to_numeric(data[col], errors="coerce")
    if y_columns:
        data = data.dropna(subset=y_columns)
    rows_used = len(data)
    dropped = before - rows_used
    if data.empty:
        raise ChartError(
            f"Chart '{spec.title or spec.type}' has no plottable rows after "
            "removing missing values. Check the source data."
        )
    data = _maybe_parse_dates(data, spec)
    if spec.type in _CATEGORICAL:
        data = _aggregate_categories(data, spec, y_columns, notes)
    else:
        data = _downsample(data, spec, notes)
    return data, rows_used, dropped, y_columns, notes


def render_chart(
    frame: pd.DataFrame,
    spec: ChartConfig,
    output_path: Path,
    theme_name: str = "default",
    dpi: int = 200,
) -> ChartResult:
    """Render one chart to a PNG file and return its metadata."""
    theme = _theme(theme_name)
    palette = theme["palette"]
    data, rows_used, dropped, y_columns, notes = _prepare(frame, spec)

    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "text.color": theme["text"],
            "axes.labelcolor": theme["text"],
            "axes.edgecolor": "#888888",
            "xtick.color": theme["text"],
            "ytick.color": theme["text"],
            "font.size": 10,
        }
    )
    fig, ax = plt.subplots(figsize=(spec.width, spec.height), facecolor=theme["background"])
    ax.set_facecolor(theme["background"])
    try:
        notes.extend(_draw(ax, data, spec, y_columns, palette))
        title = spec.title or spec.type.replace("_", " ").title()
        ax.set_title(title, loc="left", fontsize=12, fontweight="bold", pad=10)
        if spec.type not in {"pie", "donut"}:
            ax.set_xlabel(spec.x_label or (spec.x or ""))
            ax.set_ylabel(spec.y_label or (", ".join(y_columns)))
            ax.grid(True, axis="y", color=theme["grid"], linewidth=0.6)
            ax.set_axisbelow(True)
            for spine in ("top", "right"):
                ax.spines[spine].set_visible(False)
            ax.yaxis.set_major_formatter(mticker.FuncFormatter(_format_axis))
            _format_date_axis(ax, data, spec)
        if spec.legend and (len(y_columns) > 1 or spec.group_by or spec.type in {"pie", "donut"}):
            ax.legend(frameon=False, loc="best", fontsize=9)
        fig.tight_layout()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(output_path, dpi=dpi, format="png")
    except ChartError:
        raise
    except Exception as exc:
        raise ChartError(f"Failed to render chart '{spec.title or spec.type}': {exc}") from exc
    finally:
        plt.close(fig)
    return ChartResult(
        path=output_path,
        title=spec.title or spec.type,
        rows_used=rows_used,
        rows_dropped=dropped,
        notes=notes,
    )


def _format_axis(value: float, _pos: int) -> str:
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:,.1f}M"
    if abs(value) >= 10_000:
        return f"{value / 1_000:,.0f}k"
    if float(value).is_integer():
        return f"{value:,.0f}"
    return f"{value:,.1f}"


def _format_date_axis(ax, data: pd.DataFrame, spec: ChartConfig) -> None:
    if spec.x and spec.x in data and pd.api.types.is_datetime64_any_dtype(data[spec.x]):
        ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=3, maxticks=8))
        ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(ax.xaxis.get_major_locator()))
    else:
        plt.setp(ax.get_xticklabels(), rotation=30, ha="right")


def _limited_groups(values: list, spec: ChartConfig, notes: list[str]) -> list:
    ordered = sorted(values, key=str)
    if len(ordered) > MAX_GROUPS:
        notes.append(
            f"Chart '{spec.title or spec.type}': shows {MAX_GROUPS} of "
            f"{len(ordered)} groups ({spec.group_by}); the rest are omitted "
            "from the chart but included in tables/metrics."
        )
        return ordered[:MAX_GROUPS]
    return ordered


def _draw(ax, data: pd.DataFrame, spec: ChartConfig, y_columns: list[str], palette) -> list[str]:
    notes: list[str] = []
    chart_type = spec.type
    x = spec.x
    if chart_type == "histogram":
        column = x or y_columns[0]
        values = pd.to_numeric(data[column], errors="coerce").dropna()
        if values.empty:
            raise ChartError(f"Histogram column '{column}' has no numeric values.")
        ax.hist(
            values,
            bins=min(30, max(5, len(values) // 10 or 5)),
            color=palette[0],
            edgecolor="white",
            linewidth=0.5,
        )
        ax.set_xlabel(spec.x_label or column)
        return notes
    if chart_type in {"pie", "donut"}:
        y_col = y_columns[0]
        wedge = (
            {"width": 0.45, "edgecolor": "white"}
            if chart_type == "donut"
            else {"edgecolor": "white"}
        )
        ax.pie(
            data[y_col],
            labels=[str(v) for v in data[x]],
            autopct="%1.1f%%",
            colors=palette * 3,
            wedgeprops=wedge,
            textprops={"fontsize": 9},
        )
        ax.set_ylabel("")
        return notes
    if chart_type == "scatter":
        y_col = y_columns[0]
        if spec.group_by:
            groups = _limited_groups(data[spec.group_by].dropna().unique().tolist(), spec, notes)
            for index, group in enumerate(groups):
                part = data[data[spec.group_by] == group]
                ax.scatter(
                    part[x],
                    part[y_col],
                    s=18,
                    alpha=0.75,
                    color=palette[index % len(palette)],
                    label=str(group),
                )
        else:
            ax.scatter(data[x], data[y_col], s=18, color=palette[0], alpha=0.75)
        return notes
    if chart_type in {"bar", "hbar"}:
        _draw_bars(ax, data, spec, y_columns, palette, horizontal=chart_type == "hbar")
        return notes
    # line / area / timeseries
    if spec.group_by:
        groups = _limited_groups(data[spec.group_by].dropna().unique().tolist(), spec, notes)
        y_col = y_columns[0]
        for index, group in enumerate(groups):
            part = data[data[spec.group_by] == group].sort_values(x)
            color = palette[index % len(palette)]
            if chart_type == "area":
                ax.fill_between(part[x], part[y_col], alpha=0.35, color=color, label=str(group))
                ax.plot(part[x], part[y_col], color=color, linewidth=1.4)
            else:
                ax.plot(
                    part[x],
                    part[y_col],
                    color=color,
                    linewidth=1.6,
                    marker="o",
                    markersize=3,
                    label=str(group),
                )
        return notes
    ordered = data.sort_values(x) if pd.api.types.is_datetime64_any_dtype(data[x]) else data
    for index, y_col in enumerate(y_columns):
        color = palette[index % len(palette)]
        if chart_type == "area":
            ax.fill_between(ordered[x], ordered[y_col], alpha=0.35, color=color, label=y_col)
            ax.plot(ordered[x], ordered[y_col], color=color, linewidth=1.4)
        else:
            ax.plot(
                ordered[x],
                ordered[y_col],
                color=color,
                linewidth=1.6,
                marker="o",
                markersize=3,
                label=y_col,
            )
    return notes


def _draw_bars(ax, data, spec, y_columns, palette, horizontal: bool) -> None:
    """Grouped bars: one cluster per category, series side by side."""
    x = spec.x
    if spec.group_by:
        pivot = data.pivot_table(
            index=x, columns=spec.group_by, values=y_columns[0], aggfunc="sum", sort=False
        )
        categories = [str(v) for v in pivot.index.tolist()]
        series = [(str(col), pivot[col].tolist()) for col in pivot.columns]
    else:
        categories = [str(v) for v in data[x].tolist()]
        series = [(y_col, data[y_col].tolist()) for y_col in y_columns]
    positions = np.arange(len(categories), dtype=float)
    width, offsets = _grouped_bar_offsets(len(series))
    for index, (label, values) in enumerate(series):
        color = palette[index % len(palette)]
        shifted = positions + offsets[index]
        if horizontal:
            ax.barh(shifted, values, height=width, label=label, color=color)
        else:
            ax.bar(shifted, values, width=width, label=label, color=color)
    if horizontal:
        ax.set_yticks(positions)
        ax.set_yticklabels(categories)
        ax.invert_yaxis()
    else:
        ax.set_xticks(positions)
        ax.set_xticklabels(categories)
