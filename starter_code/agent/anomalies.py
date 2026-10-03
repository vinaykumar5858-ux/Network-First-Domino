"""Understand anomaly records whatever their layout.

Detector, device(s) and time window may be ordinary columns, or packed inside a JSON
column (e.g. ``model_output = {"detector": "interface_flap", "window": {"start": ..., "end": ...}, ...}``,
stored as text in DuckDB or as jsonb in Postgres). These helpers normalise both.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any

_DETECTOR_KEYS = ("detector", "detector_name", "detector_type", "anomaly_type", "anomaly_kind", "type")
_START_KEYS = ("start", "start_time", "window_start", "started_at", "from", "begin")
_END_KEYS = ("end", "end_time", "window_end", "ended_at", "to", "until")
_DEVICE_HINTS = ("device", "host", "node", "router", "switch")
_IGNORE_DEVICE_HINTS = ("count", "type", "role", "score", "ratio")


def decode_json_fields(row: dict) -> dict:
    """Copy of ``row`` with JSON text values turned into objects (dicts from jsonb are kept)."""
    out = {}
    for k, v in row.items():
        if isinstance(v, str) and v[:1] in "{[":
            try:
                v = json.loads(v)
            except ValueError:
                pass
        out[k] = v
    return out


def _walk(obj: Any, path: str = ""):
    """Yield (key_path, key, value) for every key in nested dicts/lists."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else str(k)
            yield p, str(k).lower(), v
            yield from _walk(v, p)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v, path)


def _first(pairs: list[tuple[str, str, Any]], keys: tuple[str, ...]) -> Any:
    for want in keys:
        for _, k, v in pairs:
            if k == want and v not in (None, "", [], {}):
                return v
    return None


def _devices(pairs: list[tuple[str, str, Any]]) -> list[str]:
    found: list[str] = []
    for _, k, v in pairs:
        if not any(h in k for h in _DEVICE_HINTS) or any(h in k for h in _IGNORE_DEVICE_HINTS):
            continue
        values = v if isinstance(v, list) else [v]
        for item in values:
            if isinstance(item, dict):  # e.g. {"hostname": ...} - picked up by the walk itself
                continue
            if isinstance(item, (str, int)) and str(item) and str(item) not in found:
                found.append(str(item))
    return found


def parse_time(value: Any) -> datetime | None:
    """Parse ISO strings / datetimes / dates into timezone-aware UTC datetimes."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime(value.year, value.month, value.day)
    else:
        try:
            dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def summarize(row: dict) -> dict:
    """{'detector','devices','start','end','severity'} from columns and/or nested JSON."""
    decoded = decode_json_fields(row)
    pairs = list(_walk(decoded))
    start = _first(pairs, _START_KEYS)
    end = _first(pairs, _END_KEYS)
    if start is None:  # fall back to any date/time column on the row itself
        start = next((v for v in row.values() if isinstance(v, (datetime, date))), None)
    return {
        "detector": _first(pairs, _DETECTOR_KEYS),
        "devices": _devices(pairs),
        "start": parse_time(start),
        "end": parse_time(end) or parse_time(start),
        "severity": _first(pairs, ("severity", "priority", "level")),
    }


def for_llm(row: dict) -> dict:
    """Anomaly record with JSON decoded and a normalised summary, ready to show the LLM."""
    s = summarize(row)
    out = decode_json_fields(row)
    out["_summary"] = {
        "detector": s["detector"], "devices": s["devices"], "severity": s["severity"],
        "window_start": s["start"].isoformat() if s["start"] else None,
        "window_end": s["end"].isoformat() if s["end"] else None,
    }
    return out


def overlaps(summary: dict, start: datetime | None, end: datetime | None) -> bool:
    a0, a1 = summary["start"], summary["end"]
    if a0 is None:
        return True  # unknown time: don't hide it
    if start and (a1 or a0) < start:
        return False
    if end and a0 > end:
        return False
    return True
