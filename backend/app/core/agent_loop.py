"""LangGraph-powered agent loop for API-direct providers.

Used for ``anthropic`` / ``openai`` / ``bedrock`` and any other LangChain
chat model that supports ``bind_tools``. The CLI-subshell path remains
unchanged and handles its own native tools.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

EmitFn = Callable[[str, dict, str | None], Awaitable[None]]


@dataclass
class AgentResult:
    text: str
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0


def _build_chat_model(provider: str, model_id: str, params: dict[str, Any]):
    """Construct a tool-binding-capable chat model for the given provider.
    Raises if the provider doesn't support tool calling."""
    p = (provider or "").lower()
    if p == "anthropic":
        from langchain_anthropic import ChatAnthropic
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        return ChatAnthropic(model=model_id, **{k: v for k, v in params.items() if k in {"temperature", "max_tokens", "top_p", "top_k"}})
    if p == "openai":
        from langchain_openai import ChatOpenAI
        allowed = {k: v for k, v in params.items()
                   if k in {"temperature", "max_tokens", "top_p", "base_url", "api_key"}}
        # A ``base_url`` means an OpenAI-COMPATIBLE endpoint (e.g. this platform's
        # own /v1 surface, Ollama, vLLM) — those don't need the real OpenAI key,
        # so accept a placeholder instead of hard-failing.
        if not os.environ.get("OPENAI_API_KEY") and not allowed.get("api_key"):
            if allowed.get("base_url"):
                allowed["api_key"] = "sk-local"
            else:
                raise RuntimeError("OPENAI_API_KEY not set")
        return ChatOpenAI(model=model_id, **allowed)
    if p == "bedrock":
        # Use the Converse API for tool calling — the older ChatBedrock won't
        # bind tools across all models.
        try:
            from langchain_aws import ChatBedrockConverse
        except ImportError:
            from langchain_aws import ChatBedrock as ChatBedrockConverse  # fallback
        region = params.get("region") or os.environ.get("AWS_REGION", "us-east-1")
        return ChatBedrockConverse(model=model_id, region_name=region)
    if p == "fake":
        # Test-only provider used by the unit suite.
        from .fake_chat_model import FakeToolChat
        return FakeToolChat(**params)
    raise RuntimeError(f"provider {provider!r} doesn't support LangChain tool binding")


def _to_lc_messages(system: str, prior: list[dict], user_msg: str) -> list:
    out: list = []
    if system:
        out.append(SystemMessage(content=system))
    for m in prior or []:
        r, c = m.get("role"), m.get("content", "")
        if r == "system":
            out.append(SystemMessage(content=c))
        elif r == "assistant":
            out.append(AIMessage(content=c))
        elif r == "tool":
            out.append(ToolMessage(content=c, tool_call_id=m.get("tool_call_id", "")))
        else:
            out.append(HumanMessage(content=c))
    out.append(HumanMessage(content=user_msg))
    return out


def _provider_supports_langchain(provider: str) -> bool:
    return (provider or "").lower() in {"anthropic", "openai", "bedrock", "fake"}


async def run_langchain_agent(
    *,
    provider: str,
    model_id: str,
    params: dict[str, Any],
    system_prompt: str,
    extra_messages: list[dict],
    user_message: str,
    tools: list,
    emit: EmitFn,
    node_id: str | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> AgentResult:
    """Run the user_message through a LangGraph ReAct agent. Emits platform
    events via ``emit(kind, payload, node_id)``.
    """
    from langgraph.prebuilt import create_react_agent

    llm = _build_chat_model(provider, model_id, params)
    agent = create_react_agent(llm, tools=tools)
    messages = _to_lc_messages(system_prompt, extra_messages, user_message)

    text = ""
    tin = tout = 0
    cost = 0.0
    tool_calls_by_id: dict[str, dict] = {}

    async for event in agent.astream_events({"messages": messages}, version="v2"):
        if cancel_check and cancel_check():
            await emit("error", {"error": "cancelled"}, node_id)
            break
        et = event.get("event")
        data = event.get("data") or {}

        if et == "on_chat_model_stream":
            chunk = data.get("chunk")
            content = getattr(chunk, "content", "") or ""
            if isinstance(content, list):
                # Anthropic returns blocks; concat any text parts
                content = "".join(
                    b.get("text", "") if isinstance(b, dict) and b.get("type") == "text"
                    else "" for b in content)
            if content:
                text += content
                await emit("llm_token", {"delta": content}, node_id)
            usage = getattr(chunk, "usage_metadata", None) or {}
            if usage:
                tin = max(tin, usage.get("input_tokens", 0))
                tout = max(tout, usage.get("output_tokens", 0))

        elif et == "on_chat_model_end":
            output = data.get("output")
            usage = getattr(output, "usage_metadata", None) or {}
            if usage:
                tin = max(tin, usage.get("input_tokens", 0))
                tout = max(tout, usage.get("output_tokens", 0))
            # capture any tool calls
            tc_list = getattr(output, "tool_calls", None) or []
            for tc in tc_list:
                tcid = tc.get("id") if isinstance(tc, dict) else tc.id
                name = tc.get("name") if isinstance(tc, dict) else tc.name
                args = tc.get("args") if isinstance(tc, dict) else tc.args
                tool_calls_by_id[tcid] = {"name": name, "args": args}
                await emit("tool_call", {"id": tcid, "name": name, "input": args}, node_id)

        elif et == "on_tool_start":
            name = event.get("name") or ""
            inp = data.get("input") or {}
            await emit("tool_call", {"id": event.get("run_id", ""), "name": name, "input": inp}, node_id)

        elif et == "on_tool_end":
            output = data.get("output")
            content = output if isinstance(output, str) else _safe_str(output)
            await emit("tool_result", {
                "tool_use_id": event.get("run_id", ""),
                "name": event.get("name", ""),
                "content": content[:1500] if isinstance(content, str) else str(content)[:1500],
            }, node_id)

        elif et == "on_chain_end" and event.get("name") in {"LangGraph", "agent"}:
            # Final state may include the last AI message
            output = data.get("output") or {}
            msgs = output.get("messages") if isinstance(output, dict) else None
            if msgs:
                last = msgs[-1]
                content = getattr(last, "content", "")
                if isinstance(content, list):
                    content = "".join(b.get("text", "") if isinstance(b, dict) and b.get("type") == "text" else "" for b in content)
                if content and not text:
                    text = content

    return AgentResult(text=text, tokens_in=tin, tokens_out=tout, cost_usd=cost)


def _safe_str(x: Any) -> str:
    try:
        return str(x)
    except Exception:
        return "<unprintable>"
