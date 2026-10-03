"""Build a local DuckDB copy of the network_rca database, for testing without the container.

Choose ONE source:

  # 1. From the challenge's seed CSVs (recommended - same data the graders use; files are only read)
  python -m scripts.build_duckdb --from-csv ../db/init

  # 2. Snapshot the running Postgres container once, then work offline
  #    (uses the PG* / DATABASE_URL settings from .env)
  python -m scripts.build_duckdb --from-postgres

  # 3. A small SYNTHETIC demo dataset (NOT the challenge data) for smoke-testing the agent
  python -m scripts.build_duckdb --demo

Then set in .env:   DB_BACKEND=duckdb   DUCKDB_PATH=network_rca.duckdb
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from pathlib import Path

import duckdb

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

TABLES = ("detected_anomalies", "network_devices", "device_telemetry", "device_syslogs")


def _q(value: str | Path) -> str:
    """SQL string literal."""
    return "'" + str(value).replace("'", "''") + "'"


# --- column types --------------------------------------------------------------
# CSV type sniffing is lossy (e.g. os_version "17.10" -> 17.1, ids with leading zeros -> ints), so we
# take the real column types from the Postgres schema whenever we can and map them to DuckDB types.

def pg_to_duckdb_type(pg_type: str) -> str:
    t = pg_type.strip().lower()
    if t.endswith("[]") or t == "array":
        return "VARCHAR"
    base = re.sub(r"\(.*\)", "", t).strip()
    if base in ("timestamptz", "timestamp with time zone"):
        return "TIMESTAMPTZ"
    if base.startswith("timestamp"):
        return "TIMESTAMP"
    if base in ("numeric", "decimal"):
        m = re.search(r"\((\d+)\s*,\s*(\d+)\)", t)
        return f"DECIMAL({m.group(1)},{m.group(2)})" if m and int(m.group(1)) <= 38 else "DOUBLE"
    return {
        "uuid": "UUID", "text": "VARCHAR", "varchar": "VARCHAR", "character varying": "VARCHAR",
        "char": "VARCHAR", "character": "VARCHAR", "bpchar": "VARCHAR", "citext": "VARCHAR",
        "json": "VARCHAR", "jsonb": "VARCHAR", "inet": "VARCHAR", "cidr": "VARCHAR", "macaddr": "VARCHAR",
        "integer": "INTEGER", "int": "INTEGER", "int4": "INTEGER", "serial": "INTEGER",
        "bigint": "BIGINT", "int8": "BIGINT", "bigserial": "BIGINT",
        "smallint": "SMALLINT", "int2": "SMALLINT", "smallserial": "SMALLINT",
        "double precision": "DOUBLE", "float8": "DOUBLE", "float": "DOUBLE",
        "real": "FLOAT", "float4": "FLOAT", "boolean": "BOOLEAN", "bool": "BOOLEAN",
        "date": "DATE", "time": "TIME", "time without time zone": "TIME", "interval": "INTERVAL",
    }.get(base, "VARCHAR")  # enums / user-defined types -> text


_CONSTRAINT_WORDS = ("primary", "foreign", "unique", "constraint", "check", "exclude", "like")
_TYPE_END = re.compile(r"\s+(not\s+null|null|default|primary|references|unique|check|generated|"
                       r"collate|constraint)\b.*$", re.I | re.S)


def _split_top_level(body: str) -> list[str]:
    parts, depth, cur = [], 0, []
    for ch in body:
        depth += ch == "("
        depth -= ch == ")"
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


def parse_ddl(sql_text: str) -> dict[str, dict[str, str]]:
    """{table: {column: duckdb_type}} from CREATE TABLE statements in Postgres DDL."""
    sql_text = re.sub(r"--[^\n]*", "", sql_text)
    out: dict[str, dict[str, str]] = {}
    for m in re.finditer(r"create\s+table\s+(?:if\s+not\s+exists\s+)?(?:\"?\w+\"?\.)?\"?(\w+)\"?\s*\(",
                         sql_text, re.I):
        # find the matching closing parenthesis
        depth, i = 1, m.end()
        while i < len(sql_text) and depth:
            depth += sql_text[i] == "("
            depth -= sql_text[i] == ")"
            i += 1
        cols = {}
        for item in _split_top_level(sql_text[m.end():i - 1]):
            first = item.split(None, 1)
            if len(first) < 2 or first[0].lower() in _CONSTRAINT_WORDS:
                continue
            name = first[0].strip('"')
            cols[name] = pg_to_duckdb_type(_TYPE_END.sub("", first[1]))
        out[m.group(1).lower()] = cols
    return out


def ddl_types_from_dir(folder: Path) -> dict[str, dict[str, str]]:
    types: dict[str, dict[str, str]] = {}
    for f in sorted(folder.rglob("*.sql")):
        types.update(parse_ddl(f.read_text(encoding="utf-8", errors="ignore")))
    return types


def load_csv(con: duckdb.DuckDBPyConnection, table: str, path: Path, types: dict[str, str] | None) -> None:
    with open(path, newline="", encoding="utf-8", errors="ignore") as f:
        header = next(csv.reader(f), [])
    known = {c: t for c, t in (types or {}).items() if c in header}
    if known:
        struct = "{" + ", ".join(f"{_q(c)}: {_q(t)}" for c, t in known.items()) + "}"
        con.execute(f"CREATE TABLE {table} AS SELECT * FROM read_csv({_q(path)}, header = true, types = {struct})")
    else:
        con.execute(f"CREATE TABLE {table} AS SELECT * FROM read_csv_auto({_q(path)}, header = true)")
    untyped = [c for c in header if c not in known]
    if untyped:
        print(f"    (types guessed for: {', '.join(untyped)})")


def find_csv(csv_dir: Path, table: str) -> Path | None:
    """Exact name first (network_devices.csv), then any CSV whose name contains the table name."""
    files = sorted(csv_dir.rglob("*.csv"))
    for f in files:
        if f.stem.lower() == table:
            return f
    matches = [f for f in files if table in f.stem.lower()]
    return matches[0] if matches else None


def from_csv(con: duckdb.DuckDBPyConnection, csv_dir: Path) -> None:
    if not csv_dir.is_dir():
        sys.exit(f"CSV folder not found: {csv_dir}")
    ddl = ddl_types_from_dir(csv_dir)
    print(f"  column types from schema SQL for: {', '.join(t for t in TABLES if t in ddl) or 'none found'}")
    missing = []
    for t in TABLES:
        f = find_csv(csv_dir, t)
        if not f:
            missing.append(t)
            continue
        print(f"  {t:20s} <- {f}")
        load_csv(con, t, f, ddl.get(t))
    if missing:
        sys.exit(f"No CSV found for: {', '.join(missing)} (looked under {csv_dir}). "
                 "Use --from-postgres instead, or point --from-csv at the folder holding the seed CSVs.")


def from_postgres(con: duckdb.DuckDBPyConnection, workdir: Path) -> None:
    """Copy each table out of Postgres as CSV (via psycopg COPY) and load it into DuckDB.

    Uses the same PG* / DATABASE_URL settings as the agent; needs no DuckDB extension download.
    """
    from agent.db_access import _connect_postgres

    conn = _connect_postgres()
    try:
        for t in TABLES:
            with conn.cursor() as cur:
                cur.execute("SELECT column_name, data_type FROM information_schema.columns "
                            "WHERE table_schema = 'public' AND table_name = %s", (t,))
                types = {c: pg_to_duckdb_type(d) for c, d in cur.fetchall()}
            conn.rollback()
            csv_path = workdir / f"{t}.csv"
            sql = f"COPY (SELECT * FROM public.{t}) TO STDOUT WITH (FORMAT csv, HEADER true)"
            with conn.cursor() as cur, open(csv_path, "wb") as f:
                if hasattr(cur, "copy"):  # psycopg 3
                    with cur.copy(sql) as copy:
                        for chunk in copy:
                            f.write(bytes(chunk))
                else:  # psycopg2
                    import io

                    buf = io.StringIO()
                    cur.copy_expert(sql, buf)
                    f.write(buf.getvalue().encode())
            print(f"  {t:20s} <- postgres public.{t}")
            load_csv(con, t, csv_path, types)
            csv_path.unlink()
    finally:
        conn.close()


DEMO_SQL = """
-- Synthetic data in the same shape as the challenge schema (invented devices and events).
CREATE TABLE network_devices (device_id VARCHAR, hostname VARCHAR, mgmt_ip VARCHAR, device_type VARCHAR,
    vendor VARCHAR, model VARCHAR, role VARCHAR, site_code VARCHAR, site_name VARCHAR, city VARCHAR,
    state VARCHAR, region VARCHAR, install_date DATE, os_version VARCHAR, status VARCHAR,
    wan_provider VARCHAR, wan_circuit_id VARCHAR, wan_circuit_group VARCHAR, notes VARCHAR);
