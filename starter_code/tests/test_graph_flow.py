"""Offline tests of the LangGraph control flow (routing, tool loop, memory) with a scripted LLM.
The database is faked, so no Postgres or API key is needed."""
import json
import types

import pytest
from langchain_core.messages import HumanMessage

import agent.graph as graph_mod
import agent.tools as tools_mod
from agent.schemas import EvidenceItem, RCAReport, RouteDecision

from .fakes import ScriptedLLM, tool_call

AID = "a1f0c8e2-1b44-4d90-9c31-000000000001"
ANOMALY = {"anomaly_id": AID, "detector": "interface_flap", "device_id": "dev-001"}


@pytest.fixture(autouse=True)
def fake_db(monkeypatch):
    fake = types.SimpleNamespace(
        anomaly_id_column=lambda: "anomaly_id",
        query=lambda sql, params=None, limit=None: [ANOMALY] if params and params[0] == AID else [],
    )
    monkeypatch.setattr(graph_mod, "db", fake)
    syslog = tools_mod.get_device_syslogs
    fake_tool = types.SimpleNamespace(
        invoke=lambda args: json.dumps({"total_messages": 2, "messages": ["10:01 Gi0/0/1 down", "10:02 up"]}))
    monkeypatch.setitem(graph_mod.TOOLS_BY_NAME, syslog.name, fake_tool)


def report(conf="high"):
    return RCAReport(
        summary="Gi0/0/1 flapped due to a degrading optic.", root_cause="Failing optic on Gi0/0/1",
        root_cause_category="optics", affected_devices=["dev-001"], affected_interfaces=["Gi0/0/1"],
        evidence=[EvidenceItem(source="device_syslogs", observation="link down 10:01", relation="supports")],
        confidence=conf, confidence_rationale="syslogs agree")


def run(graph, text, thread="t1"):
    cfg = {"configurable": {"thread_id": thread}}
    graph.invoke({"messages": [HumanMessage(text)]}, cfg)
    return graph.get_state(cfg).values


def test_investigation_then_followup_uses_memory():
    llm = ScriptedLLM(
        chat=[tool_call("get_device_syslogs", {"device": "dev-001"}),  # investigator: gather evidence
              "done gathering",                                          # investigator: stop
              "The link went down at 10:01."],                          # follow-up answer
        structured=[report("high"),                                      # synthesis
                    RouteDecision(intent="followup")],                   # router on follow-up
    )
    g = graph_mod.build_graph(llm=llm)

    s = run(g, f"Investigate anomaly {AID}")
    assert s["intent"] == "investigate"
    assert s["investigation"]["root_cause"] == "Failing optic on Gi0/0/1"
    assert s["investigation"]["confidence"] == "medium"   # calibrated: single evidence source
    assert "Root Cause Analysis" in s["messages"][-1].content
    assert s["scratch"] == []                              # tool chatter not kept in conversation

    s = run(g, "When did it first go down?")
    assert s["intent"] == "followup"
    assert s["messages"][-1].content == "The link went down at 10:01."
    system_prompt = llm.calls[-1][0].content
    assert AID in system_prompt and "Failing optic" in system_prompt  # grounded in prior investigation


def test_general_question_uses_no_tools():
    llm = ScriptedLLM(chat=["A BGP flap is a session repeatedly going up and down."],
                      structured=[RouteDecision(intent="general")])
    s = run(graph_mod.build_graph(llm=llm), "what's a BGP flap?")
    assert s["intent"] == "general"
    assert "BGP" in s["messages"][-1].content
    assert not s.get("evidence_log")


def test_unknown_anomaly_reports_not_found():
    llm = ScriptedLLM()
    s = run(graph_mod.build_graph(llm=llm), "Investigate anomaly 00000000-0000-0000-0000-000000000000")
    assert "couldn't find" in s["messages"][-1].content
    assert llm.calls == []  # no LLM spend on a bad id


def test_no_evidence_yields_insufficient():
    llm = ScriptedLLM(chat=["nothing to query"], structured=[report("high")])
    s = run(graph_mod.build_graph(llm=llm), f"Investigate {AID}")
    assert s["investigation"]["confidence"] == "insufficient"


def test_structured_output_fallback_on_flat_transcript():
    llm = ScriptedLLM(chat=["stop"], structured=[RuntimeError("provider rejects"), report("low")])
    s = run(graph_mod.build_graph(llm=llm), f"Investigate {AID}")
    assert s["investigation"]["summary"]
