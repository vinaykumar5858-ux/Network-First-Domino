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
CREATE TABLE network_devices (device_id VARCHAR, hostname VARCHAR, role VARCHAR, vendor VARCHAR,
    model VARCHAR, site VARCHAR, upstream_device_id VARCHAR, os_version VARCHAR);
INSERT INTO network_devices VALUES
 ('dev-001','edge-rtr-01','edge_router','Cisco','ASR1001-X','DFW1','dev-010','17.3.4'),
 ('dev-002','edge-rtr-02','edge_router','Cisco','ASR1001-X','DFW1','dev-010','17.3.4'),
 ('dev-010','agg-sw-01','aggregation_switch','Juniper','QFX5120','DFW1','dev-020','21.4R3'),
 ('dev-020','core-rtr-09','core_router','Juniper','MX480','DFW1',NULL,'22.1R1'),
 ('dev-030','edge-rtr-07','edge_router','Cisco','ASR1001-X','ATL2','dev-040','17.6.1'),
 ('dev-040','agg-sw-04','aggregation_switch','Juniper','QFX5120','ATL2',NULL,'21.4R3');

CREATE TABLE detected_anomalies (anomaly_id UUID, detector VARCHAR, device_id VARCHAR,
    interface_name VARCHAR, severity VARCHAR, start_time TIMESTAMPTZ, end_time TIMESTAMPTZ,
    description VARCHAR);
INSERT INTO detected_anomalies VALUES
 ('a1f0c8e2-1b44-4d90-9c31-000000000001','interface_flap','dev-001','Gi0/0/1','major',
  '2025-03-01 10:00:00+00','2025-03-01 10:30:00+00','Gi0/0/1 changed state 6 times in 30 minutes'),
 ('a1f0c8e2-1b44-4d90-9c31-000000000002','latency_spike','dev-020',NULL,'minor',
  '2025-03-02 02:00:00+00','2025-03-02 02:05:00+00','p95 latency above baseline for 5 minutes'),
 ('a1f0c8e2-1b44-4d90-9c31-000000000003','bgp_session_flap','dev-030',NULL,'major',
  '2025-03-03 14:03:00+00','2025-03-03 14:20:00+00','BGP neighbor 203.0.113.9 reset 3 times'),
 ('a1f0c8e2-1b44-4d90-9c31-000000000004','high_cpu','dev-010',NULL,'minor',
  '2025-03-01 10:05:00+00','2025-03-01 10:25:00+00','Control-plane CPU above 85%');

CREATE TABLE device_telemetry (ts TIMESTAMPTZ, device_id VARCHAR, interface_name VARCHAR,
    metric_name VARCHAR, value DOUBLE);
-- anomaly 1: optic degrades (rx power drop at 09:35), CRC errors climb from 09:40, link flaps from 10:01
INSERT INTO device_telemetry
 SELECT ts, 'dev-001', 'Gi0/0/1', 'rx_power_dbm',
        CASE WHEN ts >= TIMESTAMPTZ '2025-03-01 09:35:00+00' THEN -17.8 + random() ELSE -3.2 + random() * 0.2 END
 FROM generate_series(TIMESTAMPTZ '2025-03-01 09:00:00+00', TIMESTAMPTZ '2025-03-01 11:00:00+00', INTERVAL 5 MINUTE) t(ts);
INSERT INTO device_telemetry
 SELECT ts, 'dev-001', 'Gi0/0/1', 'crc_errors',
        CASE WHEN ts >= TIMESTAMPTZ '2025-03-01 09:40:00+00' THEN 40 + floor(random() * 80) ELSE 0 END
 FROM generate_series(TIMESTAMPTZ '2025-03-01 09:00:00+00', TIMESTAMPTZ '2025-03-01 11:00:00+00', INTERVAL 5 MINUTE) t(ts);
INSERT INTO device_telemetry
 SELECT ts, 'dev-001', 'Gi0/0/1', 'oper_status', CASE WHEN minute(ts) IN (5, 20) AND hour(ts) = 10 THEN 0 ELSE 1 END
 FROM generate_series(TIMESTAMPTZ '2025-03-01 09:00:00+00', TIMESTAMPTZ '2025-03-01 11:00:00+00', INTERVAL 5 MINUTE) t(ts);
-- healthy neighbour on the same aggregation switch (shows the problem is local)
INSERT INTO device_telemetry
 SELECT ts, 'dev-002', 'Gi0/0/1', 'crc_errors', 0
 FROM generate_series(TIMESTAMPTZ '2025-03-01 09:00:00+00', TIMESTAMPTZ '2025-03-01 11:00:00+00', INTERVAL 5 MINUTE) t(ts);
