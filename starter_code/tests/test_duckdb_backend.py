"""Real-database tests on DuckDB - no container, no API key.

Builds the synthetic demo database into a temp folder, then runs the actual tools and the
full LangGraph flow (with a scripted LLM) against it.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

duckdb = pytest.importorskip("duckdb")
pytest.importorskip("pytz")

import agent.db_access as db  # noqa: E402
from agent import tools as T  # noqa: E402
from agent.graph import build_graph  # noqa: E402
from agent.schemas import EvidenceItem, RCAReport, RouteDecision  # noqa: E402
from scripts.build_duckdb import parse_ddl, pg_to_duckdb_type  # noqa: E402

from .fakes import ScriptedLLM  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FLAP = "a1f0c8e2-1b44-4d90-9c31-000000000001"
THIN = "a1f0c8e2-1b44-4d90-9c31-000000000002"


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


def test_schema_introspection_on_duckdb():
    assert db.time_column("device_telemetry") == "ts"
    assert db.device_column("device_syslogs") == "device_id"
    assert "value" in db.numeric_columns("device_telemetry")
    assert "message" in db.text_columns("device_syslogs")


def test_anomaly_and_device_lookup():
    assert call(T.get_anomaly, anomaly_id=FLAP)["detector"] == "interface_flap"
    assert call(T.get_device_details, device="edge-rtr-01")["device_id"] == "dev-001"  # hostname -> id


def test_telemetry_window_and_stats():
    out = call(T.get_device_telemetry, device="edge-rtr-01", start_time="2025-03-01T09:00:00Z",
               end_time="2025-03-01T10:30:00Z", metric_filter="rx_power")
    stats = out["series_stats"]["metric_name=rx_power_dbm | interface_name=Gi0/0/1"]["value"]
    assert stats["min"] < -17 and stats["max"] > -4   # baseline vs degraded optic is visible


def test_syslog_keyword_filter_and_order():
    out = call(T.get_device_syslogs, device="dev-001", keyword="changed state")
    assert out["total_messages"] == 6
    times = [m["ts"] for m in out["messages"]]
    assert times == sorted(times)


def test_related_devices_and_window():
    rel = call(T.find_related_devices, device="dev-001")
    assert any("site=DFW1" in k for k in rel["related"])
    win = call(T.find_anomalies_in_window, start_time="2025-03-01T09:00:00Z", end_time="2025-03-01T11:00:00Z")
    assert win["count"] == 2   # the flap and the agg-switch CPU spike


def test_thin_evidence_anomaly_has_no_syslogs():
    assert call(T.get_device_syslogs, device="core-rtr-09")["total_messages"] == 0


@pytest.mark.parametrize("sql", ["select * from read_csv_auto('/etc/passwd')", "attach 'x.db' as x",
                                 "select * from glob('*')", "install httpfs"])
def test_duckdb_file_access_blocked(sql):
    assert "error" in call(T.run_readonly_sql, sql=sql)


def test_database_file_is_read_only(demo_db):
    with pytest.raises(Exception):
        db.query("CREATE TABLE hack AS SELECT 1")


def test_full_graph_on_duckdb():
    from tests.fakes import tool_call

    report = RCAReport(
        summary="s", root_cause="Degraded optic on Gi0/0/1", root_cause_category="optics",
        affected_devices=["dev-001"],
        evidence=[EvidenceItem(source="device_telemetry", observation="rx -17.8 dBm from 09:35", relation="supports"),
                  EvidenceItem(source="device_syslogs", observation="RXPOWER_LOW_WARN 09:36", relation="supports")],
        confidence="high", confidence_rationale="telemetry and syslogs agree")
    llm = ScriptedLLM(
        chat=[tool_call("get_device_telemetry", {"device": "dev-001", "start_time": "2025-03-01T09:00:00Z",
                                                 "end_time": "2025-03-01T10:30:00Z"}, "a"),
              tool_call("get_device_syslogs", {"device": "edge-rtr-01"}, "b"),
              "enough evidence",
              "The optic degraded at 09:35, before the first flap at 10:01."],
        structured=[report, RouteDecision(intent="followup")])
    g = build_graph(llm=llm)
    cfg = {"configurable": {"thread_id": "duck"}}
    from langchain_core.messages import HumanMessage

    g.invoke({"messages": [HumanMessage(f"Investigate anomaly {FLAP}")]}, cfg)
    s = g.get_state(cfg).values
    assert s["investigation"]["confidence"] == "high"      # two independent sources returned data
    assert {e["tool"] for e in s["evidence_log"] if e["has_data"]} == {"get_device_telemetry", "get_device_syslogs"}

    g.invoke({"messages": [HumanMessage("What happened first?")]}, cfg)
    s = g.get_state(cfg).values
    assert "09:35" in s["messages"][-1].content
    assert "RXPOWER_LOW_WARN" in llm.calls[-1][0].content  # follow-up sees the raw syslog evidence


def test_ddl_type_mapping():
    types = parse_ddl("""CREATE TABLE public.t (id BIGSERIAL PRIMARY KEY, v TEXT DEFAULT '1.0',
                         ts TIMESTAMP WITH TIME ZONE NOT NULL, n NUMERIC(6,2), CONSTRAINT c CHECK (n > 0));""")
    assert types == {"t": {"id": "BIGINT", "v": "VARCHAR", "ts": "TIMESTAMPTZ", "n": "DECIMAL(6,2)"}}
    assert pg_to_duckdb_type("USER-DEFINED") == "VARCHAR"
