"""
migrate_deal_financials.py
Adds the Revenue & Profit Management System columns to the `deals` table.

Use this if you're running on a local SQLite database (Caryanams.db) and the
app's automatic startup migration (which targets PostgreSQL) didn't pick up
these columns. Safe to run multiple times — skips columns that already exist.

    python migrate_deal_financials.py
"""
import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), 'Caryanams.db')

COLUMNS = [
    ("purchase_price",      "FLOAT DEFAULT 0"),
    ("transportation_cost", "FLOAT DEFAULT 0"),
    ("repair_cost",         "FLOAT DEFAULT 0"),
    ("registration_cost",   "FLOAT DEFAULT 0"),
    ("marketing_cost",      "FLOAT DEFAULT 0"),
    ("total_cost",          "FLOAT DEFAULT 0"),
    ("other_expenses",      "FLOAT DEFAULT 0"),
    ("gross_profit",        "FLOAT DEFAULT 0"),
    ("net_profit",          "FLOAT DEFAULT 0"),
]


def run():
    if not os.path.exists(DB_PATH):
        print(f"Database not found at {DB_PATH} — nothing to migrate (a fresh DB will get these columns automatically).")
        return

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(deals)")
    existing = {row[1] for row in cur.fetchall()}
    for col_name, col_def in COLUMNS:
        if col_name not in existing:
            cur.execute(f"ALTER TABLE deals ADD COLUMN {col_name} {col_def}")
            print(f"  Added column: {col_name}")
        else:
            print(f"  Already exists: {col_name}")

    # Backfill total_cost / gross_profit / net_profit for any existing deals
    # so old records show correct numbers immediately (Total Cost = sum of
    # cost fields, Gross Profit = final_price - total_cost, Net Profit =
    # Gross Profit - other_expenses).
    cur.execute("""
        UPDATE deals
        SET total_cost = COALESCE(purchase_price,0) + COALESCE(transportation_cost,0)
                        + COALESCE(repair_cost,0) + COALESCE(registration_cost,0)
                        + COALESCE(marketing_cost,0)
    """)
    cur.execute("""
        UPDATE deals
        SET gross_profit = COALESCE(final_price,0) - COALESCE(total_cost,0)
    """)
    cur.execute("""
        UPDATE deals
        SET net_profit = COALESCE(gross_profit,0) - COALESCE(other_expenses,0)
    """)

    conn.commit()
    conn.close()
    print("Migration complete.")


if __name__ == '__main__':
    run()
