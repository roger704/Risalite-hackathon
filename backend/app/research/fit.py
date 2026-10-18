"""T1 fit derivation: verified findings per hypothesis -> Cost / Operator fit / Space fit.

One small structured LLM call per run through the research model (``cfg.model``); fake mode
derives the same shape deterministically from finding dimensions and stances. Only evidence ids
of the verified findings handed in survive, so a fit score can never cite unchecked sources.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.contracts.evidence import Finding
from app.research.config import Emit, ResearchConfig
from app.research.events import Events
from app.research.models import chat_model, structured_call

DIMENSIONS = {"cost": "Cost", "operator_fit": "Operator fit", "space_fit": "Space fit"}
STANCE_SCORE = {"for": 7.0, "neutral": 5.0, "against": 3.0}

SYSTEM = """You rate how well a business idea fits one Berlin address for Risalite.
For each hypothesis and each dimension (Cost, Operator fit, Space fit) give a score from 0 to 10
(10 = best fit; for Cost 10 = cheap/affordable), one sentence why, and the evidence_ids of the
findings that support the score. Use only the findings given; cite only their evidence_ids.
Omit a dimension when no finding speaks to it."""


class FitItem(BaseModel):
    hypothesis_id: str
    dimension: Literal["Cost", "Operator fit", "Space fit"]
    score: float = Field(ge=0, le=10)
    why: str
    evidence_ids: list[str] = Field(default_factory=list)


class FitAssessment(BaseModel):
    items: list[FitItem] = Field(default_factory=list)


def _as_dict(finding: Finding | dict) -> dict[str, Any]:
    return finding.model_dump() if isinstance(finding, Finding) else dict(finding)


def _fake(hypotheses: Sequence[dict], findings: list[dict[str, Any]]) -> FitAssessment:
    items: list[FitItem] = []
    for hypothesis in hypotheses:
        hid = str(hypothesis["id"])
        for key, name in DIMENSIONS.items():
            rows = [
                f
                for f in findings
                if f.get("hypothesis_id") == hid and f.get("dimension") == key
            ]
            if not rows:
                continue
            scores = [STANCE_SCORE.get(str(f.get("stance") or "neutral"), 5.0) for f in rows]
            items.append(
                FitItem(
                    hypothesis_id=hid,
                    dimension=name,
                    score=round(sum(scores) / len(scores), 1),
                    why="; ".join(str(f.get("claim") or "") for f in rows)[:300],
                    evidence_ids=[e for f in rows for e in f.get("evidence_ids") or []],
                )
            )
    return FitAssessment(items=items)


async def derive_fit(
    question: str,
    prepared: dict,
    findings: Sequence[Finding | dict],
    emit: Emit,
    cfg: ResearchConfig,
    *,
    agent: str = "fit",
) -> dict[str, dict[str, dict[str, Any]]]:
    """``{hypothesis_id: {"Cost"|"Operator fit"|"Space fit": {score, why, evidence_ids}}}``."""
    hypotheses = list(prepared.get("hypotheses") or [])
    rows = [_as_dict(f) for f in findings]
    events = Events(emit, agent, cfg.model)
    if getattr(cfg, "fake", False):
        result: FitAssessment | None = _fake(hypotheses, rows)
    else:
        brief = {
            "space": prepared.get("space") or {},
            "operator": prepared.get("operator") or {},
            "hypotheses": [{"id": h["id"], "name": h.get("name")} for h in hypotheses],
            "findings": [
                {
                    k: f.get(k)
                    for k in ("hypothesis_id", "dimension", "stance", "claim", "quote", "evidence_ids")
                }
                for f in rows
            ],
        }
        user = f"Question: {question}\n\n{json.dumps(brief, ensure_ascii=False, default=str)}"
        result = await structured_call(
            chat_model(cfg.model, cfg), cfg.model, SYSTEM, user, FitAssessment, events, "fit"
        )
    allowed: dict[str, set[str]] = {}
    for f in rows:
        allowed.setdefault(str(f.get("hypothesis_id")), set()).update(f.get("evidence_ids") or [])
    known = {str(h["id"]) for h in hypotheses}
    fit: dict[str, dict[str, dict[str, Any]]] = {}
    for item in result.items if result else []:
        ids = [e for e in dict.fromkeys(item.evidence_ids) if e in allowed.get(item.hypothesis_id, ())]
        if item.hypothesis_id not in known or not ids:
            continue
        fit.setdefault(item.hypothesis_id, {})[item.dimension] = {
            "score": item.score,
            "why": item.why,
            "evidence_ids": ids,
        }
    await events(
        "score",
        why="fit dimensions from verified findings",
        detail={"fit": {h: sorted(d) for h, d in fit.items()}},
    )
    return fit
