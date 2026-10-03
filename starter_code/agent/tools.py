"""LangChain tools the agent can call. All are read-only.

Design notes
- Tools are generic (device + time window), not per-anomaly: the LLM decides
  which devices, windows and signals matter for a given anomaly.
- Column names are discovered from information_schema at runtime, so the tools
  keep working if the schema uses e.g. ``hostname`` instead of ``device_id``.
- Large results are summarised (per-metric statistics + a sample of rows)
  rather than dumped, to keep the LLM context focused.
- ``run_readonly_sql`` is an escape hatch for questions the typed tools don't
  cover; it is guarded and executed in a READ ONLY transaction.
"""
from __future__ import annotations

import json
import statistics
from datetime import datetime
from typing import Any

from langchain_core.tools import tool

from . import anomalies
from . import db_access as db
from .config import TOOL_OUTPUT_CHAR_LIMIT

_DEVICE_KEY_CANDIDATES = ["device_id", "hostname", "device_name", "name", "mgmt_ip",
                          "management_ip", "ip_address"]
# shared-fate relationships, most specific first (a shared WAN circuit fails together)
_GROUPING_CANDIDATES = ["circuit_group", "upstream_device", "parent_device", "uplink_device",
                        "neighbor", "peer", "rack", "site", "location", "pop", "data_center",
                        "datacenter", "hub", "market", "region", "wan_provider"]
_COMPACT_DEVICE_COLUMNS = ["hostname", "role", "device_type", "status", "site_code", "site_name"]


# --- helpers ----------------------------------------------------------------

def _dump(payload: Any) -> str:
    text = json.dumps(payload, default=str, indent=1)
    if len(text) > TOOL_OUTPUT_CHAR_LIMIT:
        text = text[:TOOL_OUTPUT_CHAR_LIMIT] + "\n... [truncated - narrow the time window or filter]"
    return text


def _check_ts(value: str) -> str:
    """Validate an ISO-8601 timestamp string (raises ValueError if bad)."""
    datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value


def _time_filter(table: str, start_time: str, end_time: str) -> tuple[str, list]:
    col = db.time_column(table)
    if not col or not (start_time or end_time):
        return "", []
    cast = db.time_cast(table, col)
    clauses, params = [], []
    if start_time:
        clauses.append(f'"{col}" >= %s::{cast}')
        params.append(_check_ts(start_time))
    if end_time:
        clauses.append(f'"{col}" <= %s::{cast}')
        params.append(_check_ts(end_time))
    return " AND ".join(clauses), params


def _resolve_device(device: str) -> dict | None:
    """Find a device row by any identifying column (id, hostname, IP...)."""
    cols = [c for c in _DEVICE_KEY_CANDIDATES if c in dict(db.table_columns("network_devices"))]
    if not cols:
        return None
    where = " OR ".join(f'lower("{c}"::text) = lower(%s)' for c in cols)
    rows = db.query(f"SELECT * FROM network_devices WHERE {where} LIMIT 1", [device] * len(cols))
    return rows[0] if rows else None


def _device_filter(table: str, device: str) -> tuple[str, list]:
    """WHERE fragment matching ``device`` in ``table``, translating hostname<->id if needed."""
    col = db.device_column(table)
    if not col:
        raise ValueError(f"Table {table} has no device column.")
    value = device
    dev = _resolve_device(device)
    if dev and col in dev and dev[col] is not None:
        value = str(dev[col])
    return f'lower("{col}"::text) = lower(%s)', [value]


def _order_by(table: str) -> str:
    """Time first, then every other column, so ties sort identically on Postgres and DuckDB."""
    tcol = db.time_column(table)
    cols = ([tcol] if tcol else []) + [c for c, _ in db.table_columns(table) if c != tcol]
    return " ORDER BY " + ", ".join(f'"{c}"' for c in cols) if cols else ""


