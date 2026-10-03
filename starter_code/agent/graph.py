"""Network Investigation Agent - LangGraph implementation.

Graph (one invocation = one user turn; state persists per thread_id):

    START -> route --investigate--> start_investigation -> investigator <-> execute_tools
                 |                                              |               |
                 |                                              v               |
                 |                                          synthesize -> END   |
                 |--followup / data_question--> start_qa -> qa_agent <----------+
                 |                                            |
                 |                                            v
                 |                                           END
                 +--general--> general -> END

- ``messages``      user-facing conversation, persisted by the checkpointer.
- ``scratch``       per-turn working memory for the tool loop (reset each turn),
                    so tool chatter does not bloat the conversation.
- ``investigation`` the last structured RCA report; follow-ups are grounded in it.
"""
from __future__ import annotations

import json
import re
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from . import db_access as db
from .config import HISTORY_WINDOW, MAX_INVESTIGATION_STEPS, MAX_QA_STEPS, get_llm
from .prompts import GENERAL_PROMPT, INVESTIGATOR_PROMPT, QA_PROMPT, ROUTER_PROMPT, SYNTHESIS_PROMPT
from .schemas import AgentState, RCAReport, RouteDecision, report_to_markdown
from .tools import INVESTIGATION_TOOLS, TOOLS_BY_NAME

UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
INVESTIGATE_WORDS = re.compile(r"\b(investigat\w*|rca|root[\s-]?cause|analy[sz]e|diagnos\w*|triage|look into)\b", re.I)
_EVIDENCE_TOOLS = {"get_device_telemetry", "get_device_syslogs", "find_anomalies_in_window",
                   "find_related_devices", "get_device_details", "run_readonly_sql"}


# --- small helpers ---------------------------------------------------------------

def text_of(msg: BaseMessage) -> str:
    """Message content as plain text (some providers return a list of parts)."""
    c = msg.content
    if isinstance(c, str):
        return c
    return "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in c)


def last_human(state: AgentState) -> str:
    for m in reversed(state.get("messages", [])):
        if isinstance(m, HumanMessage):
            return text_of(m)
    return ""


def recent_history(state: AgentState, n: int = HISTORY_WINDOW) -> list[BaseMessage]:
    """Last n user/assistant messages (tool chatter never enters ``messages``)."""
    msgs = [m for m in state.get("messages", []) if isinstance(m, (HumanMessage, AIMessage))][-n:]
    while msgs and not isinstance(msgs[0], HumanMessage):  # most providers require a user turn first
        msgs = msgs[1:]
    return [m if isinstance(m, HumanMessage) else AIMessage(content=text_of(m)) for m in msgs]


def result_has_data(output: str) -> bool:
    """Did a tool return actual rows (vs. an error or an empty result)?"""
    try:
        data = json.loads(output)
    except (ValueError, TypeError):
        return bool(output and "error" not in output.lower())
    if isinstance(data, dict):
        if "error" in data:
            return False
        for key in ("row_count", "count", "total_messages"):
            if key in data:
                return bool(data[key])
        return bool(data)
    return bool(data)


def calibrate(report: dict, evidence_log: list[dict]) -> dict:
    """Deterministic guard-rails so confidence can't outrun the evidence actually retrieved."""
    order = ["insufficient", "low", "medium", "high"]
    notes: list[str] = []
    sources_with_data = {e["tool"] for e in evidence_log if e["has_data"] and e["tool"] in _EVIDENCE_TOOLS}
    supporting = [e for e in report["evidence"] if e["relation"] == "supports"]

    def cap(level: str, why: str) -> None:
        if order.index(report["confidence"]) > order.index(level):
            notes.append(f"confidence lowered from {report['confidence']} to {level}: {why}")
            report["confidence"] = level

    if not sources_with_data:
        cap("insufficient", "no telemetry/syslog/inventory data was retrieved")
    elif len(sources_with_data) == 1:
        cap("medium", "only one evidence source returned data")
    if not supporting:
        cap("low", "no evidence item directly supports the stated root cause")
    if report["confidence"] == "insufficient" and not report["root_cause"].lower().startswith("undetermined"):
        report["root_cause"] = "Undetermined (best guess: " + report["root_cause"] + ")"
    report["calibration_notes"] = notes
    report["evidence_sources_with_data"] = sorted(sources_with_data)
    return report


# --- graph ------------------------------------------------------------------------