INSERT INTO network_devices VALUES
 ('D-101','HARBOR-EDG01','10.20.1.1','router','Cisco','ISR4451','edge','HBR','Harbor Point','Portland','OR','West','2021-04-12','17.9.4','active','Lumen','LMN-88231','CG-WEST-2','Uplink Gi0/0/1 uses third-party optic'),
 ('D-102','HARBOR-EDG02','10.20.1.2','router','Cisco','ISR4451','edge','HBR','Harbor Point','Portland','OR','West','2021-04-12','17.9.4','active','Lumen','LMN-88232','CG-WEST-2',NULL),
 ('D-103','HARBOR-AGG01','10.20.1.10','switch','Arista','7050X3','aggregation','HBR','Harbor Point','Portland','OR','West','2020-09-30','4.31.2F','active',NULL,NULL,NULL,NULL),
 ('D-201','MILLBROOK-EDG01','10.30.1.1','router','Juniper','MX204','edge','MLB','Millbrook','Boise','ID','West','2022-01-20','22.4R2','active','Zayo','ZYO-55120','CG-WEST-5',NULL),
 ('D-301','CEDAR-CORE01','10.40.0.1','router','Juniper','MX960','core','CDR','Cedar Hub','Denver','CO','Central','2019-06-02','21.4R3','active',NULL,NULL,NULL,'Core transit node'),
 ('D-302','CEDAR-CORE02','10.40.0.2','router','Juniper','MX960','core','CDR','Cedar Hub','Denver','CO','Central','2019-06-02','21.4R3','active',NULL,NULL,NULL,NULL);

