"""Reusable chart rendering.

Charts never invent data: rows with missing x/y values are dropped for
that chart only, and the number dropped is reported back so the report
builder can surface it as a warning.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # headless: no display required
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.ticker as mticker  # noqa: E402
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


@dataclass
class ChartResult:
    path: Path
    title: str
    rows_used: int
    rows_dropped: int


def _theme(name: str) -> dict:
    return THEMES.get(name, THEMES["default"])


def _prepare(frame: pd.DataFrame, spec: ChartConfig) -> tuple[pd.DataFrame, int, list[str]]:
    """Validate columns, drop missing values, bound the row count."""
    y_columns = [] if spec.y is None else [spec.y] if isinstance(spec.y, str) else list(spec.y)
    needed = [c for c in [spec.x, *y_columns, spec.group_by] if c]
    if spec.type == "histogram":
        if not (spec.x or y_columns):
            raise ChartError("Histogram charts need 'x' (the numeric column).")
        needed = [spec.x or y_columns[0]]
    elif spec.type in {"pie", "donut"}:
        if not spec.x or not y_columns:
            raise ChartError("Pie/donut charts need 'x' (labels) and 'y' (values).")
    elif spec.type == "scatter":
        if not spec.x or not y_columns:
            raise ChartError("Scatter charts need numeric 'x' and 'y'.")
    else:
        if not spec.x or not y_columns:
            raise ChartError(f"{spec.type} charts need 'x' and 'y'.")
    require_columns(frame, needed, f"Chart '{spec.title or spec.type}'")
    data = frame.copy()
    if spec.type in {"timeseries", "line", "area"} and spec.x:
        parsed = pd.to_datetime(data[spec.x], errors="coerce")
        if parsed.notna().mean() > 0.8:  # mostly dates -> use a date axis
            data[spec.x] = parsed
    before = len(data)
    data = data.dropna(subset=needed)
    dropped = before - len(data)
    for col in y_columns:
        data[col] = pd.to_numeric(data[col], errors="coerce")
    data = data.dropna(subset=y_columns) if y_columns else data
    dropped = before - len(data)
    if data.empty:
        raise ChartError(
            f"Chart '{spec.title or spec.type}' has no plottable rows after "
            "removing missing values. Check the source data."
        )
    if len(data) > spec.max_points and spec.type not in {"histogram"}:
        # Deterministic downsampling keeps charts readable and honest.
        data = data.iloc[:: max(1, len(data) // spec.max_points)]
    return data, dropped, y_columns


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
    data, dropped, y_columns = _prepare(frame, spec)

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
        _draw(ax, data, spec, y_columns, palette)
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
        rows_used=len(data),
        rows_dropped=dropped,
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
    if spec.x and pd.api.types.is_datetime64_any_dtype(data[spec.x]):
        ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=3, maxticks=8))
        ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(ax.xaxis.get_major_locator()))
    else:
        plt.setp(ax.get_xticklabels(), rotation=30, ha="right")


def _draw(ax, data: pd.DataFrame, spec: ChartConfig, y_columns: list[str], palette) -> None:
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
        return
    if chart_type in {"pie", "donut"}:
        subset = data.groupby(x, as_index=False)[y_columns[0]].sum()
        subset = subset.sort_values(y_columns[0], ascending=False).head(12)
        if (subset[y_columns[0]] < 0).any():
            raise ChartError("Pie/donut charts cannot represent negative values.")
        wedge = (
            {"width": 0.45, "edgecolor": "white"}
            if chart_type == "donut"
            else {"edgecolor": "white"}
        )
        ax.pie(
            subset[y_columns[0]],
            labels=[str(v) for v in subset[x]],
            autopct="%1.1f%%",
            colors=palette * 3,
            wedgeprops=wedge,
            textprops={"fontsize": 9},
        )
        ax.set_ylabel("")
        return
    if chart_type == "scatter":
        ax.scatter(data[x], data[y_columns[0]], s=18, color=palette[0], alpha=0.75)
        return
    if chart_type in {"bar", "hbar"}:
        subset = data
        if not pd.api.types.is_numeric_dtype(data[x]) and data[x].nunique() > MAX_CATEGORIES:
            top = (
                data.groupby(x)[y_columns[0]]
                .sum()
                .sort_values(ascending=False)
                .head(MAX_CATEGORIES)
                .index
            )
            subset = data[data[x].isin(top)]
        labels = [str(v) for v in subset[x]]
        for index, y_col in enumerate(y_columns):
            color = palette[index % len(palette)]
            if chart_type == "bar":
                ax.bar(labels, subset[y_col], label=y_col, color=color)
            else:
                ax.barh(labels, subset[y_col], label=y_col, color=color)
        return
    # line / area / timeseries, optionally split by group_by
    if spec.group_by:
        groups = sorted(data[spec.group_by].dropna().unique(), key=str)[:8]
        for index, group in enumerate(groups):
            part = data[data[spec.group_by] == group].sort_values(x)
            y_col = y_columns[0]
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
        return
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
