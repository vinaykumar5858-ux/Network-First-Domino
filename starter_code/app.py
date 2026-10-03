"""First Domino - web UI for the Network Investigation Agent.

    streamlit run app.py

Pick an anomaly in the sidebar (or type an anomaly id), watch the agent work step by step,
read the structured RCA, then ask follow-up questions in the chat box. Uses the same graph,
memory and database settings (.env) as the CLI.
"""
from __future__ import annotations

import json
import uuid

import streamlit as st
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent import anomalies as anomaly_info
from agent import db_access as db
from agent.graph import build_graph, result_has_data, text_of
from agent.preflight import check_environment, describe_backend, is_rate_limit

st.set_page_config(page_title="First Domino", page_icon=":material/account_tree:", layout="wide")

CONFIDENCE_STYLE = {
    "high": ("#1a7f37", "High"),
    "medium": ("#b35900", "Medium"),
    "low": ("#c62828", "Low"),
    "insufficient": ("#57606a", "Insufficient evidence"),
}
WORKED_EXAMPLE = "a1f0c8e2-1b44-4d90-9c31-000000000001"
NODE_LABELS = {
    "start_investigation": "Loaded the anomaly record",
    "start_qa": "Gathered conversation and investigation context",
    "general": "Answered from networking knowledge (no database access)",
}


# --- cached resources ------------------------------------------------------------

@st.cache_resource(show_spinner=False)
def get_graph():
    return build_graph()


@st.cache_data(ttl=300, show_spinner=False)
def load_anomalies() -> list[dict]:
    """One normalised record per anomaly (works whether details are columns or inside JSON)."""
    id_col = db.anomaly_id_column()
    out = []
    for row in db.query("SELECT * FROM detected_anomalies", limit=1000):
        s = anomaly_info.summarize(row)
        out.append({"id": str(row[id_col]), "detector": s["detector"] or "unknown detector",
                    "devices": s["devices"], "severity": s["severity"],
                    "start": s["start"].strftime("%Y-%m-%d %H:%M") if s["start"] else ""})
    return sorted(out, key=lambda a: a["start"], reverse=True)


# --- session helpers ---------------------------------------------------------------

def session_config() -> dict:
    if "thread_id" not in st.session_state:
        st.session_state.thread_id = str(uuid.uuid4())
        st.session_state.traces = {}
        st.session_state.errors = {}
    return {"configurable": {"thread_id": st.session_state.thread_id}, "recursion_limit": 60}


def new_conversation() -> None:
    st.session_state.thread_id = str(uuid.uuid4())
    st.session_state.traces = {}
    st.session_state.errors = {}


def ask(text: str) -> None:
    st.session_state.pending = text


def fmt_args(args: dict) -> str:
    parts = [f"{k}={v!r}" for k, v in args.items() if v not in ("", None)]
    out = ", ".join(parts)
    return out if len(out) < 140 else out[:137] + "..."


def describe_step(node: str, delta: dict | None) -> list[str]:
    """Plain-language lines for one graph step (shown live and kept as a trace)."""
    delta = delta or {}
    if node == "route":
        intent = delta.get("intent", "?").replace("_", " ")
        return [f"Understood the request as: **{intent}**"]
    if node in NODE_LABELS:
        return [NODE_LABELS[node]]
    scratch = delta.get("scratch")
    lines: list[str] = []
    if node in ("investigator", "qa_agent"):
        msgs = scratch if isinstance(scratch, list) else []
        calls = [c for m in msgs if isinstance(m, AIMessage) for c in m.tool_calls]
        if calls:
            lines += [f"Querying `{c['name']}`({fmt_args(c['args'])})" for c in calls]
        elif node == "investigator":
            lines.append("Enough evidence gathered - moving to conclusions")
        else:
            lines.append("Wrote the answer")
    elif node == "execute_tools":
        for m in scratch if isinstance(scratch, list) else []:
            if isinstance(m, ToolMessage):
                got = "returned data" if result_has_data(text_of(m)) else "returned nothing"
                lines.append(f"↳ `{m.name}` {got}")
    elif node == "synthesize":
        inv = delta.get("investigation") or {}
        lines.append(f"Wrote the RCA report - confidence **{inv.get('confidence', '?')}**")
    return lines or [node]