CREATE TABLE detected_anomalies (anomaly_id VARCHAR, severity VARCHAR, model_output VARCHAR, anomaly_date DATE);
INSERT INTO detected_anomalies VALUES
 ('a1f0c8e2-1b44-4d90-9c31-000000000001','critical',
  '{"detector": "interface_flap", "window": {"start": "2026-06-15T06:00:00Z", "end": "2026-06-15T06:45:00Z"}, "device": "HARBOR-EDG01", "score": 0.97, "features": {"interface_flap_count": 6}}',
  '2026-06-15'),
 ('a1f0c8e2-1b44-4d90-9c31-000000000002','minor',
  '{"detector": "latency_spike", "window": {"start": "2026-06-16T02:00:00Z", "end": "2026-06-16T02:05:00Z"}, "device": "CEDAR-CORE01", "score": 0.58}',
  '2026-06-16'),
 ('a1f0c8e2-1b44-4d90-9c31-000000000003','major',
  '{"detector": "bgp_session_drop", "window": {"start": "2026-06-17T14:03:00Z", "end": "2026-06-17T14:25:00Z"}, "devices": ["MILLBROOK-EDG01"], "score": 0.91}',
  '2026-06-17'),
 ('a1f0c8e2-1b44-4d90-9c31-000000000004','warning',
  '{"detector": "high_cpu", "window": {"start": "2026-06-15T06:05:00Z", "end": "2026-06-15T06:30:00Z"}, "device": "HARBOR-AGG01", "score": 0.74}',
  '2026-06-15');

CREATE TABLE device_telemetry (device_id VARCHAR, "timestamp" TIMESTAMP, cpu_utilization_pct DOUBLE,
    memory_utilization_pct DOUBLE, temperature_celsius DOUBLE, active_sessions INTEGER,
    bgp_established_peers INTEGER, interfaces_up_ratio DOUBLE, interface_error_count INTEGER,
    interface_flap_count INTEGER, policy_deny_count INTEGER, latency_ms DOUBLE, jitter_ms DOUBLE,
    packet_loss_pct DOUBLE);
