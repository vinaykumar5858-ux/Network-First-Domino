"""Runtime configuration: LLM provider selection and agent limits.

Everything is driven by environment variables (optionally loaded from a .env
file) so the same code runs in VS Code on the host or inside the Jupyter
container without edits.
"""
from __future__ import annotations

import os

try:  # optional convenience
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

# --- agent limits -----------------------------------------------------------
MAX_INVESTIGATION_STEPS = int(os.getenv("MAX_INVESTIGATION_STEPS", "8"))  # LLM<->tool rounds
MAX_QA_STEPS = int(os.getenv("MAX_QA_STEPS", "5"))
HISTORY_WINDOW = int(os.getenv("HISTORY_WINDOW", "10"))  # chat messages fed back to the LLM
TOOL_OUTPUT_CHAR_LIMIT = int(os.getenv("TOOL_OUTPUT_CHAR_LIMIT", "12000"))

_DEFAULT_MODELS = {
    "gemini": "gemini-2.5-flash",
    "openai": "gpt-4.1-mini",
    "anthropic": "claude-sonnet-4-5",
}


def get_llm(temperature: float = 0.0):
    """Return a LangChain chat model for the provider in LLM_PROVIDER.

    LLM_PROVIDER = gemini (default) | openai | anthropic
    LLM_MODEL    = optional model override
    API keys come from the provider's usual variable:
      GOOGLE_API_KEY / OPENAI_API_KEY / ANTHROPIC_API_KEY
    """
    provider = os.getenv("LLM_PROVIDER", "gemini").lower()
    model = os.getenv("LLM_MODEL") or _DEFAULT_MODELS.get(provider)

    if provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(model=model, temperature=temperature)
    if provider == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(model=model, temperature=temperature)
    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(model=model, temperature=temperature)
    raise ValueError(f"Unknown LLM_PROVIDER '{provider}' (use gemini, openai or anthropic)")
