"""Multi-provider model registry. Wraps LangChain chat models and returns a uniform invoker."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, AsyncIterator

from ...config import settings


@dataclass
class ChatChunk:
    delta: str
    finish: bool = False
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0


class BaseLLM:
    """Common interface. Each provider implements .astream(messages, **params)."""

    provider: str = "base"

    async def astream(self, messages: list[dict], **params: Any) -> AsyncIterator[ChatChunk]:
        raise NotImplementedError
        yield  # pragma: no cover

    async def ainvoke(self, messages: list[dict], **params: Any) -> ChatChunk:
        text = ""
        tin = tout = 0
        cost = 0.0
        async for chunk in self.astream(messages, **params):
            text += chunk.delta
            tin = max(tin, chunk.tokens_in)
            tout = max(tout, chunk.tokens_out)
            cost = max(cost, chunk.cost_usd)
        return ChatChunk(delta=text, finish=True, tokens_in=tin, tokens_out=tout, cost_usd=cost)


# Echo provider — works without any API keys, for first-boot smoke tests
class EchoLLM(BaseLLM):
    provider = "echo"

    def __init__(self, model_id: str = "echo", **_: Any) -> None:
        self.model_id = model_id

    async def astream(self, messages: list[dict], **params: Any) -> AsyncIterator[ChatChunk]:
        # echo the last user message in fake "chunks"
        last = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                last = str(m.get("content", ""))
                break
        # cap input echo to prevent quadratic blow-up in feedback loops (group_chat)
        if len(last) > 240:
            last = last[:120] + "…" + last[-80:]
        prefix = f"[{self.model_id}] echo > "
        text = prefix + last if last else f"[{self.model_id}] (no input)"
        words = text.split(" ")
        for i, w in enumerate(words):
            await asyncio.sleep(0.005)
            yield ChatChunk(delta=w + (" " if i < len(words) - 1 else ""))
        tin = sum(len(str(m.get("content", "")).split()) for m in messages)
        tout = len(text.split())
        yield ChatChunk(delta="", finish=True, tokens_in=tin, tokens_out=tout, cost_usd=0.0)


def make_llm(provider: str, model_id: str, **params: Any) -> BaseLLM:
    """Factory. Falls back to EchoLLM if a real provider can't be initialized.

    Security: when the effective security_mode is ``secure``, the claude CLI
    subshell loses ``--dangerously-skip-permissions`` and gains
    ``--disallowed-tools Bash`` so the inner agent can't shell out.
    """
    provider = (provider or settings.default_provider).lower()
    try:
        if provider == "anthropic":
            from .anthropic_p import AnthropicLLM
            return AnthropicLLM(model_id=model_id, **params)
        if provider == "openai":
            from .openai_p import OpenAILLM
            return OpenAILLM(model_id=model_id, **params)
        if provider == "bedrock":
            from .bedrock_p import BedrockLLM
            return BedrockLLM(model_id=model_id, **params)
        if provider == "cli_subshell":
            from .cli_subshell import CliSubshellLLM
            from .. import security
            # Resolve effective mode using the agent's params (already merged
            # into **params**) and apply gating to the CLI invocation.
            eff = security.effective_for_agent(params)
            params = dict(params)   # avoid mutating caller's dict
            if eff["mode"] == "secure":
                params["dangerous_skip_permissions"] = False
                # Append to any existing disallowed_tools list — don't replace.
                disallowed = list(params.get("disallowed_tools") or [])
                if "Bash" not in disallowed:
                    disallowed.append("Bash")
                params["disallowed_tools"] = disallowed
            return CliSubshellLLM(model_id=model_id, **params)
    except Exception as e:
        print(f"[models] provider {provider} init failed ({e}); falling back to echo")
    return EchoLLM(model_id=model_id, **params)
