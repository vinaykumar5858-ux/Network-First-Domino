"""CLI for the Network Investigation Agent.

Examples
  python main.py                                   # interactive chat
  python main.py --investigate a1f0c8e2-1b44-4d90-9c31-000000000001
  python main.py --thread ops-1                    # resume/name a conversation thread

In chat:  /new  start a fresh thread   /report  print last RCA as JSON   /quit  exit
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid

from agent import InvestigationAgent
from agent.preflight import check_environment, describe_backend, is_rate_limit


def preflight() -> None:
    """Fail fast with a clear message instead of a stack trace."""
    problems = check_environment()
    if problems:
        sys.exit("\n".join(problems))


def main() -> None:
    parser = argparse.ArgumentParser(description="Network Investigation Agent")
    parser.add_argument("--investigate", metavar="ANOMALY_ID", help="investigate this anomaly first")
    parser.add_argument("--thread", default=None, help="conversation thread id")
    args = parser.parse_args()
    preflight()

    agent = InvestigationAgent(thread_id=args.thread or str(uuid.uuid4()))
    print(f"Network Investigation Agent ({describe_backend()}) - type a question, an anomaly id, or /quit\n")

    if args.investigate:
        print(agent.investigate(args.investigate), "\n")

    while True:
        try:
            text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text:
            continue
        if text in ("/quit", "/exit"):
            break
        if text == "/new":
            agent = InvestigationAgent(thread_id=str(uuid.uuid4()), graph=agent.graph)
            print("(new conversation)\n")
            continue
        if text == "/report":
            print(json.dumps(agent.state.get("investigation"), indent=2, default=str), "\n")
            continue
        try:
            print(f"\nagent> {agent.ask(text)}\n")
        except Exception as e:  # keep the REPL alive on API/DB errors
            hint = " (LLM rate limit - wait, or set LLM_MODEL to another model)" if is_rate_limit(e) else ""
            print(f"\n[error]{hint} {type(e).__name__}: {str(e)[:400]}\n")


if __name__ == "__main__":
    main()
