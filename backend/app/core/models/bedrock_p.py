"""AWS Bedrock provider via langchain-aws."""
from __future__ import annotations

import os
from typing import Any, AsyncIterator

from . import BaseLLM, ChatChunk


class BedrockLLM(BaseLLM):
    provider = "bedrock"

    def __init__(self, model_id: str, region: str | None = None, **params: Any) -> None:
        from langchain_aws import ChatBedrock
        region = region or os.environ.get("AWS_REGION", "us-east-1")
        self._llm = ChatBedrock(model_id=model_id, region_name=region, **params)

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
            if isinstance(text, list):
                text = "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in text)
            meta = getattr(chunk, "usage_metadata", None) or {}
            tin = max(tin, meta.get("input_tokens", 0))
            tout = max(tout, meta.get("output_tokens", 0))
            if text:
                yield ChatChunk(delta=text, tokens_in=tin, tokens_out=tout)
        yield ChatChunk(delta="", finish=True, tokens_in=tin, tokens_out=tout)
