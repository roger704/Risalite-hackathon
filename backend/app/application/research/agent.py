"""Deep Agents assembly and execution for evidence-ledger research."""

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from deepagents import create_deep_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph.state import CompiledStateGraph

from app.application.research.events import EventSink
from app.application.research.ledger import EvidenceLedger, FindingStore
from app.application.research.prompts import ORCHESTRATOR_PROMPT, RESEARCHER_PROMPT
from app.application.research.tools import build_research_tools
from app.domain.research import ResearchConnector


@dataclass(frozen=True)
class ResearchRunResult:
    run_id: str
    report: str
    metrics: dict[str, Any]


def build_research_agent(
    orchestrator_model: BaseChatModel | str,
    researcher_model: BaseChatModel | str,
    tools: list[Any],
) -> CompiledStateGraph:
    return create_deep_agent(
        model=orchestrator_model,
        tools=tools,
        system_prompt=ORCHESTRATOR_PROMPT,
        subagents=[
            {
                "name": "researcher",
                "description": "Research one approved Berlin location-intelligence sub-question.",
                "system_prompt": RESEARCHER_PROMPT,
                "tools": tools,
                "model": researcher_model,
            }
        ],
        name="research-orchestrator",
    )


def _model_name(model: BaseChatModel | str) -> str:
    if isinstance(model, str):
        return model
    return str(getattr(model, "model", model.__class__.__name__))


def _message_text(message: AIMessage) -> str:
    if isinstance(message.content, str):
        return message.content
    if isinstance(message.content, list):
        return "\n".join(
            item["text"]
            for item in message.content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        )
    return ""


def _agent_name(namespace: tuple[str, ...]) -> str:
    return "researcher" if namespace else "orchestrator"


def _save_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


async def run_research(
    question: str,
    subquestions: list[str],
    *,
    connector: ResearchConnector,
    run_dir: Path,
    orchestrator_model: BaseChatModel | str,
    researcher_model: BaseChatModel | str,
    recursion_limit: int = 80,
) -> ResearchRunResult:
    output_dir = Path(run_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    ledger = EvidenceLedger(output_dir)
    findings = FindingStore(output_dir)
    events = EventSink(output_dir)
    tools = build_research_tools(connector, ledger, findings, events, "researcher")
    agent = build_research_agent(orchestrator_model, researcher_model, tools)
    plan = "\n".join(f"{index}. {item}" for index, item in enumerate(subquestions, 1))
    prompt = f"Question: {question}\n\nApproved plan:\n{plan}"
    report = ""
    model_calls = 0
    tokens_in = 0
    tokens_out = 0
    started = time.perf_counter()
    async for namespace, mode, data in agent.astream(
        {"messages": [HumanMessage(content=prompt)]},
        stream_mode=["updates"],
        subgraphs=True,
        config={"recursion_limit": recursion_limit},
    ):
        if mode != "updates" or not isinstance(data, dict):
            continue
        for node, state in data.items():
            if node != "model" or not isinstance(state, dict):
                continue
            messages = state.get("messages", [])
            if not messages:
                continue
            message = messages[-1]
            if not isinstance(message, AIMessage):
                continue
            role = _agent_name(namespace)
            selected_model = researcher_model if role == "researcher" else orchestrator_model
            usage = message.usage_metadata or {}
            input_tokens = usage.get("input_tokens")
            output_tokens = usage.get("output_tokens")
            await events.emit(
                "model_call",
                agent=role,
                model=message.response_metadata.get("model_name")
                or message.response_metadata.get("model")
                or _model_name(selected_model),
                tokens_in=input_tokens,
                tokens_out=output_tokens,
            )
            model_calls += 1
            tokens_in += input_tokens or 0
            tokens_out += output_tokens or 0
            text = _message_text(message)
            if text and not message.tool_calls:
                report = text
    seconds = time.perf_counter() - started
    (output_dir / "report.md").write_text(report.rstrip() + "\n", encoding="utf-8")
    citations = re.findall(r"\[(ev_[A-Za-z0-9_-]+)\]", report)
    accepted_evidence = {
        evidence_id for finding in findings.accepted for evidence_id in finding["evidence_ids"]
    }
    valid_citations = sum(citation in accepted_evidence for citation in citations)
    counts = {event_type: 0 for event_type in ("search", "extract", "finding", "finding_rejected")}
    for event in events.events:
        if event["type"] in counts:
            counts[event["type"]] += 1
    metrics = {
        "model_calls": model_calls,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "seconds": round(seconds, 3),
        "searches": counts["search"],
        "extracts": counts["extract"],
        "findings_accepted": len(findings.accepted),
        "findings_rejected": len(findings.rejected),
        "citations_total": len(citations),
        "citations_valid": valid_citations,
        "citation_validity_percent": (
            round(valid_citations / len(citations) * 100, 1) if citations else None
        ),
    }
    _save_json(output_dir / "metrics.json", metrics)
    return ResearchRunResult(events.run_id, report, metrics)
