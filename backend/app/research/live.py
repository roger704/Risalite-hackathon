"""Bounded live web research runs."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.research import (
    BudgetExceededError,
    ExtractionStatus,
    ResearchBudget,
    ResearchExtract,
    ResearchSearch,
    WebEvidence,
)
from app.evidence.factcheck import FactChecker, fact_check_run
from app.evidence.ledger import EvidenceLedger
from app.evidence.trace import TraceWriter


class DroppedEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: str
    reason: str


class NextStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["search", "extract", "finish"]
    query: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    why: str
    dropped: list[DroppedEvidence] = Field(default_factory=list)


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim: str
    topic: Literal["demand", "competition", "cost", "risk", "future", "other"]
    quote: str
    evidence_ids: list[str]
    localization: str | None = None


class Synthesis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str
    findings: list[Finding]


class Challenge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finding_id: str
    severity: Literal["low", "medium", "high"]
    reason: str
    adjustment: Literal["none", "penalty", "exclude"]


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid")

    challenges: list[Challenge]


def _budget_for(gateway, tavily) -> ResearchBudget:
    budget = getattr(gateway, "budget", None) or getattr(tavily, "budget", None)
    if budget is None:
        budget = ResearchBudget(6, 6, 14, 200_000)
    return budget


def _budget_left(budget: ResearchBudget) -> dict[str, int]:
    return {
        "searches": max(0, budget.max_searches - budget.searches_used),
        "extract_urls": max(0, budget.max_extract_urls - budget.extract_urls_used),
        "inference_calls": max(0, budget.max_inference_calls - budget.inference_calls_used),
        "tokens": budget.remaining_tokens(),
    }


def _call_usage(budget: ResearchBudget) -> tuple[int, int]:
    return budget.tokens_used, budget.inference_calls_used


def _redact(message: str) -> str:
    for key in ("NEBIUS_API_KEY", "TAVILY_API_KEY"):
        secret = os.environ.get(key)
        if secret:
            message = message.replace(secret, "[redacted]")
    return re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1[redacted]", message)[:1000]


async def run_live(
    run_dir: Path,
    *,
    run_id: str,
    question: str,
    area_name: str | None,
    role: str | None,
    gateway,
    tavily,
    checker: FactChecker,
) -> dict:
    """Run the bounded research loop and persist its ledger, trace and report."""
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    trace = TraceWriter(run_dir, run_id)
    ledger = EvidenceLedger(run_dir)
    budget = _budget_for(gateway, tavily)
    gathered: dict[str, WebEvidence] = {}
    dropped_ids: set[str] = set()
    searches_done: list[str] = []
    extracted_urls: set[str] = set()
    findings: list[dict] = []
    metrics: dict[str, dict] = {}
    status = "done"
    error = None

    def metric(role_name: str, model: str, *, calls=0, tokens_in=0, tokens_out=0, seconds=0.0):
        current = metrics.setdefault(
            role_name,
            {
                "model": model,
                "calls": 0,
                "tokens_in": 0,
                "tokens_out": 0,
                "seconds": 0.0,
            },
        )
        current["calls"] += calls
        current["tokens_in"] += tokens_in
        current["tokens_out"] += tokens_out
        current["seconds"] += seconds

    def model_name() -> str:
        config = getattr(gateway, "config", None)
        return getattr(config, "model", None) or os.environ.get(
            "NEBIUS_MODEL", "nvidia/nemotron-3-super-120b-a12b"
        )

    def model_event(agent: str, role_name: str, before, started):
        tokens_before, calls_before = before
        token_delta = max(0, budget.tokens_used - tokens_before)
        call_delta = max(1, budget.inference_calls_used - calls_before)
        seconds = max(0.0, time.monotonic() - started)
        model = model_name()
        trace.emit(
            "model_call",
            agent,
            f"{role_name} completed a structured model call.",
            model=model,
            tokens_in=token_delta,
            tokens_out=0,
            seconds=seconds,
            detail={"calls": call_delta},
        )
        metric(
            role_name,
            model,
            calls=call_delta,
            tokens_in=token_delta,
            seconds=seconds,
        )

    try:
        trace.emit(
            "plan",
            "planner",
            "The approved research question and area define the research scope.",
            detail={"summary": question, "area": area_name},
        )

        finished = False
        turn_limit_reached = True
        for _turn in range(8):
            compact_evidence = []
            for item in list(gathered.values())[-15:]:
                passage = (item.snippet or item.passage or "")[:500]
                compact_evidence.append(
                    item.__class__(
                        **{
                            **item.__dict__,
                            "passage": passage or None,
                            "snippet": passage or None,
                        }
                    )
                )
            before = _call_usage(budget)
            started = time.monotonic()
            try:
                decision = await gateway.decide(
                    context={
                        "question": question,
                        "area": area_name,
                        "role": role,
                        "searches_done": list(searches_done),
                        "budget_left": _budget_left(budget),
                    },
                    output_model=NextStep,
                    evidence=compact_evidence,
                )
            except BudgetExceededError as exc:
                trace.emit("budget_exhausted", "web_researcher", str(exc))
                turn_limit_reached = False
                break
            finally:
                model_event("web_researcher", "Web researcher", before, started)

            for dropped in decision.dropped:
                dropped_ids.add(dropped.evidence_id)
                trace.emit(
                    "finding_rejected",
                    "web_researcher",
                    dropped.reason,
                    detail={"summary": "Evidence dropped", "evidence_ids": [dropped.evidence_id]},
                )

            action = decision.action
            if action == "finish":
                trace.emit("status", "web_researcher", decision.why, detail={"summary": "finished"})
                finished = True
                turn_limit_reached = False
                break
            if action == "search":
                query = (decision.query or "").strip()
                if not query:
                    trace.emit(
                        "tool_error", "web_researcher", "The model supplied no search query."
                    )
                    continue
                if query.casefold() in {item.casefold() for item in searches_done}:
                    trace.emit(
                        "tool_error",
                        "web_researcher",
                        f"Duplicate search query was skipped: {query}",
                        detail={"summary": query},
                    )
                    continue
                if budget.searches_used >= budget.max_searches:
                    trace.emit(
                        "budget_exhausted",
                        "web_researcher",
                        "The search budget is exhausted; no additional search was started.",
                        detail={"summary": query},
                    )
                    turn_limit_reached = False
                    break
                started = time.monotonic()
                try:
                    page = await tavily.search(ResearchSearch(query=query, max_results=5))
                    searches_done.append(query)
                    new_ids = []
                    for item in page.evidence:
                        if item.extraction_status is ExtractionStatus.FAILED:
                            continue
                        ledger.add_web_evidence(item)
                        gathered.setdefault(item.evidence_id, item)
                        new_ids.append(item.evidence_id)
                    trace.emit(
                        "search",
                        "web_researcher",
                        decision.why,
                        detail={"summary": query, "evidence_ids": new_ids},
                        seconds=max(0.0, time.monotonic() - started),
                    )
                    metric(
                        "Web researcher",
                        "Tavily search + extract",
                        calls=1,
                        seconds=max(0.0, time.monotonic() - started),
                    )
                except BudgetExceededError as exc:
                    trace.emit("budget_exhausted", "web_researcher", str(exc))
                    turn_limit_reached = False
                    break
                except Exception as exc:
                    trace.emit("tool_error", "web_researcher", _redact(str(exc)))
                continue

            requested = []
            requested_urls = set()
            for evidence_id in decision.evidence_ids[:3]:
                item = gathered.get(evidence_id)
                if (
                    item is None
                    or evidence_id in dropped_ids
                    or item.original_url in extracted_urls
                    or item.original_url in requested_urls
                ):
                    continue
                requested.append(item)
                requested_urls.add(item.original_url)
            remaining = max(0, budget.max_extract_urls - budget.extract_urls_used)
            requested = requested[:remaining]
            if not requested:
                if remaining == 0:
                    trace.emit(
                        "budget_exhausted",
                        "web_researcher",
                        "The extraction URL budget is exhausted.",
                    )
                    turn_limit_reached = False
                    break
                else:
                    trace.emit(
                        "tool_error",
                        "web_researcher",
                        "The model selected no available evidence URLs to extract.",
                    )
                continue
            urls = tuple(item.original_url for item in requested)
            started = time.monotonic()
            try:
                page = await tavily.extract(ResearchExtract(urls=urls))
                extracted_urls.update(urls)
                new_ids = []
                for item in page.evidence:
                    if item.extraction_status is ExtractionStatus.FAILED:
                        continue
                    ledger.add_web_evidence(item)
                    gathered.setdefault(item.evidence_id, item)
                    new_ids.append(item.evidence_id)
                trace.emit(
                    "extract",
                    "web_researcher",
                    decision.why,
                    detail={"summary": ", ".join(urls), "evidence_ids": new_ids},
                    seconds=max(0.0, time.monotonic() - started),
                )
                metric(
                    "Web researcher",
                    "Tavily search + extract",
                    calls=1,
                    seconds=max(0.0, time.monotonic() - started),
                )
            except BudgetExceededError as exc:
                trace.emit("budget_exhausted", "web_researcher", str(exc))
                turn_limit_reached = False
                break
            except Exception as exc:
                trace.emit("tool_error", "web_researcher", _redact(str(exc)))

        if turn_limit_reached and not finished:
            trace.emit(
                "budget_exhausted",
                "web_researcher",
                "The iterative research loop reached its eight-turn limit.",
            )

        kept = [item for item in gathered.values() if item.evidence_id not in dropped_ids]
        synthesis_evidence = sorted(
            kept,
            key=lambda item: 0 if getattr(item.kind, "value", item.kind) == "extracted_page" else 1,
        )[:10]
        before = _call_usage(budget)
        started = time.monotonic()
        try:
            synthesis = await gateway.synthesize(
                context={
                    "question": question,
                    "area": area_name,
                    "role": role,
                    "instructions": (
                        "Return 4-8 findings. For each finding, 'quote' must be copied verbatim "
                        "(at most 300 characters) from the passage of the first evidence_id "
                        "it cites; do not paraphrase the quote. 'topic' is one of demand, "
                        "competition, cost, risk, future, other. Also return a two-sentence "
                        "'summary' of the answer."
                    ),
                },
                output_model=Synthesis,
                evidence=synthesis_evidence,
            )
        finally:
            model_event("writer", "Writer", before, started)

        by_id = {item.evidence_id: item for item in synthesis_evidence}
        for index, item in enumerate(synthesis.findings, 1):
            ids = [evidence_id for evidence_id in item.evidence_ids if evidence_id in by_id]
            if (
                not ids
                or len(item.quote) > 300
                or item.quote not in (by_id[ids[0]].passage or by_id[ids[0]].snippet or "")
            ):
                trace.emit(
                    "finding_rejected",
                    "web_researcher",
                    "The finding did not contain a verbatim quote from its cited evidence.",
                    detail={"summary": item.claim, "evidence_ids": ids},
                )
                continue
            finding = {
                "finding_id": f"f_{index}",
                "claim": item.claim,
                "quote": item.quote,
                "evidence_ids": ids,
                "claim_type": item.topic,
                "localization": item.localization,
                "agent": "web_researcher",
                "status": "accepted",
            }
            findings.append(finding)
            trace.emit(
                "finding_accepted",
                "web_researcher",
                "The claim is grounded in a verbatim passage from the cited source.",
                detail={
                    "summary": item.claim,
                    "finding_id": finding["finding_id"],
                    "evidence_ids": ids,
                },
            )

        findings_path = run_dir / "findings.jsonl"
        findings_path.write_text(
            "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in findings),
            encoding="utf-8",
        )

        before = _call_usage(budget)
        started = time.monotonic()
        try:
            review = await gateway.review(
                context={"question": question, "findings": findings},
                output_model=Review,
                evidence=synthesis_evidence,
            )
        finally:
            model_event("skeptic", "Skeptic", before, started)
        by_finding_id = {item["finding_id"]: item for item in findings}
        for challenge in review.challenges:
            finding = by_finding_id.get(challenge.finding_id)
            if finding is None:
                continue
            if challenge.adjustment == "exclude":
                finding["status"] = "rejected"
                finding["reject_reason"] = challenge.reason
                trace.emit(
                    "finding_rejected",
                    "skeptic",
                    challenge.reason,
                    detail={"summary": finding["claim"], "finding_id": finding["finding_id"]},
                )
            else:
                trace.emit(
                    "skeptic_risk",
                    "skeptic",
                    challenge.reason,
                    detail={
                        "summary": finding["claim"],
                        "finding_id": finding["finding_id"],
                        "severity": challenge.severity,
                        "adjustment": challenge.adjustment,
                    },
                )
        findings_path.write_text(
            "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in findings),
            encoding="utf-8",
        )

        accepted = [item for item in findings if item["status"] == "accepted"]
        report_lines = ["## Answer", synthesis.summary, "", "## Findings"]
        report_lines.extend(
            f"- {item['claim']} [{', '.join(item['evidence_ids'])}]" for item in accepted
        )
        (run_dir / "report.md").write_text("\n".join(report_lines) + "\n", encoding="utf-8")

        fact_check_started = time.monotonic()
        fact_report = await fact_check_run(run_dir, checker, trace=trace)
        fact_count = len(fact_report.evidence) + len(fact_report.findings)
        metric(
            "Fact-checker",
            "Tavily extract + fuzzy match ≥0.85",
            calls=fact_count,
            seconds=max(0.0, time.monotonic() - fact_check_started),
        )
        metrics_path = run_dir / "metrics.json"
        metrics_path.write_text(
            json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        trace.emit(
            "metrics",
            "orchestrator",
            "Per-role model and tool usage was recorded.",
            detail=metrics,
        )
        trace.emit("status", "orchestrator", "Research run completed.", detail={"summary": "done"})
    except Exception as exc:
        status = "failed"
        error = _redact(str(exc) or type(exc).__name__)
        trace.emit("error", "orchestrator", error, detail={"summary": "run failed"})
        trace.emit(
            "status",
            "orchestrator",
            "Research run failed.",
            detail={"summary": "error", "status": "failed"},
        )
        findings_path = run_dir / "findings.jsonl"
        if not findings_path.exists():
            findings_path.write_text("", encoding="utf-8")
        report_path = run_dir / "report.md"
        if not report_path.exists():
            report_path.write_text(
                "## Answer\nResearch run failed.\n\n## Findings\n", encoding="utf-8"
            )
        (run_dir / "metrics.json").write_text(
            json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    result = {"run_id": run_id, "status": status, "error": error}
    (run_dir / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return result
