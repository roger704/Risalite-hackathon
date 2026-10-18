"""Run one deepagents researcher under hard budgets; always returns what was recorded."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from deepagents import create_deep_agent
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.errors import GraphRecursionError

from app.contracts.evidence import Finding
from app.research.events import clean_error
from app.research.models import model_name, usage
from app.research.toolkit import TOOL_NAMES, Toolkit


def _tool_name(tool: Any) -> str | None:
    if isinstance(tool, dict):
        name = tool.get("name") or tool.get("function", {}).get("name")
        return name if isinstance(name, str) else None
    return getattr(tool, "name", None)


class BudgetGuard(AgentMiddleware):
    """Offers only the research tools, emits model_call events and ends the loop on budget stop."""

    def __init__(self, toolkit: Toolkit, model: str):
        super().__init__()
        self.toolkit = toolkit
        self.model = model
        self.calls = 0

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        budget = self.toolkit.budget
        if budget.stop_reason is not None:
            if budget.grace_model_calls <= 0:
                return AIMessage(content=f"Stopped: {budget.stop_reason}.")
            budget.grace_model_calls -= 1
        tools = [t for t in request.tools if _tool_name(t) in TOOL_NAMES]
        request = request.override(tools=tools)
        started = time.perf_counter()
        response = await handler(request)
        self.calls += 1
        messages = getattr(response, "result", None) or [response]
        message = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
        tokens_in, tokens_out = usage(message) if message is not None else (None, None)
        meta = getattr(message, "response_metadata", None) or {}
        await self.toolkit.events(
            "model_call",
            detail={
                "call": self.calls,
                "tool_calls": len(getattr(message, "tool_calls", []) or []),
            },
            model=meta.get("model_name") or meta.get("model") or self.model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            seconds=time.perf_counter() - started,
        )
        return response

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        call = request.tool_call
        if call["name"] not in TOOL_NAMES:
            return ToolMessage(
                content=f"Tool {call['name']!r} is not available. Use {', '.join(TOOL_NAMES)}.",
                tool_call_id=call["id"],
                name=call["name"],
                status="error",
            )
        return await handler(request)


async def run_agent(
    *,
    toolkit: Toolkit,
    model: BaseChatModel,
    model_label: str,
    system_prompt: str,
    task: str,
) -> list[Finding]:
    cfg = toolkit.cfg
    agent = create_deep_agent(
        model=model,
        tools=toolkit.tools(),
        system_prompt=system_prompt,
        middleware=[BudgetGuard(toolkit, model_name(model, model_label))],
        name=toolkit.events.agent.replace(":", "-"),
    )
    reason: str | None = None
    try:
        async with asyncio.timeout(cfg.timeout_s):
            await agent.ainvoke(
                {"messages": [HumanMessage(content=task)]},
                config={"recursion_limit": cfg.recursion_limit},
            )
    except TimeoutError:
        reason = "timeout"
    except GraphRecursionError:
        reason = "recursion_limit"
    except Exception as error:
        await toolkit.events(
            "tool_error",
            why="agent run failed",
            detail={"tool": "agent", "error": clean_error(error)},
        )
    if reason is not None:
        limit = cfg.timeout_s if reason == "timeout" else cfg.recursion_limit
        await toolkit.events(
            "budget_exhausted",
            why=f"{reason} reached; keeping recorded findings",
            detail={"resource": reason, "limit": limit},
        )
    return list(toolkit.findings)
