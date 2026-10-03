"""Print each table's columns and what the agent detects as its time / device / id columns.

    python -m scripts.show_schema

Useful when the agent's tools or the UI don't pick up the right columns for your data.
"""
from __future__ import annotations

import agent  # noqa: F401  (loads .env)
from agent import db_access as db


def main() -> None:
    print(f"backend: {db.backend()}")
    for t in db.TABLES:
        cols = db.table_columns(t)
        n = db.query(f"SELECT count(*) AS n FROM {t}")[0]["n"]
        print(f"\n== {t} ({n} rows)")
        for name, dtype in cols:
            print(f"   {name:28s} {dtype}")
        print(f"   -> detected time column:   {db.time_column(t)}")
        print(f"   -> detected device column: {db.device_column(t)}")
    print(f"\nanomaly id column: {db.anomaly_id_column()}")
    row = db.query("SELECT * FROM detected_anomalies LIMIT 1")
    if row:
        print("\nexample anomaly row:")
        for k, v in row[0].items():
            print(f"   {k:28s} {str(v)[:80]}")


if __name__ == "__main__":
    main()
