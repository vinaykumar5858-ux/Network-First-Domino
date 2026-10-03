"""Deliverable #3: end-to-end interface_flap investigation + follow-up turns.

Run from the starter_code folder:
    python -m examples.worked_example [anomaly_id]

Prints the conversation with a trace of graph nodes and tool calls, and saves it
to examples/worked_example_output.md for the write-up.
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage

from agent.graph import build_graph, text_of

DEFAULT_ANOMALY = "a1f0c8e2-1b44-4d90-9c31-000000000001"


def run_turn(graph, config, text: str, log: list[str]) -> None:
    print(f"\n{'=' * 80}\nUSER: {text}\n{'-' * 80}")
    log.append(f"### User\n\n> {text}\n")
    trace = []
    for update in graph.stream({"messages": [HumanMessage(text)]}, config, stream_mode="updates"):
        for node, delta in update.items():
            line = node
            if node == "route":
                line += f" -> intent={delta.get('intent')}"
            scratch = (delta or {}).get("scratch")
            for m in scratch if isinstance(scratch, list) else []:
                if isinstance(m, AIMessage) and m.tool_calls:
                    line += " | " + ", ".join(f"{c['name']}({json.dumps(c['args'], default=str)})"
                                              for c in m.tool_calls)
            trace.append(line)
            print(f"  [trace] {line}")
    answer = text_of(graph.get_state(config).values["messages"][-1])
    print(f"\nAGENT:\n{answer}")
    log.append("<details><summary>Graph trace</summary>\n\n```\n" + "\n".join(trace) + "\n```\n</details>\n")
    log.append(f"### Agent\n\n{answer}\n")


def main() -> None:
    anomaly_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ANOMALY
    graph = build_graph()
    config = {"configurable": {"thread_id": f"worked-example-{uuid.uuid4()}"}, "recursion_limit": 60}
    log = [f"# Worked example - interface_flap anomaly `{anomaly_id}`\n"]

    turns = [
        f"Investigate anomaly {anomaly_id}",
        "What happened first - the errors or the link going down?",            # follow-up, no id repeated
        "Is any other device affected, or is this isolated?",                  # follow-up needing reasoning/data
        "How confident are you, and what would raise your confidence?",        # follow-up on calibration
        "In plain terms, what's a BGP flap and how is it different from an interface flap?",  # general
    ]
    for t in turns:
        run_turn(graph, config, t, log)

    report = graph.get_state(config).values.get("investigation")
    log.append("## Structured RCA (JSON)\n\n```json\n" + json.dumps(report, indent=2, default=str) + "\n```\n")
    out = Path(__file__).with_name("worked_example_output.md")
    out.write_text("\n".join(log), encoding="utf-8")
    print(f"\nSaved transcript to {out}")


if __name__ == "__main__":
    main()
