"""Environment checks shared by the CLI and the web UI."""
from __future__ import annotations

import os

from . import db_access

PROVIDER_KEYS = {"gemini": ("GOOGLE_API_KEY", "GEMINI_API_KEY"), "openai": ("OPENAI_API_KEY",),
                 "anthropic": ("ANTHROPIC_API_KEY",)}


def check_environment() -> list[str]:
    """Return a list of human-readable problems (empty list = ready to run)."""
    problems: list[str] = []
    provider = os.getenv("LLM_PROVIDER", "gemini").lower()
    keys = PROVIDER_KEYS.get(provider)
    if keys is None:
        problems.append(f"LLM_PROVIDER='{provider}' is not supported - use gemini, openai or anthropic.")
    elif not any(os.getenv(k) for k in keys):
        problems.append(f"No API key for LLM_PROVIDER={provider}: set {keys[0]} in .env")
    try:
        db_access.query("SELECT 1 AS ok")
    except FileNotFoundError as e:
        problems.append(str(e))
        return problems
    except Exception as e:
        problems.append(f"Cannot reach the {db_access.backend()} database: {type(e).__name__}: {e}. "
                        "Check the DB settings in .env (or set DB_BACKEND=duckdb to work without the container).")
        return problems
    missing = [t for t in db_access.TABLES if not db_access.table_columns(t)]
    if missing:
        problems.append(f"Database is reachable but these tables are missing: {', '.join(missing)}")
    return problems


def describe_backend() -> str:
    if db_access.backend() == "duckdb":
        return f"DuckDB ({db_access.duckdb_path()})"
    return f"PostgreSQL ({os.getenv('PGHOST', 'localhost')}:{os.getenv('PGPORT', '5432')})"


def is_rate_limit(error: Exception) -> bool:
    text = f"{type(error).__name__} {error}".lower()
    return any(k in text for k in ("429", "resource_exhausted", "ratelimit", "rate limit", "quota"))