-- anomaly 1: errors start 05:50 (degrading optic), flaps from 06:00; HARBOR-EDG02 on the same circuit is healthy
INSERT INTO device_telemetry
 SELECT 'D-101', ts, 30 + random() * 5, 52 + random() * 2, 41 + random(), 1200 + floor(random() * 50)::INT, 4,
        CASE WHEN ts BETWEEN TIMESTAMP '2026-06-15 06:00:00' AND TIMESTAMP '2026-06-15 06:40:00'
             AND minute(ts) % 10 = 0 THEN 0.75 ELSE 1.0 END,
        CASE WHEN ts >= TIMESTAMP '2026-06-15 05:50:00' THEN 150 + floor(random() * 250)::INT ELSE floor(random() * 3)::INT END,
        CASE WHEN ts BETWEEN TIMESTAMP '2026-06-15 06:00:00' AND TIMESTAMP '2026-06-15 06:40:00'
             AND minute(ts) % 10 = 0 THEN 2 ELSE 0 END,
        floor(random() * 4)::INT,
        CASE WHEN ts >= TIMESTAMP '2026-06-15 05:50:00' THEN 9 + random() * 6 ELSE 6 + random() END,
        CASE WHEN ts >= TIMESTAMP '2026-06-15 05:50:00' THEN 3 + random() * 2 ELSE 0.5 + random() * 0.3 END,
        CASE WHEN ts >= TIMESTAMP '2026-06-15 05:50:00' THEN 1.5 + random() * 2 ELSE random() * 0.05 END
 FROM generate_series(TIMESTAMP '2026-06-15 05:00:00', TIMESTAMP '2026-06-15 07:00:00', INTERVAL 5 MINUTE) t(ts);
INSERT INTO device_telemetry
 SELECT 'D-102', ts, 28 + random() * 5, 50 + random() * 2, 40 + random(), 1100 + floor(random() * 50)::INT, 4,
        1.0, floor(random() * 3)::INT, 0, floor(random() * 4)::INT, 6 + random(), 0.5 + random() * 0.3, random() * 0.05
 FROM generate_series(TIMESTAMP '2026-06-15 05:00:00', TIMESTAMP '2026-06-15 07:00:00', INTERVAL 5 MINUTE) t(ts);
-- anomaly 4: the aggregation switch CPU rises while processing the edge router's link churn (symptom)
INSERT INTO device_telemetry
 SELECT 'D-103', ts,
        CASE WHEN ts BETWEEN TIMESTAMP '2026-06-15 06:05:00' AND TIMESTAMP '2026-06-15 06:30:00' THEN 86 + random() * 8 ELSE 24 + random() * 5 END,
        60 + random() * 2, 45 + random(), 0, 2, 1.0, floor(random() * 3)::INT, 0, 0, 1 + random() * 0.2, 0.2, 0
 FROM generate_series(TIMESTAMP '2026-06-15 05:00:00', TIMESTAMP '2026-06-15 07:00:00', INTERVAL 5 MINUTE) t(ts);
-- anomaly 2: thin evidence - latency is normal in telemetry and there are no syslogs at all
INSERT INTO device_telemetry
 SELECT 'D-301', ts, 40 + random() * 4, 63 + random(), 47 + random(), 5400 + floor(random() * 100)::INT, 12,
        1.0, floor(random() * 2)::INT, 0, 0, 11 + random() * 2, 1 + random() * 0.3, random() * 0.02
 FROM generate_series(TIMESTAMP '2026-06-16 01:30:00', TIMESTAMP '2026-06-16 02:30:00', INTERVAL 5 MINUTE) t(ts);
-- anomaly 3: BGP peers drop right after a config change
INSERT INTO device_telemetry
 SELECT 'D-201', ts, 35 + random() * 4, 55 + random(), 43 + random(), 900 + floor(random() * 50)::INT,
        CASE WHEN ts >= TIMESTAMP '2026-06-17 14:03:00' AND ts < TIMESTAMP '2026-06-17 14:25:00' THEN 1 ELSE 3 END,
        1.0, floor(random() * 3)::INT, 0,
        CASE WHEN ts >= TIMESTAMP '2026-06-17 14:03:00' THEN 40 + floor(random() * 20)::INT ELSE floor(random() * 4)::INT END,
        8 + random(), 0.8 + random() * 0.3, random() * 0.05
 FROM generate_series(TIMESTAMP '2026-06-17 13:30:00', TIMESTAMP '2026-06-17 14:45:00', INTERVAL 5 MINUTE) t(ts);

