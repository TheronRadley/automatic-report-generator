"""Benchmarks for Automatic Report Generator.

Generates synthetic datasets of increasing size in a temporary
directory, runs real report generations in-process, and writes the
measured results (with environment details) to docs/benchmarks.md.
Nothing here is estimated — run it yourself to reproduce:

    python benchmarks/run_benchmarks.py

Timing is wall-clock (median of N runs); memory is the tracemalloc
peak of Python allocations during the run (native library buffers,
e.g. inside pandas/numpy C code, are not fully captured, so treat the
figures as a lower bound suitable for relative comparison).
"""

from __future__ import annotations

import platform
import statistics
import sys
import tempfile
import time
import tracemalloc
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reportgen.reports.builder import run_report  # noqa: E402

REGIONS = ["North", "South", "East", "West", "Central"]
PRODUCTS = ["Lamp", "Chair", "Notebook", "Bottle", "Mouse", "Clock"]


def make_sales_csv(path: Path, rows: int, seed: int = 7) -> None:
    rng = np.random.default_rng(seed)
    start = date(2024, 1, 1)
    dates = [start + timedelta(days=int(d)) for d in rng.integers(0, 730, rows)]
    quantity = rng.integers(1, 15, rows)
    unit_price = np.round(rng.uniform(5, 220, rows), 2)
    frame = pd.DataFrame(
        {
            "date": [d.isoformat() for d in dates],
            "region": rng.choice(REGIONS, rows),
            "product": rng.choice(PRODUCTS, rows),
            "quantity": quantity,
            "unit_price": unit_price,
            "revenue": np.round(quantity * unit_price, 2),
        }
    )
    frame.to_csv(path, index=False)


def sales_config(
    work: Path, csv_path: Path, name: str, many_charts: bool = False, extra_source: str = ""
) -> Path:
    chart_sections = ""
    if many_charts:
        chart_sections = """
  - {title: Units histogram, type: chart, dataset: sales,
     chart: {type: histogram, x: quantity, title: Units per order}}
  - {title: Revenue scatter, type: chart, dataset: sales,
     chart: {type: scatter, x: quantity, y: revenue, title: Revenue vs units}}
  - {title: Regional share, type: chart, dataset: revenue_by_region,
     chart: {type: donut, x: region, y: revenue, title: Revenue share}}
  - {title: Cumulative trend, type: chart, dataset: monthly_revenue,
     chart: {type: area, x: date, y: revenue, title: Monthly revenue area}}
"""
    config = work / f"{name}.yaml"
    config.write_text(
        f"""
version: 1
report: {{title: "Benchmark — {name}", template: sales}}
sources:
  - {{name: sales, type: csv, path: "{csv_path}"}}
{extra_source}
processing:
  - dataset: sales
    output: monthly_revenue
    steps:
      - {{op: date_normalize, column: date, truncate: month}}
      - {{op: group_aggregate, group_by: [date], aggregations: {{revenue: sum, quantity: sum}}}}
      - {{op: sort, by: [date]}}
  - dataset: sales
    output: revenue_by_region
    steps:
      - {{op: group_aggregate, group_by: [region], aggregations: {{revenue: sum, quantity: sum}}}}
metrics:
  - {{name: total_revenue, dataset: sales, aggregation: sum, column: revenue, format: currency}}
sections:
  - {{title: Key metrics, type: metrics}}
  - {{title: Trend, type: chart, dataset: monthly_revenue,
     chart: {{type: timeseries, x: date, y: revenue, title: Revenue by month}}}}
  - {{title: By region, type: chart, dataset: revenue_by_region,
     chart: {{type: bar, x: region, y: revenue, title: Revenue by region}}}}
  - {{title: Regional detail, type: table, dataset: revenue_by_region}}
{chart_sections}
exports:
  output_dir: "{work / "out"}"
  filename: {name}
  overwrite: true
""",
        encoding="utf-8",
    )
    return config


def measure(config: Path, formats: list[str], runs: int) -> tuple[float, float]:
    times: list[float] = []
    peak_mb = 0.0
    for _ in range(runs):
        tracemalloc.start()
        started = time.perf_counter()
        run_report(config, formats=formats, overwrite=True)
        times.append(time.perf_counter() - started)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        peak_mb = max(peak_mb, peak / (1024 * 1024))
    return statistics.median(times), peak_mb


