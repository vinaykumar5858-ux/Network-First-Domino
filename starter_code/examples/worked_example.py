"""Deliverable #3: end-to-end interface_flap investigation + follow-up turns.

Run from the starter_code folder:
    python -m examples.worked_example                 # investigation + 3 follow-ups + 1 general question
    python -m examples.worked_example --quick         # investigation + 1 follow-up (fewest LLM calls)
    python -m examples.worked_example <anomaly_id>

Prints the conversation with a trace of graph nodes and tool calls, and saves it to
examples/worked_example_output.md AFTER EVERY TURN, so a rate-limit error part-way
through still leaves you with the turns that completed.
"""
from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage

from agent.graph import build_graph, text_of

DEFAULT_ANOMALY = "a1f0c8e2-1b44-4d90-9c31-000000000001"
OUT = Path(__file__).with_name("worked_example_output.md")


def run_turn(graph, config, text: str, log: list[str]) -> None:
    print(f"\n{'=' * 80}\nUSER: {text}\n{'-' * 80}")
    log.append(f"### User\n\n> {text}\n")
    trace = []
    for update in graph.stream({"messages": [HumanMessage(text)]}, config, stream_mode="updates"):
        for node, delta in update.items():
            line = node
            if node == "route":
                line += f" -> intent={(delta or {}).get('intent')}"
            scratch = (delta or {}).get("scratch")
            for m in scratch if isinstance(scratch, list) else []:
                if isinstance(m, AIMessage) and m.tool_calls:
                    line += " | " + ", ".join(f"{c['name']}({json.dumps(c['args'], default=str)})"
                                              for c in m.tool_calls)
            trace.append(line)
            print(f"  [trace] {line}", flush=True)
    answer = text_of(graph.get_state(config).values["messages"][-1])
    print(f"\nAGENT:\n{answer}")
    log.append("<details><summary>Graph trace</summary>\n\n```\n" + "\n".join(trace) + "\n```\n</details>\n")
    log.append(f"### Agent\n\n{answer}\n")


def save(graph, config, log: list[str]) -> None:
    report = graph.get_state(config).values.get("investigation")
    extra = []
    if report:
        extra = ["## Structured RCA (JSON)\n\n```json\n" + json.dumps(report, indent=2, default=str) + "\n```\n"]
    OUT.write_text("\n".join(log + extra), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("anomaly_id", nargs="?", default=DEFAULT_ANOMALY)
    ap.add_argument("--quick", action="store_true", help="investigation + one follow-up only")
    args = ap.parse_args()

    graph = build_graph()
    config = {"configurable": {"thread_id": f"worked-example-{uuid.uuid4()}"}, "recursion_limit": 60}
    log = [f"# Worked example - interface_flap anomaly `{args.anomaly_id}`\n"]

    turns = [
        f"Investigate anomaly {args.anomaly_id}",
        "What happened first - the errors or the link going down?",            # follow-up, no id repeated
        "Is any other device affected, or is this isolated?",                  # follow-up needing reasoning/data
        "How confident are you, and what would raise your confidence?",        # follow-up on calibration
        "In plain terms, what's a BGP flap and how is it different from an interface flap?",  # general
    ]
    if args.quick:
        turns = turns[:2]

    for i, t in enumerate(turns, 1):
        try:
            run_turn(graph, config, t, log)
        except Exception as e:  # e.g. provider rate limit: keep what we have
            msg = f"{type(e).__name__}: {str(e)[:300]}"
            print(f"\n[stopped at turn {i}/{len(turns)}] {msg}")
            if "429" in msg or "RESOURCE_EXHAUSTED" in msg or "rate" in msg.lower():
                print("Rate limit reached - set LLM_MODEL to a different model in .env, use --quick, or retry later.")
            log.append(f"_Run stopped at turn {i}: {msg}_\n")
            save(graph, config, log)
            print(f"Saved the {i - 1} completed turn(s) to {OUT}")
            return
        save(graph, config, log)
    print(f"\nSaved transcript to {OUT}")


if __name__ == "__main__":
    main()