CREATE TABLE device_syslogs (log_id VARCHAR, device_id VARCHAR, "timestamp" TIMESTAMP, severity VARCHAR,
    message_type VARCHAR, message VARCHAR);
INSERT INTO device_syslogs VALUES
 ('L-0001','D-101','2026-06-15 05:48:10','warning','TRANSCEIVER','Gi0/0/1: Rx power -16.9 dBm below low warning threshold -14.0 dBm'),
 ('L-0002','D-101','2026-06-15 05:52:30','warning','INTERFACE','Gi0/0/1: input errors increasing (CRC 212 in last 5 min)'),
 ('L-0003','D-101','2026-06-15 06:00:41','error','LINK','Interface GigabitEthernet0/0/1, changed state to down'),
 ('L-0004','D-101','2026-06-15 06:01:05','notice','LINK','Interface GigabitEthernet0/0/1, changed state to up'),
 ('L-0005','D-101','2026-06-15 06:20:12','error','LINK','Interface GigabitEthernet0/0/1, changed state to down'),
 ('L-0006','D-101','2026-06-15 06:20:39','notice','LINK','Interface GigabitEthernet0/0/1, changed state to up'),
 ('L-0007','D-101','2026-06-15 06:40:02','error','LINK','Interface GigabitEthernet0/0/1, changed state to down'),
 ('L-0008','D-101','2026-06-15 06:40:30','notice','LINK','Interface GigabitEthernet0/0/1, changed state to up'),
 ('L-0009','D-103','2026-06-15 06:06:00','warning','SYSTEM','CPU 88% - routing process handling interface events from HARBOR-EDG01'),
 ('L-0010','D-201','2026-06-17 14:01:47','info','CONFIG','Configuration committed by netops-jdoe: route-map PEER-IN modified'),
 ('L-0011','D-201','2026-06-17 14:03:02','error','BGP','BGP neighbor 203.0.113.9 Down - Hold timer expired'),
 ('L-0012','D-201','2026-06-17 14:03:31','error','BGP','BGP neighbor 198.51.100.4 Down - Peer closed the session'),
 ('L-0013','D-201','2026-06-17 14:25:10','info','CONFIG','Configuration rolled back to commit 3 by netops-jdoe'),
 ('L-0014','D-201','2026-06-17 14:26:00','notice','BGP','BGP neighbor 203.0.113.9 Up');
"""


def demo(con: duckdb.DuckDBPyConnection) -> None:
    print("  building SYNTHETIC demo data (not the challenge data)")
    con.execute("SELECT setseed(0.42)")
    con.execute(DEMO_SQL)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--from-csv", type=Path, metavar="DIR", help="folder containing the seed CSVs")
    src.add_argument("--from-postgres", action="store_true", help="copy from the running Postgres")
    src.add_argument("--demo", action="store_true", help="synthetic demo data for smoke tests")
    ap.add_argument("--out", type=Path, default=Path(os.getenv("DUCKDB_PATH", "network_rca.duckdb")))
    ap.add_argument("--force", action="store_true", help="overwrite an existing file")
    args = ap.parse_args()

    if args.out.exists():
        if not args.force:
            sys.exit(f"{args.out} already exists (use --force to overwrite)")
        args.out.unlink()
    tmp = args.out.with_suffix(".building")
    tmp.unlink(missing_ok=True)

    print(f"Building {args.out} ...")
    con = duckdb.connect(str(tmp))
    try:
        con.execute("SET TimeZone = 'UTC'")
        if args.from_csv:
            from_csv(con, args.from_csv)
        elif args.from_postgres:
            from_postgres(con, args.out.parent)
        else:
            demo(con)
        for t in TABLES:
            n = con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            print(f"  {t:20s} {n:>8} rows")
    except BaseException:
        con.close()
        tmp.unlink(missing_ok=True)
        raise
    con.close()
    tmp.rename(args.out)
    print(f"Done. Set DB_BACKEND=duckdb and DUCKDB_PATH={args.out} in .env")


if __name__ == "__main__":
    main()
