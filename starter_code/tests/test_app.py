"""Headless UI test: Streamlit AppTest + scripted LLM + demo DuckDB (no browser, no API key)."""
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("streamlit")
pytest.importorskip("duckdb")
from streamlit.testing.v1 import AppTest  # noqa: E402

import agent.db_access as db  # noqa: E402
import agent.graph as graph_mod  # noqa: E402
from agent.schemas import EvidenceItem, RCAReport, RouteDecision  # noqa: E402

from .fakes import ScriptedLLM, tool_call  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FLAP = "a1f0c8e2-1b44-4d90-9c31-000000000001"


@pytest.fixture
def app(tmp_path, monkeypatch):
    path = tmp_path / "demo.duckdb"
    subprocess.run([sys.executable, "-m", "scripts.build_duckdb", "--demo", "--out", str(path)],
                   cwd=ROOT, check=True, capture_output=True)
    monkeypatch.setenv("DB_BACKEND", "duckdb")
    monkeypatch.setenv("DUCKDB_PATH", str(path))
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    db.table_columns.cache_clear()

    report = RCAReport(
        summary="Optic on Gi0/0/1 degraded, causing CRC errors then link flaps.",
        root_cause="Degraded optic on HARBOR-EDG01 Gi0/0/1", root_cause_category="optics",
        affected_devices=["HARBOR-EDG01"], affected_interfaces=["Gi0/0/1"],
        timeframe_start="2026-06-15T05:48:00Z", timeframe_end="2026-06-15T06:45:00Z",
        evidence=[EvidenceItem(source="device_syslogs", observation="05:48 Rx power -16.9 dBm", relation="supports"),
                  EvidenceItem(source="device_telemetry", observation="errors left baseline 05:50", relation="supports")],
        confidence="high", confidence_rationale="telemetry and syslogs agree")
    llm = ScriptedLLM(
        chat=[tool_call("get_device_telemetry", {"device": "HARBOR-EDG01"}, "a"),
              tool_call("get_device_syslogs", {"device": "HARBOR-EDG01"}, "b"),
              "enough",
              "The optic degraded first (05:48); the first flap was at 06:00."],
        structured=[report, RouteDecision(intent="followup")])
    import streamlit as st

    st.cache_resource.clear()  # the app caches its graph; never reuse one from another test
    st.cache_data.clear()
    holder = {"llm": llm}
    real_build = graph_mod.build_graph
    monkeypatch.setattr(graph_mod, "build_graph", lambda: real_build(llm=holder["llm"]))
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
    at.holder = holder  # tests can swap in a different scripted LLM
    yield at
    st.cache_resource.clear()
    st.cache_data.clear()
    db.table_columns.cache_clear()


def test_investigate_from_sidebar_then_follow_up(app):
    app.run()
    assert not app.exception
    assert any("Investigate" in b.label for b in app.sidebar.button)

    options = app.sidebar.selectbox[1].options
    assert any("interface_flap" in o and "HARBOR-EDG01" in o for o in options)   # read from the JSON column
    assert sorted(app.sidebar.selectbox[0].options) == ["All", "bgp_session_drop", "high_cpu",
                                                         "interface_flap", "latency_spike"]
    app.sidebar.selectbox[1].select(next(o for o in options if "interface_flap" in o))
    next(b for b in app.sidebar.button if "Investigate" in b.label).click().run()
    assert not app.exception
    page = " ".join(m.value for m in app.markdown)
    assert "Degraded optic on HARBOR-EDG01 Gi0/0/1" in page          # investigation panel
    assert "High" in page                                            # confidence badge
    assert len(app.dataframe) == 1                                    # evidence trail table

    app.chat_input[0].set_value("What happened first?").run()
    assert not app.exception
    assert "The optic degraded first" in " ".join(m.value for m in app.markdown)


def test_setup_problems_are_shown_not_crashed(app, monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY")
    app.run()
    assert not app.exception
    assert "No API key" in " ".join(m.value for m in app.markdown)


def test_rate_limit_error_is_shown_and_persists(app, monkeypatch):
    class QuotaLLM(ScriptedLLM):
        def invoke(self, messages, *a, **k):
            raise RuntimeError("429 RESOURCE_EXHAUSTED: Quota exceeded for metric generate_content_free_tier_requests")

        def with_structured_output(self, schema, **kwargs):
            return self

    app.holder["llm"] = QuotaLLM()
    app.run()
    app.chat_input[0].set_value("Which devices had the most anomalies?").run()
    assert not app.exception
    errors = [e.value for e in app.error]
    assert errors and "rate limit or daily quota" in errors[0]

    app.run()  # any later rerun (e.g. clicking a button) must still show why it failed
    assert any("rate limit or daily quota" in e.value for e in app.error)
