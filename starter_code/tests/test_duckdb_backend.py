"""Real-database tests on DuckDB - no container, no API key.

The demo database has the same SHAPE as the challenge data (anomaly details packed in a JSON
`model_output` column, wide telemetry with one column per metric, syslogs with `message_type`),
with invented devices and events. Tests run the actual tools and the full LangGraph flow
(with a scripted LLM) against it.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

duckdb = pytest.importorskip("duckdb")
pytest.importorskip("pytz")

import agent.db_access as db  # noqa: E402
from agent import anomalies  # noqa: E402
from agent import tools as T  # noqa: E402
from agent.graph import build_graph  # noqa: E402
from agent.schemas import EvidenceItem, RCAReport, RouteDecision  # noqa: E402
from scripts.build_duckdb import parse_ddl, pg_to_duckdb_type  # noqa: E402

from .fakes import ScriptedLLM, tool_call  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FLAP = "a1f0c8e2-1b44-4d90-9c31-000000000001"
WINDOW = {"start_time": "2026-06-15T05:00:00Z", "end_time": "2026-06-15T07:00:00Z"}


@pytest.fixture(scope="module")
def demo_db(tmp_path_factory):
    path = tmp_path_factory.mktemp("duck") / "demo.duckdb"
    subprocess.run([sys.executable, "-m", "scripts.build_duckdb", "--demo", "--out", str(path)],
                   cwd=ROOT, check=True, capture_output=True)
    return path


@pytest.fixture(autouse=True)
def use_duckdb(demo_db, monkeypatch):
    monkeypatch.setenv("DB_BACKEND", "duckdb")
    monkeypatch.setenv("DUCKDB_PATH", str(demo_db))
    db.table_columns.cache_clear()
    yield
    db.table_columns.cache_clear()


def call(tool, **args):
    return json.loads(tool.invoke(args))


def test_schema_detection_matches_challenge_shape():
    assert db.time_column("device_telemetry") == "timestamp"
    assert db.device_column("device_syslogs") == "device_id"
    assert db.time_column("detected_anomalies") is None      # window lives inside model_output JSON
    assert "interface_flap_count" in db.numeric_columns("device_telemetry")


def test_anomaly_json_is_decoded_and_summarised():
    a = call(T.get_anomaly, anomaly_id=FLAP)
    assert a["model_output"]["detector"] == "interface_flap"  # JSON text decoded for the LLM
    assert a["_summary"] == {"detector": "interface_flap", "devices": ["HARBOR-EDG01"], "severity": "critical",
                             "window_start": "2026-06-15T06:00:00+00:00",
                             "window_end": "2026-06-15T06:45:00+00:00"}


def test_summary_handles_device_lists_and_jsonb_dicts():
    s = anomalies.summarize({"id": "x", "model_output": {"detector": "bgp", "devices": ["A", "B"],
                                                         "window": {"start": "2026-01-01T00:00:00Z"}}})
    assert s["detector"] == "bgp" and s["devices"] == ["A", "B"] and s["start"].year == 2026


def test_hostname_translates_to_device_id():
    assert call(T.get_device_details, device="harbor-edg01")["device_id"] == "D-101"
    assert call(T.get_device_syslogs, device="HARBOR-EDG01", **WINDOW)["total_messages"] == 8


def test_onset_shows_errors_before_flaps():
    stats = call(T.get_device_telemetry, device="HARBOR-EDG01", **WINDOW)["series_stats"]["all"]
    errors = stats["interface_error_count"]["left_baseline_at"]
    flaps = stats["interface_flap_count"]["left_baseline_at"]
    assert errors < flaps                                       # the first domino comes first
    assert stats["interfaces_up_ratio"]["change"] == "drop"


def test_no_false_onsets_on_healthy_device():
    stats = call(T.get_device_telemetry, device="HARBOR-EDG02", **WINDOW)["series_stats"]["all"]
    assert not [m for m, v in stats.items() if isinstance(v, dict) and v.get("left_baseline_at")]


def test_wide_metric_filter_selects_columns():
    out = call(T.get_device_telemetry, device="HARBOR-EDG01", metric_filter="flap", **WINDOW)
    metrics = [k for k, v in out["series_stats"]["all"].items() if isinstance(v, dict)]
    assert metrics == ["interface_flap_count"]
    assert "cpu_utilization_pct" in out["metrics_available"]
    assert "note" in call(T.get_device_telemetry, device="HARBOR-EDG01", metric_filter="nope")


def test_telemetry_output_fits_without_truncation():
    raw = T.get_device_telemetry.invoke({"device": "HARBOR-EDG01", **WINDOW})
    json.loads(raw)  # would fail if the output had been cut off


def test_related_devices_include_shared_circuit():
    rel = call(T.find_related_devices, device="HARBOR-EDG01")["related"]
    circuit = rel["same wan_circuit_group (CG-WEST-2)"]
    assert [d["hostname"] for d in circuit] == ["HARBOR-EDG02"]


def test_anomalies_in_window_from_json_fields():
    out = call(T.find_anomalies_in_window, **WINDOW)
    assert sorted(a["detector"] for a in out["anomalies"]) == ["high_cpu", "interface_flap"]
    by_device = call(T.find_anomalies_in_window, device="D-101", **WINDOW)  # id resolved to hostname
    assert [a["detector"] for a in by_device["anomalies"]] == ["interface_flap"]


def test_thin_evidence_anomaly_has_no_syslogs():
    assert call(T.get_device_syslogs, device="CEDAR-CORE01")["total_messages"] == 0


@pytest.mark.parametrize("sql", ["select * from read_csv_auto('/etc/passwd')", "attach 'x.db' as x",
                                 "select * from glob('*')", "install httpfs"])
def test_duckdb_file_access_blocked(sql):
    assert "error" in call(T.run_readonly_sql, sql=sql)


def test_database_file_is_read_only():
    with pytest.raises(Exception):
        db.query("CREATE TABLE hack AS SELECT 1")


def test_full_graph_on_duckdb():
    from langchain_core.messages import HumanMessage

    report = RCAReport(
        summary="s", root_cause="Degraded optic on HARBOR-EDG01 Gi0/0/1", root_cause_category="optics",
        affected_devices=["HARBOR-EDG01"],
        evidence=[EvidenceItem(source="device_syslogs", observation="05:48 Rx power -16.9 dBm", relation="supports"),
                  EvidenceItem(source="device_telemetry", observation="errors left baseline 05:50", relation="supports")],
        confidence="high", confidence_rationale="telemetry and syslogs agree")
    llm = ScriptedLLM(
        chat=[tool_call("get_device_telemetry", {"device": "HARBOR-EDG01", **WINDOW}, "a"),
              tool_call("get_device_syslogs", {"device": "HARBOR-EDG01", **WINDOW}, "b"),
              "enough evidence",
              "The optic degraded at 05:48, errors followed at 05:50 and the first flap was at 06:00."],
        structured=[report, RouteDecision(intent="followup")])
    g = build_graph(llm=llm)
    cfg = {"configurable": {"thread_id": "duck"}}

    g.invoke({"messages": [HumanMessage(f"Investigate anomaly {FLAP}")]}, cfg)
    s = g.get_state(cfg).values
    assert "HARBOR-EDG01" in llm.calls[0][1].content          # decoded JSON summary reached the LLM
    assert s["investigation"]["confidence"] == "high"        # two independent sources returned data
    assert {e["tool"] for e in s["evidence_log"] if e["has_data"]} == {"get_device_telemetry", "get_device_syslogs"}

    g.invoke({"messages": [HumanMessage("What happened first?")]}, cfg)
    s = g.get_state(cfg).values
    assert "05:48" in s["messages"][-1].content
    assert "Rx power" in llm.calls[-1][0].content             # follow-up sees the raw syslog evidence


def test_ddl_type_mapping():
    types = parse_ddl("""CREATE TABLE public.t (id BIGSERIAL PRIMARY KEY, v TEXT DEFAULT '1.0',
                         ts TIMESTAMP WITH TIME ZONE NOT NULL, n NUMERIC(6,2), CONSTRAINT c CHECK (n > 0));""")
    assert types == {"t": {"id": "BIGINT", "v": "VARCHAR", "ts": "TIMESTAMPTZ", "n": "DECIMAL(6,2)"}}
    assert pg_to_duckdb_type("USER-DEFINED") == "VARCHAR"