def _onset(srows: list[dict], col: str, tcol: str | None):
    """When did this metric first leave its baseline (the first quarter of the window)?

    Works for step changes (errors jump and stay high) and drops (interfaces_up_ratio falls),
    and ignores ordinary noise. Returns (timestamp, direction) or (None, None).
    """
    pts = [(r.get(tcol) if tcol else i, float(r[col])) for i, r in enumerate(srows) if r.get(col) is not None]
    if len(pts) < 6:
        return None, None
    base = [v for _, v in pts[: max(3, len(pts) // 4)]]
    bmin, bmax = min(base), max(base)
    bmean = statistics.fmean(base)
    vmax, vmin = max(v for _, v in pts), min(v for _, v in pts)
    up, down = vmax - bmax, bmin - vmin
    # the change must be material relative to the baseline level AND bigger than its normal wobble
    floor = max(0.2 * abs(bmean), bmax - bmin) + 1e-9
    if max(up, down) <= floor:
        return None, None
    if up >= down:
        limit = bmax + 0.25 * up
        hit = next((t for t, v in pts if v > limit), None)
        return hit, "rise"
    limit = bmin - 0.25 * down
    hit = next((t for t, v in pts if v < limit), None)
    return hit, "drop"


def _round(row: dict) -> dict:
    return {k: (round(v, 3) if isinstance(v, float) else v) for k, v in row.items()}


def _summarise(rows: list[dict], table: str) -> dict:
    """Per-series statistics (incl. when each metric left its baseline) + a compact sample."""
    if not rows:
        return {"row_count": 0}
    tcol = db.time_column(table)
    group_cols = [c for c in ("metric_name", "metric", "interface", "interface_name", "if_name")
                  if c in rows[0]]
    num_cols = [c for c in db.numeric_columns(table) if c in rows[0]]
    series: dict[str, list[dict]] = {}
    for r in rows:
        key = " | ".join(f"{g}={r[g]}" for g in group_cols) or "all"
        series.setdefault(key, []).append(r)

    stats = {}
    for key, srows in series.items():
        entry: dict[str, Any] = {"points": len(srows)}
        if tcol:
            entry["first"] = srows[0].get(tcol)
            entry["last"] = srows[-1].get(tcol)
        for c in num_cols:
            vals = [float(r[c]) for r in srows if r.get(c) is not None]
            if not vals:
                continue
            peak_row = max(srows, key=lambda r: float(r[c]) if r.get(c) is not None else float("-inf"))
            onset_at, direction = _onset(srows, c, tcol)
            entry[c] = {"min": round(min(vals), 3), "max": round(max(vals), 3),
                        "mean": round(statistics.fmean(vals), 3),
                        "max_at": peak_row.get(tcol) if tcol else None}
            if onset_at is not None:
                entry[c]["left_baseline_at"] = onset_at
                entry[c]["change"] = direction
        stats[key] = entry
    # keep the sample to ~300 cells so wide tables fit in the tool output
    width = max(1, len(rows[0]))
    sample_size = max(8, min(40, 300 // width))
    step = max(1, len(rows) // sample_size)
    return {"row_count": len(rows), "series_stats": stats,
            "sample_rows": [_round(r) for r in rows[::step][:sample_size]]}


# --- tools -------------------------------------------------------------------

@tool
def describe_schema() -> str:
    """List the tables, their columns/types and two example rows each.
    Call this first if you are unsure which columns exist."""
    out = {}
    for t in db.TABLES:
        cols = db.table_columns(t)
        out[t] = {
            "columns": {c: d for c, d in cols},
            "example_rows": db.query(f"SELECT * FROM {t} LIMIT 2"),
        }
    return _dump(out)


@tool
def get_anomaly(anomaly_id: str) -> str:
    """Fetch one detected anomaly (detector, device, time window, severity, etc.) by its id."""
    col = db.anomaly_id_column()
    rows = db.query(f'SELECT * FROM detected_anomalies WHERE "{col}"::text = %s', [anomaly_id.strip()])
    return _dump(anomalies.for_llm(rows[0]) if rows else {"error": f"No anomaly with id {anomaly_id}"})


@tool
def get_device_details(device: str) -> str:
    """Inventory details for one device (role, model, site, OS version, links...).
    `device` may be a device id, hostname or management IP."""
    dev = _resolve_device(device)
    return _dump(dev or {"error": f"Device '{device}' not found in network_devices"})


@tool
def get_device_telemetry(device: str, start_time: str = "", end_time: str = "", metric_filter: str = "") -> str:
    """Telemetry for a device within a time window (ISO-8601, e.g. 2025-03-01T10:00:00Z).
    Returns per-metric statistics (min/max/mean, time of peak, and `left_baseline_at`: when the
    metric first departed from its level at the start of the window) plus sample rows.
    `metric_filter` optionally narrows to metrics/interfaces whose name contains this text.
    Tip: include some time BEFORE the anomaly to get a baseline."""
    where, params = _device_filter("device_telemetry", device)
    tf, tparams = _time_filter("device_telemetry", start_time, end_time)
    if tf:
        where, params = f"{where} AND {tf}", params + tparams
    names = [c for c, _ in db.table_columns("device_telemetry")]
    metric_cols = db.numeric_columns("device_telemetry")
    long_format = any(c in names for c in ("metric_name", "metric"))
    select, note = "*", ""
    if metric_filter and long_format:  # one row per metric: filter rows by metric/interface name
        txt = db.text_columns("device_telemetry")
        where += " AND (" + " OR ".join(f'"{c}" ILIKE %s' for c in txt) + ")"
        params += [f"%{metric_filter}%"] * len(txt)
    elif metric_filter:  # one column per metric: keep only the matching metric columns
        keep = [c for c in metric_cols if metric_filter.lower() in c.lower()]
        if keep:
            base = [c for c in names if c not in metric_cols]
            select = ", ".join(f'"{c}"' for c in base + keep)
        else:
            note = f"no metric column matches '{metric_filter}' - returning all metrics"
    order = _order_by("device_telemetry")
    rows = db.query(f"SELECT {select} FROM device_telemetry WHERE {where}{order}", params, limit=3000)
    out = _summarise(rows, "device_telemetry")
    if not long_format:
        out["metrics_available"] = metric_cols
    if note:
        out["note"] = note
    return _dump(out)


@tool
def get_device_syslogs(device: str, start_time: str = "", end_time: str = "", keyword: str = "",
                       max_rows: int = 60) -> str:
    """Syslog messages for a device in a time window (ISO-8601), oldest first.
    `keyword` optionally filters messages (e.g. 'LINK', 'BGP', 'CRC', 'config', 'optic').
    Returns total count, counts by severity/category and the messages themselves."""
    where, params = _device_filter("device_syslogs", device)
    tf, tparams = _time_filter("device_syslogs", start_time, end_time)
    if tf:
        where, params = f"{where} AND {tf}", params + tparams
    if keyword:
        txt = db.text_columns("device_syslogs")
        where += " AND (" + " OR ".join(f'"{c}" ILIKE %s' for c in txt) + ")"
        params += [f"%{keyword}%"] * len(txt)
    order = _order_by("device_syslogs")
    rows = db.query(f"SELECT * FROM device_syslogs WHERE {where}{order}", params, limit=2000)

    breakdown = {}
    for c in ("severity", "level", "facility", "message_type", "event_type", "mnemonic", "category"):
        if rows and c in rows[0]:
            counts: dict[str, int] = {}
            for r in rows:
                counts[str(r[c])] = counts.get(str(r[c]), 0) + 1
            breakdown[c] = counts
    max_rows = max(1, min(int(max_rows), 150))
    shown = rows if len(rows) <= max_rows else rows[: max_rows // 2] + rows[-max_rows // 2:]
    return _dump({"total_messages": len(rows), "breakdown": breakdown,
                  "note": "middle messages omitted" if len(rows) > max_rows else "",
                  "messages": shown})


@tool
def find_related_devices(device: str) -> str:
    """Devices that share a site/region/upstream/peer relationship with `device`,
    useful for checking whether a problem is local or shared (e.g. common uplink)."""
    dev = _resolve_device(device)
    if not dev:
        return _dump({"error": f"Device '{device}' not found"})
    cols = dict(db.table_columns("network_devices"))
    id_col = db.device_column("network_devices") or next(iter(cols))
    show = [id_col] + [c for c in _COMPACT_DEVICE_COLUMNS if c in cols and c != id_col]
    related: dict[str, list] = {}
    used: set[str] = set()
    for cand in _GROUPING_CANDIDATES:
        col = next((n for n in cols if cand in n and n not in used), None)
        if not col or dev.get(col) in (None, ""):
            continue
        used.add(col)
        select = ", ".join(f'"{c}"' for c in dict.fromkeys(show + [col]))
        rows = db.query(
            f'SELECT {select} FROM network_devices WHERE "{col}"::text = %s AND "{id_col}"::text <> %s '
            f'ORDER BY "{id_col}" LIMIT 25',
            [str(dev[col]), str(dev[id_col])],
        )
        related[f"same {col} ({dev[col]})"] = rows
    for col in cols:  # devices that point at this one, e.g. upstream_device = this device
        if any(k in col for k in ("upstream", "parent", "uplink", "neighbor", "peer")):
            select = ", ".join(f'"{c}"' for c in dict.fromkeys(show + [col]))
            rows = db.query(f'SELECT {select} FROM network_devices WHERE "{col}"::text = %s LIMIT 25',
                            [str(dev[id_col])])
            if rows:
                related[f"devices whose {col} is {dev[id_col]}"] = rows
    return _dump({"device": dev, "related": related or "no shared site/circuit/upstream columns found"})


@tool
def find_anomalies_in_window(start_time: str, end_time: str, device: str = "") -> str:
    """Other detected anomalies overlapping start_time..end_time (ISO-8601), optionally only
    those involving one device. Use to spot correlated, shared-cause or cascading events."""
    start = anomalies.parse_time(_check_ts(start_time)) if start_time else None
    end = anomalies.parse_time(_check_ts(end_time)) if end_time else None
    aliases: set[str] = set()
    if device:
        aliases = {device.lower()}
        dev = _resolve_device(device)
        if dev:
            aliases |= {str(dev[c]).lower() for c in _DEVICE_KEY_CANDIDATES if dev.get(c) not in (None, "")}
    id_col = db.anomaly_id_column()
    out = []
    for row in db.query("SELECT * FROM detected_anomalies", limit=5000):
        s = anomalies.summarize(row)
        if not anomalies.overlaps(s, start, end):
            continue
        if aliases:
            mentioned = {d.lower() for d in s["devices"]}
            text = json.dumps(row, default=str).lower()
            if not (mentioned & aliases or any(a in text for a in aliases)):
                continue
        out.append({"anomaly_id": row.get(id_col), "detector": s["detector"], "devices": s["devices"],
                    "severity": s["severity"],
                    "window_start": s["start"].isoformat() if s["start"] else None,
                    "window_end": s["end"].isoformat() if s["end"] else None})
    out.sort(key=lambda a: a["window_start"] or "")
    return _dump({"count": len(out), "anomalies": out})


@tool
def run_readonly_sql(sql: str) -> str:
    """Run ONE read-only SELECT against the network_rca database (tables: detected_anomalies,
    network_devices, device_telemetry, device_syslogs). Use only when the other tools can't
    answer the question, e.g. aggregates across devices. Max 200 rows returned."""
    try:
        cleaned = db.validate_select(sql)
    except ValueError as e:
        return _dump({"error": str(e)})
    rows = db.query(cleaned, None, limit=200)
    return _dump({"row_count": len(rows), "rows": rows})


INVESTIGATION_TOOLS = [
    describe_schema, get_anomaly, get_device_details, get_device_telemetry,
    get_device_syslogs, find_related_devices, find_anomalies_in_window, run_readonly_sql,
]
TOOLS_BY_NAME = {t.name: t for t in INVESTIGATION_TOOLS}
