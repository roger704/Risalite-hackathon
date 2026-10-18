"""Research agents (waves, skeptic, writer) on deepagents + Nebius + Tavily.

Every function returns what was recorded, also when a budget, the recursion limit or the
timeout stops the agent. Returned findings carry ``status`` "accepted" or "rejected".
"""

from __future__ import annotations

from app.contracts.evidence import Finding
from app.evidence.ledger import EvidenceLedger
from app.research import agents, writer
from app.research.config import Emit, ResearchConfig

__all__ = [
    "Emit",
    "ResearchConfig",
    "research_area",
    "research_cell",
    "research_hypothesis",
    "skeptic",
    "write_report",
]


async def research_area(question: str, area: dict, cells: list[dict], ledger: EvidenceLedger, emit: Emit, cfg: ResearchConfig, **options) -> list[Finding]:
    """Wave 1. area={"area_id","name"}; cells=[{"id","name","parent_name"}]. Finding.localization = cell id or "area".

    options: subquestions (list[str]), agent (e.g. "researcher:wave1:2"), subquestion_id.
    """
    return await agents.research_area(question, area, cells, ledger, emit, cfg, **options)


async def research_cell(question: str, cell: dict, ledger: EvidenceLedger, emit: Emit, cfg: ResearchConfig, **options) -> list[Finding]:
    """Wave 2, one top-3 cell. cell={"id","name","parent_name","raw_values"}.

    options: context (wave-1 findings for this cell), agent.
    """
    return await agents.research_cell(question, cell, ledger, emit, cfg, **options)


async def research_hypothesis(question: str, space: dict, hypothesis: dict, ledger: EvidenceLedger, emit: Emit, cfg: ResearchConfig, **options) -> list[Finding]:
    """T1: one catalog hypothesis at an address (rent, operator/space fit evidence).

    options: agent.
    """
    return await agents.research_hypothesis(question, space, hypothesis, ledger, emit, cfg, **options)


async def skeptic(question: str, cell: dict, findings: list[Finding], ledger: EvidenceLedger, emit: Emit, cfg: ResearchConfig, **options) -> list[dict]:
    """Counter-evidence; returns risks {"severity","text","quote","evidence_id","finding_id"}.

    Extra keys per risk: evidence_ids, rationale, localization, agent, finding (the risk
    Finding as a dict, claim_type "risk", for findings.jsonl / fact-check). options: agent.
    """
    return await agents.skeptic(question, cell, findings, ledger, emit, cfg, **options)


async def write_report(question: str, result: dict, findings: list[Finding], ledger: EvidenceLedger, emit: Emit, cfg: ResearchConfig, **options) -> str:
    """Markdown; cites only ledger ids as [ev_…]/[ds_…]; unknown citations stripped.

    options: title.
    """
    return await writer.write_report(question, result, findings, ledger, emit, cfg, **options)
