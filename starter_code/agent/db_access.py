"""Read-only data access for the agent's tools - PostgreSQL or DuckDB.

Kept separate from the provided ``db.py`` so the agent does not depend on that
helper's exact API. Both backends are read-only:
  postgres  every query runs in a READ ONLY transaction with a 10s statement timeout
  duckdb    the database file is opened with read_only=True

Backend selection:
  DB_BACKEND = postgres (default) | duckdb
  DUCKDB_PATH = path to the .duckdb file (default: network_rca.duckdb)
                build it with:  python -m scripts.build_duckdb --help

Postgres connection settings (first match wins):
  DATABASE_URL                      e.g. postgresql://postgres:postgres@localhost:5432/network_rca
  PGHOST / PGPORT / PGDATABASE / PGUSER / PGPASSWORD
"""
from __future__ import annotations

import os
import re
from functools import lru_cache
from typing import Any

TABLES = ("detected_anomalies", "network_devices", "device_telemetry", "device_syslogs")


def backend() -> str:
    return os.getenv("DB_BACKEND", "postgres").strip().lower()


def duckdb_path() -> str:
    return os.getenv("DUCKDB_PATH", "network_rca.duckdb")


def _connect_postgres():
    dsn = os.getenv("DATABASE_URL")
    params = dict(
        host=os.getenv("PGHOST", "localhost"),
        port=int(os.getenv("PGPORT", "5432")),
        dbname=os.getenv("PGDATABASE", "network_rca"),
        user=os.getenv("PGUSER", "postgres"),
        password=os.getenv("PGPASSWORD", "postgres"),
    )
    try:
        import psycopg  # psycopg 3

        return psycopg.connect(dsn) if dsn else psycopg.connect(**params)
    except ImportError:
        import psycopg2  # fallback

        return psycopg2.connect(dsn) if dsn else psycopg2.connect(**params)


def _query_postgres(sql: str, params, limit: int | None) -> list[dict[str, Any]]:
    conn = _connect_postgres()
    try:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute("SET LOCAL statement_timeout = 10000")
            cur.execute(sql, params)
            if cur.description is None:
                return []
            cols = [d[0] for d in cur.description]
            rows = cur.fetchmany(limit) if limit else cur.fetchall()
        conn.rollback()
        return [dict(zip(cols, r)) for r in rows]
    finally:
        conn.close()


def _query_duckdb(sql: str, params, limit: int | None) -> list[dict[str, Any]]:
    import duckdb

    path = duckdb_path()
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"DuckDB file '{path}' not found. Build it with: python -m scripts.build_duckdb --help")
    if params:  # tools write psycopg-style %s placeholders; DuckDB uses ?
        sql = sql.replace("%s", "?")
    con = duckdb.connect(path, read_only=True)
    try:
        con.execute("SET TimeZone = 'UTC'")  # timestamps come back in UTC, like the Postgres seed data
        cur = con.execute(sql, list(params) if params else [])
        if cur.description is None:
            return []
        cols = [d[0] for d in cur.description]
        rows = cur.fetchmany(limit) if limit else cur.fetchall()
        return [dict(zip(cols, r)) for r in rows]
    finally:
        con.close()


def query(sql: str, params: tuple | list | None = None, limit: int | None = None) -> list[dict[str, Any]]:
    """Run one read-only SELECT on the configured backend and return rows as dicts.

    Write SQL with ``%s`` placeholders; they are translated for DuckDB.
    """
    if backend() == "duckdb":
        return _query_duckdb(sql, params, limit)
    return _query_postgres(sql, params, limit)


# --- schema introspection (so tools adapt to the real column names) -------------

@lru_cache(maxsize=None)
def table_columns(table: str) -> tuple[tuple[str, str], ...]:
    """(column_name, data_type) pairs for a public table, in ordinal order."""
    # current_schema() is 'public' on Postgres and 'main' on DuckDB
    rows = query(
        """SELECT column_name, data_type FROM information_schema.columns
           WHERE table_schema = current_schema() AND table_name = %s ORDER BY ordinal_position""",
        (table,),
    )
    # DuckDB reports upper-case types (VARCHAR, DOUBLE); normalise for both backends
    return tuple((r["column_name"], str(r["data_type"]).lower()) for r in rows)


def _names(table: str) -> list[str]:
    return [c for c, _ in table_columns(table)]


def find_column(table: str, candidates: list[str], type_hint: str | None = None) -> str | None:
    """First column whose name matches a candidate (exact, then substring), else by type."""
    names = _names(table)
    for cand in candidates:
        if cand in names:
            return cand
    for cand in candidates:
        for n in names:
            if cand in n:
                return n
    if type_hint:
        for n, t in table_columns(table):
            if type_hint in t:
                return n
    return None


def time_column(table: str) -> str | None:
    return find_column(
        table,
        ["timestamp", "ts", "event_time", "collected_at", "logged_at", "detected_at",
         "start_time", "time", "created_at"],
        type_hint="timestamp",
    )


def time_cast(table: str, column: str) -> str:
    dtype = dict(table_columns(table)).get(column, "")
    return "timestamptz" if "with time zone" in dtype else "timestamp"


def device_column(table: str) -> str | None:
    return find_column(table, ["device_id", "hostname", "device_name", "device"])


def anomaly_id_column() -> str:
    return find_column("detected_anomalies", ["anomaly_id", "id"]) or "anomaly_id"


_TEXT_TYPES = ("text", "character varying", "character", "varchar", "string")
_NUMERIC_TYPES = {"integer", "int", "bigint", "smallint", "tinyint", "hugeint", "uhugeint", "ubigint",
                  "uinteger", "usmallint", "utinyint", "numeric", "decimal", "real", "double",
                  "double precision", "float"}


def text_columns(table: str) -> list[str]:
    return [n for n, t in table_columns(table) if t.split("(")[0] in _TEXT_TYPES]


def numeric_columns(table: str) -> list[str]:
    return [n for n, t in table_columns(table) if t.split("(")[0].strip() in _NUMERIC_TYPES]


# --- SQL guard for the free-form query tool -----------------------------------

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|merge|call|do|"
    r"vacuum|analyze|lock|set|reset|refresh|comment|pg_sleep|pg_read_file|dblink|"
    # DuckDB: file/network access and catalog changes
    r"attach|detach|install|load|pragma|export|import|checkpoint|read_\w+|glob|sniff_csv|"
    r"parquet_\w+|query_table|query)\b",
    re.IGNORECASE,
)


def validate_select(sql: str) -> str:
    """Return a cleaned single SELECT/WITH statement or raise ValueError."""
    cleaned = re.sub(r"--[^\n]*|/\*.*?\*/", " ", sql, flags=re.DOTALL).strip().rstrip(";").strip()
    if not cleaned:
        raise ValueError("Empty query.")
    if ";" in cleaned:
        raise ValueError("Only a single statement is allowed.")
    if not re.match(r"^(select|with)\b", cleaned, re.IGNORECASE):
        raise ValueError("Only SELECT (or WITH ... SELECT) queries are allowed.")
    # ignore keywords inside string literals
    without_strings = re.sub(r"'(?:[^']|'')*'", "''", cleaned)
    m = _FORBIDDEN.search(without_strings)
    if m:
        raise ValueError(f"Keyword '{m.group(0)}' is not allowed in read-only queries.")
    return cleaned