def run_turn(text: str) -> None:
    graph, config = get_graph(), session_config()
    with st.chat_message("user"):
        st.markdown(text)
    trace: list[str] = []
    failed: Exception | None = None
    with st.chat_message("assistant"):
        with st.status("Working...", expanded=True) as status:
            try:
                for update in graph.stream({"messages": [HumanMessage(text)]}, config, stream_mode="updates"):
                    for node, delta in update.items():
                        for line in describe_step(node, delta):
                            trace.append(line)
                            status.write(line)
            except Exception as e:  # shown under the question after the rerun (see render_history)
                failed = e
    messages = graph.get_state(config).values.get("messages", [])
    if failed is not None:
        st.session_state.errors[len(messages) - 1] = (error_text(failed), trace)
    else:
        st.session_state.traces[len(messages) - 1] = trace
    st.rerun()


def error_text(e: Exception) -> str:
    if is_rate_limit(e):
        return ("**The LLM provider's rate limit or daily quota was reached**, so this question was not "
                "answered. Wait and retry, or set `LLM_MODEL` to a different model or another "
                "`LLM_PROVIDER` in `.env` and restart the app.\n\n"
                f"Details: `{type(e).__name__}: {str(e)[:300]}`")
    return f"**This question could not be answered.**\n\n`{type(e).__name__}: {str(e)[:500]}`"


# --- page sections -------------------------------------------------------------------

def short_time(value) -> str:
    """'2025-03-01T09:35:00Z' -> '2025-03-01 09:35' (falls back to the raw value)."""
    text = str(value or "?").replace("T", " ").replace("Z", "")
    return text[:16] if len(text) >= 16 and text[4] == "-" else text


def compact_report(markdown: str) -> str:
    """Shrink the report's headings so they fit inside a chat bubble."""
    if not markdown.startswith("## Root Cause Analysis"):
        return markdown
    lines = []
    for line in markdown.splitlines():
        if line.startswith("### "):
            line = "##### " + line[4:]
        elif line.startswith("## "):
            line = "#### " + line[3:]
        lines.append(line)
    return "\n".join(lines)


def confidence_badge(level: str) -> str:
    color, label = CONFIDENCE_STYLE.get(level, ("#57606a", level))
    return (f"<span style='background:{color};color:white;padding:2px 10px;border-radius:12px;"
            f"font-size:0.85rem;font-weight:600'>{label}</span>")


def render_sidebar(investigation: dict | None) -> None:
    with st.sidebar:
        st.title("First Domino")
        st.caption("Finds the first domino behind a network anomaly.")
        st.caption(f"Database: {describe_backend()}")

        st.subheader("Investigate an anomaly")
        try:
            anomalies = load_anomalies()
        except Exception as e:
            st.error(f"Could not load anomalies: {e}")
            anomalies = []
        if anomalies:
            detectors = sorted({a["detector"] for a in anomalies})
            choice = st.selectbox("Detector", ["All"] + detectors)
            rows = [a for a in anomalies if choice == "All" or a["detector"] == choice]

            def label(a: dict) -> str:
                bits = [a["detector"], ", ".join(a["devices"][:2]), a["severity"] or "", a["start"]]
                return " · ".join([b for b in bits if b] + [f"…{a['id'][-6:]}"])  # ids often differ only at the end

            # pre-select the challenge's worked-example anomaly when it is in the list
            default = next((i for i, a in enumerate(rows) if a["id"] == WORKED_EXAMPLE), 0)
            picked = st.selectbox(f"Anomaly ({len(rows)})", rows, index=default, format_func=label)
            if picked and st.button("🔍 Investigate", type="primary", use_container_width=True):
                ask(f"Investigate anomaly {picked['id']}")
        else:
            st.info("No anomalies found in the database.")

        st.subheader("Try asking")
        suggestions = (["What happened first?", "Is any other device affected?",
                        "How confident are you, and what would raise confidence?",
                        "What should an engineer check next?"]
                       if investigation else
                       ["Which devices had the most anomalies?"])
        suggestions.append("What's a BGP flap, in plain terms?")
        for s in suggestions:
            st.button(s, on_click=ask, args=(s,), use_container_width=True)

        st.divider()
        st.button("➕ New conversation", on_click=new_conversation, use_container_width=True)
        st.caption("Read-only: the agent investigates and recommends, it never changes devices.")


