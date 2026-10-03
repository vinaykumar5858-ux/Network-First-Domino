"""A scripted stand-in for a chat model so graph control flow can be tested offline."""
from __future__ import annotations

from langchain_core.messages import AIMessage


class ScriptedLLM:
    """Returns pre-scripted responses in order; records every prompt it receives.

    ``chat`` items: AIMessage (or str) returned by invoke() / bound-tools invoke().
    ``structured`` items: pydantic objects returned by with_structured_output(...).invoke().
    """

    def __init__(self, chat=None, structured=None):
        self.chat = list(chat or [])
        self.structured = list(structured or [])
        self.calls: list[list] = []

    def bind_tools(self, tools, **kwargs):
        return self

    def invoke(self, messages, *args, **kwargs):
        self.calls.append(messages)
        nxt = self.chat.pop(0)
        return AIMessage(content=nxt) if isinstance(nxt, str) else nxt

    def with_structured_output(self, schema, **kwargs):
        parent = self

        class _S:
            def invoke(self, messages, *a, **k):
                parent.calls.append(messages)
                item = parent.structured.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item

        return _S()


def tool_call(name: str, args: dict, call_id: str = "c1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}])