def build_graph(llm=None, checkpointer=None):
    """Compile the agent. Pass ``llm`` to override the provider (useful for tests)."""
    llm = llm or get_llm()
    llm_tools = llm.bind_tools(INVESTIGATION_TOOLS)

    # ---- routing ---------------------------------------------------------------
    def route(state: AgentState) -> dict:
        text = last_human(state)
        current = state.get("anomaly_id")
        has_investigation = bool(state.get("investigation"))
        ids = UUID_RE.findall(text)

        # cheap deterministic path: a new anomaly id, or an explicit "investigate <id>"
        if ids and (ids[0] != current or INVESTIGATE_WORDS.search(text) or text.strip() == ids[0]):
            return {"intent": "investigate", "anomaly_id": ids[0].lower()}

        try:
            current_desc = (f"anomaly {current}: {state['investigation']['summary']}"
                            if has_investigation else "none")
            decision: RouteDecision = llm.with_structured_output(RouteDecision).invoke(
                [SystemMessage(ROUTER_PROMPT.format(current=current_desc))]
                + recent_history(state, 4)
            )
            intent = decision.intent
            if intent == "investigate" and decision.anomaly_id and decision.anomaly_id.strip():
                return {"intent": "investigate", "anomaly_id": decision.anomaly_id.strip()}
        except Exception:  # router failure must never kill the turn
            intent = "followup" if has_investigation else "general"

        if intent == "investigate":          # asked to investigate but gave no usable id
            intent = "data_question"         # let the QA agent help find the anomaly
        if intent == "followup" and not has_investigation:
            intent = "data_question"
        return {"intent": intent}

    # ---- investigation ---------------------------------------------------------
    def start_investigation(state: AgentState) -> dict:
        aid = state["anomaly_id"]
        col = db.anomaly_id_column()
        rows = db.query(f'SELECT * FROM detected_anomalies WHERE "{col}"::text = %s', [aid])
        if not rows:
            previous = (state.get("investigation") or {}).get("anomaly_id")
            return {  # keep any earlier investigation intact
                "intent": "not_found", "anomaly_id": previous, "scratch": None,
                "messages": [AIMessage(content=f"I couldn't find anomaly `{aid}` in detected_anomalies. "
                                               "Please check the id (you can ask me to list recent anomalies).")],
            }
        anomaly = rows[0]
        task = (f"Investigate anomaly {aid}. The anomaly record is:\n"
                f"{json.dumps(anomaly, default=str, indent=1)}\n\n"
                f"User request: {last_human(state)}")
        return {
            "anomaly": anomaly, "investigation": None, "steps": 0, "evidence_log": [],
            "scratch": {"reset": [SystemMessage(INVESTIGATOR_PROMPT), HumanMessage(task)]},
        }

    def investigator(state: AgentState) -> dict:
        ai = llm_tools.invoke(state["scratch"])
        return {"scratch": [ai], "steps": state.get("steps", 0) + 1}

    def execute_tools(state: AgentState) -> dict:
        """Explicit tool executor: runs calls, records evidence, never raises."""
        ai: AIMessage = state["scratch"][-1]
        results, log = [], list(state.get("evidence_log") or [])
        for call in ai.tool_calls:
            tool = TOOLS_BY_NAME.get(call["name"])
            try:
                output = tool.invoke(call["args"]) if tool else f'{{"error": "unknown tool {call["name"]}"}}'
            except Exception as e:  # bad args, bad timestamp, DB error -> let the LLM recover
                output = json.dumps({"error": f"{type(e).__name__}: {e}"})
            results.append(ToolMessage(content=output, tool_call_id=call["id"], name=call["name"]))
            if state.get("intent") == "investigate":
                log.append({"tool": call["name"], "args": call["args"], "has_data": result_has_data(output),
                            "preview": output[:1500]})
        return {"scratch": results, "evidence_log": log}

    def synthesize(state: AgentState) -> dict:
        structured = llm.with_structured_output(RCAReport)
        try:
            report: RCAReport = structured.invoke(state["scratch"] + [HumanMessage(SYNTHESIS_PROMPT)])
        except Exception:
            # Some providers reject structured output after tool turns: retry on a flat transcript.
            transcript = "\n\n".join(
                f"[{type(m).__name__}{' ' + m.name if isinstance(m, ToolMessage) else ''}]\n{text_of(m)}"
                for m in state["scratch"][1:]
            )
            report = structured.invoke([SystemMessage(INVESTIGATOR_PROMPT),
                                        HumanMessage(transcript + "\n\n" + SYNTHESIS_PROMPT)])
        data = calibrate(report.model_dump(), state.get("evidence_log") or [])
        data["anomaly_id"] = state["anomaly_id"]
        data["tool_calls"] = [{"tool": e["tool"], "args": e["args"], "has_data": e["has_data"]}
                              for e in state.get("evidence_log") or []]
        return {"investigation": data, "messages": [AIMessage(content=report_to_markdown(data))],
                "scratch": None}

    # ---- conversational Q&A (follow-ups and ad-hoc data questions) ---------------
    def start_qa(state: AgentState) -> dict:
        if state.get("intent") == "followup" and state.get("investigation"):
            inv = {k: v for k, v in state["investigation"].items() if k != "tool_calls"}
            evidence = "\n\n".join(f"- {e['tool']}({json.dumps(e['args'], default=str)}):\n{e['preview']}"
                                   for e in state.get("evidence_log") or [])[:15000]
            context = (f"CURRENT INVESTIGATION (anomaly {state['anomaly_id']}):\n"
                       f"Anomaly record: {json.dumps(state.get('anomaly'), default=str)}\n"
                       f"RCA report: {json.dumps(inv, default=str)}\n\n"
                       f"Raw tool results gathered during the investigation (truncated):\n{evidence}")
        else:
            context = "No investigation is active; use the tools to answer from the database."
        return {"steps": 0,
                "scratch": {"reset": [SystemMessage(QA_PROMPT.format(context=context))] + recent_history(state)}}

    def qa_agent(state: AgentState) -> dict:
        steps = state.get("steps", 0)
        msgs = state["scratch"]
        if steps >= MAX_QA_STEPS:
            msgs = msgs + [HumanMessage("Tool budget reached. Answer now with what you have; "
                                        "state clearly anything you could not verify.")]
        ai = llm_tools.invoke(msgs)
        if ai.tool_calls and steps < MAX_QA_STEPS:
            return {"scratch": [ai], "steps": steps + 1}
        answer = text_of(ai) or "I wasn't able to complete that lookup within my tool budget."
        return {"messages": [AIMessage(content=answer)], "scratch": None}

    # ---- general domain knowledge (no tools) ------------------------------------
    def general(state: AgentState) -> dict:
        ctx = ""
        if state.get("investigation"):
            ctx = (f"\n\n(For context, the conversation's current investigation concluded: "
                   f"{state['investigation']['summary']})")
        ai = llm.invoke([SystemMessage(GENERAL_PROMPT.format(context=ctx))] + recent_history(state))
        return {"messages": [AIMessage(content=text_of(ai))]}

    # ---- edges ---------------------------------------------------------------------
    def after_route(state: AgentState) -> str:
        return {"investigate": "start_investigation", "general": "general"}.get(state["intent"], "start_qa")

    def after_start(state: AgentState) -> str:
        return END if state.get("intent") == "not_found" else "investigator"

    def after_investigator(state: AgentState) -> str:
        return "execute_tools" if state["scratch"][-1].tool_calls else "synthesize"

    def after_tools(state: AgentState) -> str:
        if state.get("intent") == "investigate":
            return "synthesize" if state.get("steps", 0) >= MAX_INVESTIGATION_STEPS else "investigator"
        return "qa_agent"

    def after_qa(state: AgentState) -> str:
        scratch = state.get("scratch") or []
        return "execute_tools" if scratch and getattr(scratch[-1], "tool_calls", None) else END

    g = StateGraph(AgentState)
    for name, fn in [("route", route), ("start_investigation", start_investigation),
                     ("investigator", investigator), ("execute_tools", execute_tools),
                     ("synthesize", synthesize), ("start_qa", start_qa), ("qa_agent", qa_agent),
                     ("general", general)]:
        g.add_node(name, fn)
    g.add_edge(START, "route")
    g.add_conditional_edges("route", after_route, ["start_investigation", "start_qa", "general"])
    g.add_conditional_edges("start_investigation", after_start, ["investigator", END])
    g.add_conditional_edges("investigator", after_investigator, ["execute_tools", "synthesize"])
    g.add_conditional_edges("execute_tools", after_tools, ["investigator", "synthesize", "qa_agent"])
    g.add_conditional_edges("qa_agent", after_qa, ["execute_tools", END])
    g.add_edge("start_qa", "qa_agent")
    g.add_edge("synthesize", END)
    g.add_edge("general", END)
    return g.compile(checkpointer=checkpointer if checkpointer is not None else MemorySaver())


class InvestigationAgent:
    """Thin convenience wrapper: one object = one conversation thread."""

    def __init__(self, thread_id: str = "default", graph=None):
        self.graph = graph or build_graph()
        self.config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 60}

    def ask(self, text: str) -> str:
        out = self.graph.invoke({"messages": [HumanMessage(content=text)]}, self.config)
        return text_of(out["messages"][-1])

    def investigate(self, anomaly_id: str) -> str:
        return self.ask(f"Investigate anomaly {anomaly_id}")

    @property
    def state(self) -> dict[str, Any]:
        return self.graph.get_state(self.config).values
