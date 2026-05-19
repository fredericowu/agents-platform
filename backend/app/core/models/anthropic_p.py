"""Anthropic provider. Uses langchain-anthropic under the hood."""
from __future__ import annotations

import os
from typing import Any, AsyncIterator

from . import BaseLLM, ChatChunk


class AnthropicLLM(BaseLLM):
    provider = "anthropic"

    def __init__(self, model_id: str, **params: Any) -> None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        from langchain_anthropic import ChatAnthropic
        self._llm = ChatAnthropic(model=model_id, **params)

    async def astream(self, messages: list[dict], **params: Any) -> AsyncIterator[ChatChunk]:
        from langchain_core.messages import HumanMessage, SystemMessage, AIMessage
        lc = []
        for m in messages:
            r, c = m.get("role"), m.get("content", "")
            if r == "system":
                lc.append(SystemMessage(content=c))
            elif r == "assistant":
                lc.append(AIMessage(content=c))
            else:
                lc.append(HumanMessage(content=c))
        tin = tout = 0
        async for chunk in self._llm.astream(lc):
            text = getattr(chunk, "content", "") or ""
            if isinstance(text, list):  # claude returns list of blocks sometimes
                text = "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in text)
            meta = getattr(chunk, "usage_metadata", None) or {}
            tin = max(tin, meta.get("input_tokens", 0))
            tout = max(tout, meta.get("output_tokens", 0))
            if text:
                yield ChatChunk(delta=text, tokens_in=tin, tokens_out=tout)
        yield ChatChunk(delta="", finish=True, tokens_in=tin, tokens_out=tout)
