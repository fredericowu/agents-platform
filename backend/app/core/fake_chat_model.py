"""Offline chat model that emulates tool calling. Used by tests so the
LangGraph agent loop can be exercised without real LLM credentials.

Behavior: deterministic, scripted. The first call returns a tool_call to
``write_file`` (if available) with content derived from the user message;
the second call returns a short text confirmation. Cycle ends.
"""
from __future__ import annotations

import uuid
from typing import Any, Iterator, List, Optional, Sequence

from langchain_core.callbacks.manager import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.tools import BaseTool
from pydantic import Field


class FakeToolChat(BaseChatModel):
    """A scripted chat model. On call N (0-based) it returns the Nth response
    from ``script``. Each response is either:
        ("text",       "the text content")
        ("tool_call",  "tool_name", {"arg":...})
        ("done",       "final text")
    """

    script: List[tuple] = Field(default_factory=list)
    bound_tools: List[BaseTool] = Field(default_factory=list)
    call_index: int = 0

    def bind_tools(self, tools, **_: Any) -> "FakeToolChat":
        # Return a new instance with the tools bound; mimic LangChain pattern
        new = self.copy(update={"bound_tools": list(tools), "call_index": self.call_index})
        return new

    def copy(self, update: dict | None = None) -> "FakeToolChat":  # type: ignore[override]
        data = self.model_dump()
        if update:
            data.update(update)
        return FakeToolChat(**data)

    @property
    def _llm_type(self) -> str:
        return "fake-tool-chat"

    def _next(self) -> tuple:
        if not self.script:
            return ("done", "ok")
        idx = min(self.call_index, len(self.script) - 1)
        self.call_index += 1
        return self.script[idx]

    def _emit(self, item: tuple) -> AIMessage:
        kind = item[0]
        if kind == "tool_call":
            return AIMessage(
                content="",
                tool_calls=[{
                    "id": f"call_{uuid.uuid4().hex[:8]}",
                    "name": item[1],
                    "args": item[2] if len(item) > 2 else {},
                }],
                usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            )
        return AIMessage(
            content=item[1] if len(item) > 1 else "ok",
            usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        )

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        msg = self._emit(self._next())
        return ChatResult(generations=[ChatGeneration(message=msg)])

    async def _agenerate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[AsyncCallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        return self._generate(messages, stop=stop, **kwargs)

    def _stream(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        msg = self._emit(self._next())
        yield ChatGenerationChunk(message=AIMessageChunk(content=msg.content or "",
                                                          tool_calls=getattr(msg, "tool_calls", []),
                                                          usage_metadata=getattr(msg, "usage_metadata", None)))
