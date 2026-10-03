"""Optional deliverable #4: lightweight evaluation of the agent's own output.

    python -m eval.run_eval --limit 10

Checks per anomaly (automatic, no labels needed):
  completed          the investigation finished and produced a valid structured report
  device_grounded    every affected device exists in network_devices
  anomaly_device_hit the anomaly's own device appears among affected devices
  evidence_sources   number of distinct tools that returned data
  confidence         the (calibrated) confidence level
Plus routing checks: general questions must not touch the DB; follow-ups must use context.
If detected_anomalies has a label-like column (e.g. root_cause/ground_truth) it is printed
next to the agent's answer for manual or LLM-judge comparison.
Results go to eval/results.json and eval/results.md.
"""
from __future__ import annotations

import argparse
import json
import time
import uuid
from pathlib import Path

from langchain_core.messages import HumanMessage

from agent import anomalies as anomaly_info
from agent import db_access as db
from agent.graph import build_graph
from agent.tools import _resolve_device

ROUTING_CASES = [
    ("What's a BGP flap, in plain terms?", "general"),
    ("Explain the difference between CRC errors and input drops.", "general"),
    ("Which device had the most anomalies?", "data_question"),
]


def label_columns() -> list[str]:
    return [c for c, _ in db.table_columns("detected_anomalies")
            if any(k in c for k in ("label", "ground_truth", "root_cause", "expected", "cause"))]


def investigate(graph, anomaly_id: str) -> dict:
    cfg = {"configurable": {"thread_id": str(uuid.uuid4())}, "recursion_limit": 60}
    t0 = time.time()
    try:
        graph.invoke({"messages": [HumanMessage(f"Investigate anomaly {anomaly_id}")]}, cfg)
        state = graph.get_state(cfg).values
        return {"state": state, "seconds": round(time.time() - t0, 1), "error": None}
    except Exception as e:
        return {"state": {}, "seconds": round(time.time() - t0, 1), "error": f"{type(e).__name__}: {e}"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=10)
    args = ap.parse_args()

    graph = build_graph()
    id_col = db.anomaly_id_column()
    labels = label_columns()
    anomalies = db.query(f"SELECT * FROM detected_anomalies LIMIT %s", [args.limit])

    rows = []
    for a in anomalies:
        aid = str(a[id_col])
        print(f"investigating {aid} ...", flush=True)
        res = investigate(graph, aid)
        inv = res["state"].get("investigation") or {}
        devices = inv.get("affected_devices", [])
        resolved = [_resolve_device(d) for d in devices]
        summary = anomaly_info.summarize(a)
        anomaly_devs = [d for d in (_resolve_device(x) for x in summary["devices"]) if d]
        rows.append({
            "anomaly_id": aid,
            "detector": summary["detector"],
            "completed": bool(inv) and res["error"] is None,
            "error": res["error"],
            "seconds": res["seconds"],
            "confidence": inv.get("confidence"),
            "root_cause": inv.get("root_cause"),
            "evidence_sources": len(inv.get("evidence_sources_with_data", [])),
            "tool_calls": len(inv.get("tool_calls", [])),
            "device_grounded": bool(devices) and all(resolved),
            "anomaly_device_hit": any(d in resolved for d in anomaly_devs),
            "calibration_notes": inv.get("calibration_notes"),
            "labels": {c: a.get(c) for c in labels},
        })

    routing = []
    for question, expected in ROUTING_CASES:
        cfg = {"configurable": {"thread_id": str(uuid.uuid4())}, "recursion_limit": 40}
        nodes = []
        for upd in graph.stream({"messages": [HumanMessage(question)]}, cfg, stream_mode="updates"):
            nodes += list(upd)
        intent = graph.get_state(cfg).values.get("intent")
        routing.append({"question": question, "expected": expected, "got": intent,
                        "used_tools": "execute_tools" in nodes, "pass": intent == expected})

    out_dir = Path(__file__).parent
    (out_dir / "results.json").write_text(json.dumps({"investigations": rows, "routing": routing},
                                                     indent=2, default=str))
    n = len(rows) or 1
    md = ["# Evaluation results", "",
          f"- completed: {sum(r['completed'] for r in rows)}/{len(rows)}",
          f"- devices grounded in inventory: {sum(r['device_grounded'] for r in rows)}/{len(rows)}",
          f"- anomaly's own device identified: {sum(r['anomaly_device_hit'] for r in rows)}/{len(rows)}",
          f"- mean evidence sources with data: {sum(r['evidence_sources'] for r in rows) / n:.1f}",
          f"- mean latency: {sum(r['seconds'] for r in rows) / n:.1f}s",
          f"- routing: {sum(r['pass'] for r in routing)}/{len(routing)} correct; general questions "
          f"using tools: {sum(r['used_tools'] for r in routing if r['expected'] == 'general')}",
          "", "| anomaly | detector | confidence | sources | grounded | root cause | label |",
          "|---|---|---|---|---|---|---|"]
    for r in rows:
        md.append(f"| {r['anomaly_id'][-6:]} | {r['detector']} | {r['confidence']} | {r['evidence_sources']} | "
                  f"{'yes' if r['device_grounded'] else 'no'} | {(r['root_cause'] or r['error'] or '')[:90]} | "
                  f"{json.dumps(r['labels'], default=str)[:60]} |")
    (out_dir / "results.md").write_text("\n".join(md))
    print("\n".join(md))


if __name__ == "__main__":
    main()
