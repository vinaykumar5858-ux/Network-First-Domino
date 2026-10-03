"""State and structured-output schemas for the agent graph."""
from __future__ import annotations

from typing import Annotated, Any, Literal, Optional

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field
from typing_extensions import TypedDict


def scratch_reducer(left: list | None, right: Any) -> list:
    """Append-only working memory that can be reset.

    Write a list to append, ``{"reset": [...]}`` to replace, or ``None`` to clear.
    The investigation/Q&A loops keep their tool chatter here instead of in the
    user-facing ``messages`` list, so conversation history stays clean.
    """
    if right is None:
        return []
    if isinstance(right, dict) and "reset" in right:
        return list(right["reset"])
    return (left or []) + list(right)


Intent = Literal["investigate", "followup", "data_question", "general"]
TurnIntent = Literal["investigate", "followup", "data_question", "general", "not_found"]


class AgentState(TypedDict, total=False):
    # user-facing conversation (persisted per thread by the checkpointer)
    messages: Annotated[list[AnyMessage], add_messages]
    # router output for the current turn
    intent: TurnIntent
    # current investigation context (persists across turns -> follow-ups work)
    anomaly_id: Optional[str]
    anomaly: Optional[dict]
    investigation: Optional[dict]  # the final RCAReport as a dict
    # per-turn working memory of the tool loop
    scratch: Annotated[list[AnyMessage], scratch_reducer]
    steps: int
    evidence_log: list[dict]  # what each tool call returned (for calibration/eval)


# --- structured outputs --------------------------------------------------------

class RouteDecision(BaseModel):
    intent: Intent = Field(description=(
        "investigate = user wants a root-cause analysis of a specific anomaly; "
        "followup = question about the investigation already done in this conversation; "
        "data_question = needs facts from the network database but is not about the current investigation; "
        "general = conceptual networking question answerable from domain knowledge alone"))
    anomaly_id: Optional[str] = Field(default=None, description="anomaly id mentioned by the user, if any")


class EvidenceItem(BaseModel):
    source: str = Field(description="table/tool the observation came from, e.g. 'device_syslogs'")
    observation: str = Field(description="concrete fact with timestamps/values, quoted from tool output")
    relation: Literal["supports", "contradicts", "context"] = Field(
        description="how this observation relates to the stated root cause")


class RCAReport(BaseModel):
    summary: str = Field(description="2-3 sentence plain-language summary of what happened")
    root_cause: str = Field(description="most likely root cause, or 'Undetermined' if evidence is insufficient")
    root_cause_category: str = Field(description=(
        "e.g. physical_layer, optics, configuration_change, hardware, software_bug, capacity, "
        "upstream_dependency, protocol, unknown"))
    affected_devices: list[str]
    affected_interfaces: list[str] = Field(default_factory=list)
    timeframe_start: Optional[str] = None
    timeframe_end: Optional[str] = None
    evidence: list[EvidenceItem]
    alternative_hypotheses: list[str] = Field(
        default_factory=list, description="other explanations considered and why they are less likely")
    confidence: Literal["high", "medium", "low", "insufficient"]
    confidence_rationale: str
    evidence_gaps: list[str] = Field(default_factory=list, description="what data was missing or ambiguous")
    recommended_next_steps: list[str] = Field(
        default_factory=list, description="investigative/remediation recommendations for an engineer (no actions taken)")


def report_to_markdown(r: dict[str, Any]) -> str:
    lines = [
        f"## Root Cause Analysis - anomaly `{r.get('anomaly_id', '?')}`",
        f"**Summary:** {r['summary']}",
        "",
        f"**Most likely root cause:** {r['root_cause']}  _(category: {r['root_cause_category']})_",
        f"**Confidence:** {r['confidence'].upper()} - {r['confidence_rationale']}",
        f"**Affected device(s):** {', '.join(r['affected_devices']) or 'n/a'}",
    ]
    if r.get("affected_interfaces"):
        lines.append(f"**Affected interface(s):** {', '.join(r['affected_interfaces'])}")
    lines.append(f"**Timeframe:** {r.get('timeframe_start') or '?'} -> {r.get('timeframe_end') or '?'}")
    # markdown hard line breaks so each "**Field:**" line stays on its own line when rendered
    lines = [ln + "  " if ln.startswith("**") and not ln.startswith("**Summary") else ln for ln in lines]
    lines += ["", "### Evidence"]
    icon = {"supports": "[+]", "contradicts": "[-]", "context": "[i]"}
    for e in r["evidence"]:
        lines.append(f"- {icon.get(e['relation'], '-')} **{e['source']}**: {e['observation']}")
    for title, key in (("Alternative hypotheses considered", "alternative_hypotheses"),
                       ("Evidence gaps", "evidence_gaps"),
                       ("Recommended next steps (not executed)", "recommended_next_steps")):
        if r.get(key):
            lines += ["", f"### {title}"] + [f"- {x}" for x in r[key]]
    if r.get("calibration_notes"):
        lines += ["", "_Calibration: " + "; ".join(r["calibration_notes"]) + "_"]
    return "\n".join(lines)
