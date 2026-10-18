"""Research agents (waves, skeptic, writer) on deepagents + Nebius + Tavily. Interface stub."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field

from app.contracts.evidence import Finding
from app.evidence.ledger import EvidenceLedger

Emit = Callable[[str, dict], None]  # (event type from contracts.events, payload); orchestrator adds seq/ts/run_id


@dataclass
class ResearchConfig:
    model: str = field(default_factory=lambda: os.environ.get("RISALITE_RESEARCH_MODEL", "nvidia/nemotron-3-super-120b-a12b"))
    skeptic_model: str = field(default_factory=lambda: os.environ.get("RISALITE_SKEPTIC_MODEL", "deepseek-ai/DeepSeek-V4.1-Flash"))
    writer_model: str = field(default_factory=lambda: os.environ.get("RISALITE_WRITER_MODEL", "nvidia/nemotron-3-super-120b-a12b"))
    max_searches: int = 8
    max_extracts: int = 6
    recursion_limit: int = 80
    timeout_s: float = 300.0
    fake: bool = field(default_factory=lambda: os.environ.get("RISALITE_RESEARCH_FAKE") == "1")


async def research_area(question: str, area: dict, cells: list[dict], ledger: EvidenceLedger, emit: Emit, cfg: ResearchConfig) -> list[Finding]:
    """Wave 1. area={"area_id","name"}; cells=[{"id","name","parent_name"}]. Finding.localization = cell id or "area"."""
    raise NotImplementedError


async def research_cell(question: str, cell: dict, ledger: EvidenceLedger, emit: Emit, cfg: ResearchConfig) -> list[Finding]:
    """Wave 2, one top-3 cell. cell={"id","name","parent_name","raw_values"}."""
    raise NotImplementedError


async def research_hypothesis(question: str, space: dict, hypothesis: dict, ledger: EvidenceLedger, emit: Emit, cfg: ResearchConfig) -> list[Finding]:
    """T1: one catalog hypothesis at an address (rent, operator/space fit evidence)."""
    raise NotImplementedError


async def skeptic(question: str, cell: dict, findings: list[Finding], ledger: EvidenceLedger, emit: Emit, cfg: ResearchConfig) -> list[dict]:
    """Counter-evidence; returns risks {"severity","text","quote","evidence_id","finding_id"}."""
    raise NotImplementedError


async def write_report(question: str, result: dict, findings: list[Finding], ledger: EvidenceLedger, emit: Emit, cfg: ResearchConfig) -> str:
    """Markdown; cites only ledger ids as [ev:<id>]; unknown citations stripped."""
    raise NotImplementedError
