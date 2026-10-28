"""Regenerate every synthetic example dataset deterministically.

All data is SYNTHETIC (invented for demonstration) and seeded, so the
files in examples/data/ are reproducible byte-for-byte apart from the
xlsx/sqlite binary containers.

Usage: python examples/generate_demo_data.py
"""

from __future__ import annotations

import json
import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).parent / "data"
SEED = 20260115


def sales_frame() -> pd.DataFrame:
    rng = random.Random(SEED)
    products = [
        ("Aurora Desk Lamp", "Lighting", 49.99),
        ("Nimbus Office Chair", "Furniture", 189.50),
        ("Vertex Notebook A5", "Stationery", 7.25),
        ("Atlas Water Bottle", "Accessories", 18.90),
        ("Echo Wireless Mouse", "Electronics", 34.99),
        ("Quartz Wall Clock", "Decor", 59.00),
    ]
    regions = ["North", "South", "East", "West", "Central"]
    rows = []
    start = date(2025, 1, 1)
    order_id = 1000
    for day_offset in range(365):
        current = start + timedelta(days=day_offset)
        # A few orders most days, with a mild upward trend — synthetic.
        orders_today = rng.randint(1, 3) + (1 if day_offset > 270 else 0)
        for _ in range(orders_today):
            product, category, base_price = rng.choice(products)
            quantity = rng.randint(1, 12)
            unit_price = round(base_price * rng.uniform(0.92, 1.08), 2)
            order_id += 1
            rows.append(
                {
                    "order_id": order_id,
                    "date": current.isoformat(),
                    "region": rng.choice(regions),
                    "product": product,
                    "category": category,
                    "quantity": quantity,
                    "unit_price": unit_price,
                    "revenue": round(quantity * unit_price, 2),
                }
            )
    return pd.DataFrame(rows)


def business_frame() -> pd.DataFrame:
    rng = random.Random(SEED + 1)
    departments = ["Retail", "Wholesale", "Online", "Services"]
    rows = []
    for month in range(1, 13):
        for department in departments:
            revenue = round(rng.uniform(18000, 95000), 2)
            expenses = round(revenue * rng.uniform(0.55, 0.9), 2)
            rows.append(
                {
                    "date": f"2025-{month:02d}-01",
                    "department": department,
                    "revenue": revenue,
                    "expenses": expenses,
                }
            )
    return pd.DataFrame(rows)


def financial_frame() -> pd.DataFrame:
    rng = random.Random(SEED + 2)
    categories = ["Sales income", "Consulting", "Payroll", "Hosting", "Office", "Travel"]
    rows = []
    start = date(2025, 1, 1)
    for index in range(240):
        category = rng.choice(categories)
        income = category in {"Sales income", "Consulting"}
        amount = round(rng.uniform(120, 8500), 2) * (1 if income else -1)
        rows.append(
            {
                "date": (start + timedelta(days=rng.randint(0, 364))).isoformat(),
                "category": category,
                "account": "Operating" if index % 4 else "Savings",
                "amount": amount,
            }
        )
    frame = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    return frame


def operations_frame() -> pd.DataFrame:
    rng = random.Random(SEED + 3)
    sites = ["Plant A", "Plant B", "Plant C"]
    rows = []
    start = date(2025, 1, 1)
    for day_offset in range(120):
        current = start + timedelta(days=day_offset)
        for site in sites:
            output = rng.randint(620, 1400)
            rows.append(
                {
                    "date": current.isoformat(),
                    "site": site,
                    "output_units": output,
                    "defects": max(0, int(output * rng.uniform(0.002, 0.02))),
                    "downtime_hours": round(rng.uniform(0, 3.5), 2),
                }
            )
    return pd.DataFrame(rows)


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    sales = sales_frame()
    sales.to_csv(DATA_DIR / "synthetic_sales.csv", index=False)
    business_frame().to_csv(DATA_DIR / "business_performance.csv", index=False)
    financial_frame().to_csv(DATA_DIR / "financial_summary.csv", index=False)
    operations_frame().to_csv(DATA_DIR / "operations.csv", index=False)

    # JSON example: nested document + a JSON Lines file.
    records = sales.head(60).to_dict(orient="records")
    (DATA_DIR / "orders.json").write_text(
        json.dumps({"meta": {"synthetic": True}, "orders": records}, indent=2),
        encoding="utf-8",
    )
    with (DATA_DIR / "orders.jsonl").open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")

    # Excel example: two worksheets.
    with pd.ExcelWriter(DATA_DIR / "inventory.xlsx", engine="openpyxl") as writer:
        sales.groupby("product", as_index=False).agg(
            units=("quantity", "sum"), revenue=("revenue", "sum")
        ).to_excel(writer, sheet_name="By product", index=False)
        sales.head(80).to_excel(writer, sheet_name="Sample orders", index=False)

    # SQLite example.
    db_path = DATA_DIR / "sales.sqlite"
    db_path.unlink(missing_ok=True)
    with sqlite3.connect(db_path) as conn:
        sales.to_sql("sales", conn, index=False)
    print(f"Wrote synthetic example data to {DATA_DIR} ({len(sales)} sales rows)")


if __name__ == "__main__":
    main()