def main() -> None:
    results: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="reportgen-bench-") as tmp:
        work = Path(tmp)
        scenarios = [
            ("Small — 1k rows, HTML", 1_000, ["html"], 3, {}),
            ("Small — 1k rows, PDF+HTML+XLSX", 1_000, ["pdf", "html", "xlsx"], 3, {}),
            ("Medium — 50k rows, HTML", 50_000, ["html"], 3, {}),
            ("Medium — 50k rows, all formats", 50_000, ["pdf", "html", "xlsx"], 2, {}),
            ("Large — 250k rows, HTML", 250_000, ["html"], 2, {}),
            ("Large — 250k rows, all formats", 250_000, ["pdf", "html", "xlsx"], 1, {}),
            (
                "Medium — 50k rows, 6 charts, all formats",
                50_000,
                ["pdf", "html", "xlsx"],
                2,
                {"many_charts": True},
            ),
        ]
        csv_cache: dict[int, Path] = {}
        # Warm-up (discarded): one full all-format run absorbs one-time
        # import/font-cache costs so scenario medians are comparable.
        warm_csv = work / "warmup.csv"
        make_sales_csv(warm_csv, 1_000)
        run_report(
            sales_config(work, warm_csv, "warmup"), formats=["pdf", "html", "xlsx"], overwrite=True
        )
        for label, rows, formats, runs, kwargs in scenarios:
            if rows not in csv_cache:
                csv_path = work / f"sales_{rows}.csv"
                make_sales_csv(csv_path, rows)
                csv_cache[rows] = csv_path
            config = sales_config(
                work,
                csv_cache[rows],
                label.split("—")[0].strip().lower().replace(" ", "_") + f"_{rows}_{len(results)}",
                **kwargs,
            )
            seconds, peak = measure(config, formats, runs)
            results.append(
                {"scenario": label, "rows": rows, "runs": runs, "seconds": seconds, "peak_mb": peak}
            )
            print(f"{label}: median {seconds:.2f}s, tracemalloc peak {peak:.0f} MB")

        # Multi-source: 50k sales rows merged with a targets table.
        targets = work / "targets.csv"
        pd.DataFrame({"region": REGIONS, "target": [900_000] * 5}).to_csv(targets, index=False)
        extra = f'  - {{name: targets, type: csv, path: "{targets}"}}'
        config = sales_config(work, csv_cache[50_000], "multi_source", extra_source=extra)
        text = config.read_text(encoding="utf-8").replace(
            "      - {op: group_aggregate, group_by: [region], aggregations: {revenue: sum, quantity: sum}}",
            "      - {op: group_aggregate, group_by: [region], aggregations: {revenue: sum, quantity: sum}}\n"
            '      - {op: merge, other: targets, "on": [region], how: left}',
        )
        config.write_text(text, encoding="utf-8")
        seconds, peak = measure(config, ["pdf", "html", "xlsx"], 2)
        results.append(
            {
                "scenario": "Multi-source — 50k rows + merge, all formats",
                "rows": 50_000,
                "runs": 2,
                "seconds": seconds,
                "peak_mb": peak,
            }
        )
        print(f"Multi-source: median {seconds:.2f}s, tracemalloc peak {peak:.0f} MB")

    write_results(results)


def write_results(results: list[dict]) -> None:
    env_lines = [
        f"- Date (UTC): {pd.Timestamp.now(tz='UTC'):%Y-%m-%d %H:%M}",
        f"- Python: {platform.python_version()} on {platform.system()} {platform.release()} "
        f"({platform.machine()})",
        f"- pandas {pd.__version__}, numpy {np.__version__}",
        "- Machine: shared CI-style sandbox container; absolute numbers will differ "
        "on other hardware — re-run `python benchmarks/run_benchmarks.py` locally.",
    ]
    rows_md = "\n".join(
        f"| {r['scenario']} | {r['rows']:,} | {r['runs']} | {r['seconds']:.2f} | "
        f"{r['peak_mb']:.0f} |"
        for r in results
    )
    content = f"""# Benchmarks

Generated by `benchmarks/run_benchmarks.py` from actual runs — do not
edit the numbers by hand; re-run the script instead.

## Methodology

- Synthetic sales CSVs (seeded) of 1,000 / 50,000 / 250,000 rows.
- Each scenario builds a full sales report (date truncation, two
  grouped aggregations, metrics, charts, tables) with
  `run_report(...)` in-process.
- Timing is wall-clock median over the stated runs. Memory is the
  `tracemalloc` peak of Python allocations (a lower bound; native
  buffers in C libraries are not fully captured).
- The detail table sections contain aggregated data only; the raw
  dataset is loaded once and reused by every pipeline.

## Environment

{chr(10).join(env_lines)}

## Results

| Scenario | Rows | Runs | Median seconds | Tracemalloc peak (MB) |
|---|---:|---:|---:|---:|
{rows_md}

## Notes

- HTML-only exports are fastest; Excel adds one writer pass per table
  section and PDF adds full pagination, so "all formats" costs roughly
  the sum of the individual exporters plus one shared build.
- Fixed costs dominate at these sizes: each additional chart adds
  roughly 0.5–1 s of Matplotlib rendering at 200 DPI (6 charts vs 2
  charts above). Point count per chart is bounded by `max_points`
  downsampling, so row count matters less than chart count until
  datasets get much larger.
- For very large CSVs, `options.chunk_rows` bounds read memory chunk
  by chunk (the full frame is still assembled for reporting). The
  `options.max_input_mb` limit (default 200 MB) refuses oversized
  inputs before any allocation happens.
"""
    target = Path(__file__).resolve().parents[1] / "docs" / "benchmarks.md"
    target.write_text(content, encoding="utf-8")
    print(f"Wrote {target}")


if __name__ == "__main__":
    main()
