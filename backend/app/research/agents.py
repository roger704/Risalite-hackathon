"""Wave 1/2, T1 and skeptic researchers (deepagents + Nebius + Tavily)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.contracts.evidence import Finding
from app.domain.research import ResearchBudget, ResearchConnector
from app.evidence.ledger import EvidenceLedger
from app.research import fake, prompts
from app.research.config import Emit, ResearchConfig
from app.research.events import Events
from app.research.locality import address_street, address_terms, cell_terms
from app.research.models import chat_model, structured_call
from app.research.runner import run_agent
from app.research.toolkit import Role, Toolkit

SEVERITIES = ("low", "medium", "high")
DEFAULT_SEVERITY = "medium"


@asynccontextmanager
async def research_connector(cfg: ResearchConfig) -> AsyncIterator[ResearchConnector]:
    if cfg.connector is not None:
        yield cfg.connector
        return
    from app.connectors.tavily import TavilyClient, TavilyConfig

    budget = ResearchBudget(
        max_searches=cfg.max_searches,
        max_extract_urls=cfg.max_extracts,
        max_inference_calls=0,
        max_tokens=0,
    )
    async with TavilyClient(TavilyConfig.from_env(max_results=cfg.max_results), budget) as client:
        yield client


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=1, default=str)


async def _research(
    *,
    cfg: ResearchConfig,
    ledger: EvidenceLedger,
    events: Events,
    model: str,
    role: Role,
    system: str,
    task: str,
    localizations: Sequence[str],
    default_localization: str,
    place_terms: Mapping[str, Sequence[str]] | None = None,
    claim_types: Sequence[str] | None = None,
    finding_extra: dict[str, Any] | None = None,
) -> list[Finding]:
    llm = chat_model(model, cfg)
    async with research_connector(cfg) as connector:
        toolkit = Toolkit(
            ledger=ledger,
            events=events,
            cfg=cfg,
            connector=connector,
            role=role,
            localizations=localizations,
            default_localization=default_localization,
            place_terms=place_terms,
            claim_types=claim_types,
            finding_extra=finding_extra,
        )
        system_prompt = f"{system}\n\n{prompts.rules(cfg.max_searches, cfg.max_extracts)}"
        return await run_agent(
            toolkit=toolkit, model=llm, model_label=model, system_prompt=system_prompt, task=task
        )


def _cell_brief(cell: dict) -> dict:
    brief = {k: cell.get(k) for k in ("id", "name", "parent_name") if cell.get(k) is not None}
    if cell.get("streets"):
        brief["streets"] = list(cell["streets"][:15])
    return brief


async def research_area(
    question: str,
    area: dict,
    cells: list[dict],
    ledger: EvidenceLedger,
    emit: Emit,
    cfg: ResearchConfig,
    *,
    subquestions: Sequence[str] = (),
    agent: str = "researcher:wave1:1",
    subquestion_id: str | None = None,
) -> list[Finding]:
    events = Events(emit, agent, cfg.model)
    if cfg.fake:
        return _tag(await fake.area(events, ledger, area, cells), subquestion_id)
    focus = "\n".join(f"- {q}" for q in subquestions) or "- planning, future demand, market"
    task = (
        f"Question: {question}\n\nDistrict: {_dump(area)}\n\nFocus:\n{focus}\n\n"
        f"Cells (Planungsräume) for localization:\n{_dump([_cell_brief(c) for c in cells])}"
    )
    extra = {"subquestion_id": subquestion_id} if subquestion_id else None
    return await _research(
        cfg=cfg,
        ledger=ledger,
        events=events,
        model=cfg.model,
        role="future",
        system=prompts.AREA,
        task=task,
        localizations=[*(str(c["id"]) for c in cells if c.get("id")), "area"],
        default_localization="area",
        place_terms={
            str(c["id"]): cell_terms(c.get("name"), c.get("streets") or [])
            for c in cells
            if c.get("id")
        },
        finding_extra=extra,
    )


async def research_cell(
    question: str,
    cell: dict,
    ledger: EvidenceLedger,
    emit: Emit,
    cfg: ResearchConfig,
    *,
    context: Sequence[Finding | dict] = (),
    agent: str | None = None,
) -> list[Finding]:
    cell_id = str(cell.get("id") or "cell")
    events = Events(emit, agent or f"researcher:wave2:{cell_id}", cfg.model)
    if cfg.fake:
        return await fake.cell(events, ledger, cell)
    known = [_claim_brief(f) for f in context]
    task = (
        f"Question: {question}\n\nCell: {_dump(_cell_brief(cell))}\n\n"
        f"Data profile (raw layer values): {_dump(cell.get('raw_values') or {})}\n\n"
        f"Wave-1 findings already localized here (do not repeat): {_dump(known)}\n\n"
        "Why does this cell score well on data, and is there a reason the business would not "
        "work here?"
    )
    return await _research(
        cfg=cfg,
        ledger=ledger,
        events=events,
        model=cfg.model,
        role="future",
        system=prompts.CELL,
        task=task,
        localizations=[cell_id, "area"],
        default_localization=cell_id,
        place_terms={cell_id: cell_terms(cell.get("name"), cell.get("streets") or [])},
    )


async def research_hypothesis(
    question: str,
    space: dict,
    hypothesis: dict,
    ledger: EvidenceLedger,
    emit: Emit,
    cfg: ResearchConfig,
    *,
    agent: str | None = None,
) -> list[Finding]:
    hid = str(hypothesis.get("id") or "h")
    cell_id = space.get("cell_id") or space.get("plr_id")
    events = Events(emit, agent or f"researcher:t1:{hid}", cfg.model)
    localizations = ["address", "area", *([str(cell_id)] if cell_id else [])]
    default = str(cell_id) if cell_id else "address"
    if cfg.fake:
        return await fake.hypothesis(events, ledger, space, hypothesis, default)
    task = (
        f"Question: {question}\n\nSpace: {_dump(space)}\n\nHypothesis: {_dump(hypothesis)}\n\n"
        "Find rent, space and operator-fit evidence for this business at this address."
    )
    return await _research(
        cfg=cfg,
        ledger=ledger,
        events=events,
        model=cfg.model,
        role="t1",
        system=prompts.HYPOTHESIS,
        task=task,
        localizations=localizations,
        default_localization=default,
        place_terms={
            **({"address": address_terms(space.get("address"))}),
            **(
                {
                    str(cell_id): cell_terms(
                        space.get("cell_name"),
                        [
                            *(space.get("streets") or []),
                            *([street] if (street := address_street(space.get("address"))) else []),
                        ],
                    )
                }
                if cell_id
                else {}
            ),
        },
        finding_extra={"hypothesis_id": hid},
    )


def _claim_brief(finding: Finding | dict) -> dict:
    data = finding.model_dump() if isinstance(finding, Finding) else dict(finding)
    return {k: data.get(k) for k in ("finding_id", "claim", "quote", "evidence_ids", "claim_type")}


def _top_claims(findings: Sequence[Finding], limit: int = 3) -> list[Finding]:
    live = [f for f in findings if f.status != "rejected" and f.claim_type != "risk"]
    order = {"verified": 0, "partial": 1, None: 2}
    return sorted(live, key=lambda f: order.get(f.verification, 3))[:limit]


class RiskReview(BaseModel):
    finding_id: str
    severity: Literal["low", "medium", "high"]
    rationale: str = ""


class RiskReviews(BaseModel):
    reviews: list[RiskReview] = Field(default_factory=list)


async def review_risks(
    question: str,
    cell: dict,
    risks: Sequence[Finding],
    ledger: EvidenceLedger,
    events: Events,
    cfg: ResearchConfig,
) -> dict[str, RiskReview]:
    if not risks:
        return {}
    if cfg.fake:
        return {
            f.finding_id: RiskReview(finding_id=f.finding_id, severity="medium", rationale="fake")
            for f in risks
        }
    items = [
        {
            "finding_id": f.finding_id,
            "claim": f.claim,
            "quote": f.quote,
            "sources": [(ledger.get(e) or {}).get("title") or e for e in f.evidence_ids],
        }
        for f in risks
    ]
    user = f"Question: {question}\n\nCell: {_dump(_cell_brief(cell))}\n\nRisks: {_dump(items)}"
    model = chat_model(cfg.review_model, cfg)
    result = await structured_call(
        model, cfg.review_model, prompts.REVIEW, user, RiskReviews, events, "severity review"
    )
    known = {f.finding_id for f in risks}
    reviews = {r.finding_id: r for r in (result.reviews if result else []) if r.finding_id in known}
    await events(
        "review",
        why="severity review of skeptic risks",
        detail={
            "reviews": [r.model_dump() for r in reviews.values()],
            "defaulted": sorted(known - set(reviews)),
        },
    )
    return reviews


async def skeptic(
    question: str,
    cell: dict,
    findings: list[Finding],
    ledger: EvidenceLedger,
    emit: Emit,
    cfg: ResearchConfig,
    *,
    agent: str | None = None,
    place_terms: Mapping[str, Sequence[str]] | None = None,
) -> list[dict]:
    cell_id = str(cell.get("id") or "cell")
    events = Events(emit, agent or f"skeptic:{cell_id}", cfg.skeptic_model)
    if cfg.fake:
        recorded = await fake.skeptic(events, ledger, cell)
    else:
        top = [_claim_brief(f) for f in _top_claims(findings)]
        task = (
            f"Question: {question}\n\nCell: {_dump(_cell_brief(cell))}\n\n"
            f"Data profile: {_dump(cell.get('raw_values') or {})}\n\n"
            f"Top claims to challenge: {_dump(top)}\n\nSearch for counter-evidence."
        )
        recorded = await _research(
            cfg=cfg,
            ledger=ledger,
            events=events,
            model=cfg.skeptic_model,
            role="risk",
            system=prompts.SKEPTIC,
            task=task,
            localizations=[cell_id, "area"],
            default_localization=cell_id,
            place_terms=(
                place_terms
                if place_terms is not None
                else {cell_id: cell_terms(cell.get("name"), cell.get("streets") or [])}
            ),
            claim_types=["risk"],
        )
    risks = [f for f in recorded if f.status == "accepted"]
    reviews = await review_risks(question, cell, risks, ledger, events, cfg)
    out = []
    for finding in risks:
        review = reviews.get(finding.finding_id)
        severity = review.severity if review else DEFAULT_SEVERITY
        risk = {
            "severity": severity,
            "text": finding.claim,
            "quote": finding.quote,
            "evidence_id": finding.evidence_ids[0],
            "evidence_ids": list(finding.evidence_ids),
            "finding_id": finding.finding_id,
            "rationale": review.rationale if review else "severity review unavailable",
            "localization": finding.localization,
            "agent": finding.agent,
            "finding": finding.model_dump(),
        }
        await events(
            "skeptic_risk",
            why=finding.claim,
            detail={k: risk[k] for k in ("finding_id", "severity", "evidence_ids", "text")},
        )
        out.append(risk)
    return out


def _tag(findings: list[Finding], subquestion_id: str | None) -> list[Finding]:
    if subquestion_id:
        for finding in findings:
            setattr(finding, "subquestion_id", subquestion_id)
    return findings