def render_investigation_panel(inv: dict) -> None:
    with st.container(border=True):
        top = st.columns([3, 2])
        top[0].markdown(f"**Current investigation** · anomaly `{inv.get('anomaly_id', '?')}`")
        top[1].markdown(f"Confidence: {confidence_badge(inv['confidence'])}", unsafe_allow_html=True)
        st.markdown(f"**Root cause:** {inv['root_cause']}")
        cols = st.columns(3)
        cols[0].markdown(f"**Category**  \n{inv.get('root_cause_category', '-')}")
        cols[1].markdown(f"**Devices**  \n{', '.join(inv.get('affected_devices') or []) or '-'}")
        start, end = short_time(inv.get("timeframe_start")), short_time(inv.get("timeframe_end"))
        if start[:10] == end[:10] and len(end) == 16:
            end = end[11:]  # same day: show only the end time
        cols[2].markdown(f"**Timeframe**  \n{start} → {end}")
        with st.expander("Evidence trail - every query the agent ran"):
            calls = inv.get("tool_calls") or []
            if calls:
                st.dataframe(
                    [{"tool": c["tool"], "arguments": json.dumps(c["args"], default=str),
                      "returned data": "yes" if c["has_data"] else "no"} for c in calls],
                    use_container_width=True, hide_index=True)
            for note in inv.get("calibration_notes") or []:
                st.warning(f"Calibration: {note}")


def render_history(messages: list) -> None:
    traces = st.session_state.get("traces", {})
    errors = st.session_state.get("errors", {})
    for i, m in enumerate(messages):
        if isinstance(m, HumanMessage):
            with st.chat_message("user"):
                st.markdown(text_of(m))
        elif isinstance(m, AIMessage):
            with st.chat_message("assistant"):
                st.markdown(compact_report(text_of(m)))
                if traces.get(i):
                    with st.expander(f"How I got this ({len(traces[i])} steps)"):
                        st.markdown("\n".join(f"- {line}" for line in traces[i]))
        if i in errors:  # this turn failed - say why instead of leaving the question unanswered
            message, steps = errors[i]
            with st.chat_message("assistant"):
                st.error(message)
                if steps:
                    with st.expander(f"Steps completed before the error ({len(steps)})"):
                        st.markdown("\n".join(f"- {line}" for line in steps))


def main() -> None:
    problems = check_environment()
    if problems:
        st.title("First Domino")
        st.error("Setup needs attention before the agent can run:")
        for p in problems:
            st.markdown(f"- {p}")
        st.info("Fix `.env`, then restart with `streamlit run app.py`.")
        st.stop()

    graph, config = get_graph(), session_config()
    state = graph.get_state(config).values
    investigation = state.get("investigation")
    render_sidebar(investigation)

    st.header("Network Investigation Agent")
    if investigation:
        render_investigation_panel(investigation)
    messages = state.get("messages", [])
    if not messages:
        st.markdown("Pick an anomaly in the sidebar and press **Investigate**, paste an anomaly id below, "
                    "or ask a networking question. Follow-ups use the investigation context automatically.")
    render_history(messages)

    prompt = st.chat_input("Ask a follow-up, paste an anomaly id, or ask a networking question")
    pending = st.session_state.pop("pending", None)
    if prompt or pending:
        run_turn(prompt or pending)


main()
