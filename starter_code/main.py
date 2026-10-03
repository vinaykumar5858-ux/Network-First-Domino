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
import os
import sys
import uuid

from agent import InvestigationAgent
from agent import db_access


_KEYS = {"gemini": ("GOOGLE_API_KEY", "GEMINI_API_KEY"), "openai": ("OPENAI_API_KEY",),
         "anthropic": ("ANTHROPIC_API_KEY",)}


def preflight() -> None:
    """Fail fast with a clear message instead of a stack trace."""
    provider = os.getenv("LLM_PROVIDER", "gemini").lower()
    keys = _KEYS.get(provider)
    if keys is None:
        sys.exit(f"LLM_PROVIDER='{provider}' is not supported - use gemini, openai or anthropic.")
    if not any(os.getenv(k) for k in keys):
        sys.exit(f"No API key for LLM_PROVIDER={provider}: set {keys[0]} in .env")
    try:
        db_access.query("SELECT 1 AS ok")
    except FileNotFoundError as e:
        sys.exit(str(e))
    except Exception as e:
        sys.exit(f"Cannot reach the {db_access.backend()} database: {type(e).__name__}: {e}\n"
                 "Check the DB settings in .env (or set DB_BACKEND=duckdb to work without the container).")
    missing = [t for t in db_access.TABLES if not db_access.table_columns(t)]
    if missing:
        sys.exit(f"Database is reachable but these tables are missing: {', '.join(missing)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Network Investigation Agent")
    parser.add_argument("--investigate", metavar="ANOMALY_ID", help="investigate this anomaly first")
    parser.add_argument("--thread", default=None, help="conversation thread id")
    args = parser.parse_args()
    preflight()

    agent = InvestigationAgent(thread_id=args.thread or str(uuid.uuid4()))
    where = (f"duckdb file {db_access.duckdb_path()}" if db_access.backend() == "duckdb"
             else "postgres")
    print(f"Network Investigation Agent ({where}) - type a question, an anomaly id, or /quit\n")

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
            print(f"\n[error] {type(e).__name__}: {e}\n")


if __name__ == "__main__":
    main()