-- anomaly 4: CPU on agg-sw-01 rises while it processes edge-rtr-01's link churn (a symptom, not the cause)
INSERT INTO device_telemetry
 SELECT ts, 'dev-010', NULL, 'cpu_util_pct',
        CASE WHEN ts BETWEEN TIMESTAMPTZ '2025-03-01 10:05:00+00' AND TIMESTAMPTZ '2025-03-01 10:25:00+00'
             THEN 86 + random() * 8 ELSE 22 + random() * 5 END
 FROM generate_series(TIMESTAMPTZ '2025-03-01 09:00:00+00', TIMESTAMPTZ '2025-03-01 11:00:00+00', INTERVAL 5 MINUTE) t(ts);
-- anomaly 2: thin evidence - latency is flat in telemetry, no syslogs at all
INSERT INTO device_telemetry
 SELECT ts, 'dev-020', NULL, 'latency_ms_p95', 11 + random() * 2
 FROM generate_series(TIMESTAMPTZ '2025-03-02 01:30:00+00', TIMESTAMPTZ '2025-03-02 02:30:00+00', INTERVAL 5 MINUTE) t(ts);
-- anomaly 3: BGP prefixes drop after a config change
INSERT INTO device_telemetry
 SELECT ts, 'dev-030', NULL, 'bgp_prefixes_received',
        CASE WHEN ts >= TIMESTAMPTZ '2025-03-03 14:03:00+00' AND minute(ts) % 10 = 5 THEN 0 ELSE 812000 END
 FROM generate_series(TIMESTAMPTZ '2025-03-03 13:30:00+00', TIMESTAMPTZ '2025-03-03 14:30:00+00', INTERVAL 5 MINUTE) t(ts);

CREATE TABLE device_syslogs (ts TIMESTAMPTZ, device_id VARCHAR, severity INTEGER, facility VARCHAR,
    mnemonic VARCHAR, message VARCHAR);
INSERT INTO device_syslogs VALUES
 ('2025-03-01 09:36:00+00','dev-001',4,'TRANSCEIVER','RXPOWER_LOW_WARN','Gi0/0/1: Rx power -17.6 dBm below warning threshold -14.0 dBm'),
 ('2025-03-01 09:52:00+00','dev-001',4,'IFMGR','CRC_ERRORS','Gi0/0/1: input CRC errors rising (112 in last interval)'),
 ('2025-03-01 10:01:12+00','dev-001',3,'LINK','UPDOWN','Interface GigabitEthernet0/0/1, changed state to down'),
 ('2025-03-01 10:01:40+00','dev-001',3,'LINK','UPDOWN','Interface GigabitEthernet0/0/1, changed state to up'),
 ('2025-03-01 10:08:03+00','dev-001',3,'LINK','UPDOWN','Interface GigabitEthernet0/0/1, changed state to down'),
 ('2025-03-01 10:08:31+00','dev-001',3,'LINK','UPDOWN','Interface GigabitEthernet0/0/1, changed state to up'),
 ('2025-03-01 10:21:55+00','dev-001',3,'LINK','UPDOWN','Interface GigabitEthernet0/0/1, changed state to down'),
 ('2025-03-01 10:22:20+00','dev-001',3,'LINK','UPDOWN','Interface GigabitEthernet0/0/1, changed state to up'),
 ('2025-03-01 10:06:00+00','dev-010',4,'CHASSISD','HIGH_CPU','Routing engine CPU 88% - rpd processing interface events'),
 ('2025-03-01 10:01:13+00','dev-010',5,'SNMP','LINK_DOWN','xe-0/0/5 (to edge-rtr-01) link down'),
 ('2025-03-01 10:01:41+00','dev-010',5,'SNMP','LINK_UP','xe-0/0/5 (to edge-rtr-01) link up'),
 ('2025-03-03 14:01:47+00','dev-030',5,'SYS','CONFIG_I','Configured from console by netops-jdoe on vty0 (route-map PEER-IN modified)'),
 ('2025-03-03 14:03:02+00','dev-030',3,'BGP','ADJCHANGE','neighbor 203.0.113.9 Down - Hold timer expired'),
 ('2025-03-03 14:03:30+00','dev-030',5,'BGP','ADJCHANGE','neighbor 203.0.113.9 Up'),
 ('2025-03-03 14:13:05+00','dev-030',3,'BGP','ADJCHANGE','neighbor 203.0.113.9 Down - Peer closed the session');
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
