#!/usr/bin/env python3
"""Run the deepagents research spike and optional cookbook baseline."""

import argparse
import asyncio
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from deepagents import create_deep_agent
from langchain_core.messages import AIMessage, ToolMessage
from langchain_nebius import ChatNebius
from langchain_tavily import TavilySearch

from app.application.research.agent import _message_text, run_research
from app.application.research.models import nebius_chat_model
from app.connectors.tavily import TavilyClient, TavilyConfig
from app.domain.research import ResearchBudget

DEFAULT_QUESTION = "Where is it smart to open a coffee place in Kreuzberg?"
DEFAULT_SUBQUESTIONS = [
    "What do IHK guidance, retail data, and the Zentrenkonzept say about cafés in Friedrichshain-Kreuzberg?",
    "Which residential or office developments in Kreuzberg are planned after today, and where?",
    "What are current commercial rent levels and café competition in Kreuzberg Kieze?",
]


def _slug(model: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", model.casefold()).strip("-")


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


async def _run_baseline(model_name: str, question: str, out_dir: Path) -> dict[str, Any]:
    model = ChatNebius(
        model=model_name,
        base_url=os.getenv("NEBIUS_BASE_URL", "https://api.tokenfactory.nebius.com/v1"),
        api_key=os.getenv("NEBIUS_API_KEY"),
        temperature=0,
    )
    agent = create_deep_agent(
        model=model,
        tools=[TavilySearch(max_results=5, search_depth="advanced")],
    )
    report = ""
    model_calls = 0
    tokens_in = 0
    tokens_out = 0
    searches = 0
    started = time.perf_counter()
    async for namespace, mode, data in agent.astream(
        {"messages": [("user", question)]},
        stream_mode=["updates"],
        subgraphs=True,
        config={"recursion_limit": 80},
    ):
        if mode != "updates" or not isinstance(data, dict):
            continue
        for node, state in data.items():
            if not isinstance(state, dict):
                continue
            messages = state.get("messages", [])
            for message in messages:
                if isinstance(message, AIMessage) and node == "model":
                    model_calls += 1
                    usage = message.usage_metadata or {}
                    tokens_in += usage.get("input_tokens", 0)
                    tokens_out += usage.get("output_tokens", 0)
                    text = _message_text(message)
                    if text and not message.tool_calls:
                        report = text
                if isinstance(message, ToolMessage):
                    searches += 1
    seconds = time.perf_counter() - started
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.md").write_text(report.rstrip() + "\n", encoding="utf-8")
    metrics = {
        "model_calls": model_calls,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "seconds": round(seconds, 3),
        "searches": searches,
        "extracts": 0,
        "findings_accepted": 0,
        "findings_rejected": 0,
        "citations_total": 0,
        "citations_valid": 0,
        "citation_validity_percent": None,
    }
    _write_json(out_dir / "metrics.json", metrics)
    return metrics


async def _run(args: argparse.Namespace, models: list[str], subquestions: list[str]) -> None:
    output_root = Path(args.out)
    rows: list[tuple[str, dict[str, Any]]] = []
    for model_name in models:
        connector = TavilyClient(
            TavilyConfig.from_env(search_depth="advanced", max_results=5),
            ResearchBudget(
                max_searches=40,
                max_extract_urls=100,
                max_inference_calls=80,
                max_tokens=100_000,
            ),
        )
        async with connector:
            result = await run_research(
                args.question,
                subquestions,
                connector=connector,
                run_dir=output_root / _slug(model_name),
                orchestrator_model=nebius_chat_model("orchestrator", model_name),
                researcher_model=nebius_chat_model("researcher", model_name),
            )
        rows.append((model_name, result.metrics))
    if args.baseline:
        baseline_model = models[0]
        metrics = await _run_baseline(
            baseline_model,
            args.question,
            output_root / "cookbook-baseline",
        )
        rows.append((f"cookbook: {baseline_model}", metrics))
    _print_table(rows)


def _print_table(rows: list[tuple[str, dict[str, Any]]]) -> None:
    print(
        f"{'model':38} {'seconds':>8} {'calls':>6} {'tokens':>8} "
        f"{'accepted':>9} {'rejected':>8} {'citation validity':>17}"
    )
    for model, metrics in rows:
        validity = metrics["citation_validity_percent"]
        validity_text = "n/a" if validity is None else f"{validity:.1f}%"
        tokens = metrics["tokens_in"] + metrics["tokens_out"]
        print(
            f"{model[:38]:38} {metrics['seconds']:8.1f} {metrics['model_calls']:6d} "
            f"{tokens:8d} {metrics['findings_accepted']:9d} "
            f"{metrics['findings_rejected']:8d} {validity_text:>17}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--question", default=DEFAULT_QUESTION)
    parser.add_argument(
        "--subquestion",
        action="append",
        help="Research plan sub-question; may be repeated.",
    )
    parser.add_argument("--models", help="Comma-separated Nebius model IDs for a bake-off.")
    parser.add_argument(
        "--out",
        default=f"runs/spike-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        help="Output root; each model gets a slugged subdirectory.",
    )
    parser.add_argument("--baseline", action="store_true", help="Also run the cookbook baseline.")
    args = parser.parse_args()
    missing = [name for name in ("TAVILY_API_KEY", "NEBIUS_API_KEY") if not os.getenv(name)]
    if missing:
        parser.error("Missing required environment variable(s): " + ", ".join(missing))
    models = (
        [item.strip() for item in args.models.split(",") if item.strip()]
        if args.models
        else [os.getenv("NEBIUS_MODEL", "MiniMaxAI/MiniMax-M3")]
    )
    if not models:
        parser.error("--models must contain at least one model ID")
    subquestions = args.subquestion or DEFAULT_SUBQUESTIONS
    asyncio.run(_run(args, models, subquestions))


if __name__ == "__main__":
    main()
