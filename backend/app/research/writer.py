"""Report writer (§5.7): structured sentences with evidence ids, rendered to cited markdown."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, Field

from app.contracts.evidence import Finding
from app.evidence.citations import CITATION, strip_unknown_citations
from app.evidence.ledger import EvidenceLedger, entry_status
from app.research import prompts
from app.research.config import Emit, ResearchConfig
from app.research.events import Events
from app.research.models import chat_model, structured_call

_LOOSE_MARKER = re.compile(r"\s*\[(?:ev|ds):\s*[^\]]*\]")
_IDS = re.compile(r"\b(?:ev|ds)_[A-Za-z0-9]+\b")


class Sentence(BaseModel):
    text: str
    evidence_ids: list[str] = Field(default_factory=list)


class CellSection(BaseModel):
    cell_id: str
    name: str = ""
    reasons: list[Sentence] = Field(default_factory=list)
    risks: list[Sentence] = Field(default_factory=list)


class Report(BaseModel):
    summary: list[Sentence] = Field(default_factory=list)
    recommendation: list[Sentence] = Field(default_factory=list)
    cells: list[CellSection] = Field(default_factory=list)
    what_could_change: list[Sentence] = Field(default_factory=list)
    unknowns: list[Sentence] = Field(default_factory=list)


def top_items(result: dict, limit: int = 3) -> list[dict]:
    cells = result.get("cells")
    if isinstance(cells, dict):
        cells = cells.get("cells")
    items = cells if isinstance(cells, list) else result.get("hypotheses") or []
    items = [dict(c) for c in items if isinstance(c, dict)]
    for item in items:
        item.setdefault("id", item.get("cell_id"))
    wanted = result.get("top3") or result.get("top")
    if isinstance(wanted, list) and wanted:
        by_id = {str(c.get("id")): c for c in items}
        ranked = [by_id[str(w)] for w in wanted if str(w) in by_id]
        return ranked[:limit]
    return [c for c in items if not c.get("incomplete")][:limit]


def _cited_ids(value: Any) -> set[str]:
    return set(_IDS.findall(json.dumps(value, ensure_ascii=False, default=str)))


def _clean(text: str) -> str:
    text = _LOOSE_MARKER.sub("", text)
    text = CITATION.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def _sentence(sentence: Sentence, allowed: set[str], *, required: bool = True) -> str | None:
    ids = [e for e in dict.fromkeys(sentence.evidence_ids) if e in allowed]
    text = _clean(sentence.text)
    if not text or (required and not ids):
        return None
    marker = f" [{', '.join(ids)}]" if ids else ""
    if marker and text[-1] in ".!?":
        return f"{text[:-1]}{marker}{text[-1]}"
    return f"{text}{marker}"


def _score(item: dict) -> str:
    parts = []
    for key, label in (("score_present", "score"), ("score_future", "with future")):
        if isinstance(item.get(key), (int, float)):
            parts.append(f"{label} {item[key]:.2f}")
    if item.get("confidence"):
        parts.append(f"confidence {item['confidence']}")
    return f" — {', '.join(parts)}" if parts else ""


def render(report: Report, result: dict, allowed: set[str], title: str) -> str:
    lines = [f"# {title}", ""]
    summary = [s for s in (_sentence(x, allowed) for x in report.summary) if s]
    if summary:
        lines += [" ".join(summary), ""]
    recommendation = [s for s in (_sentence(x, allowed) for x in report.recommendation) if s]
    if recommendation:
        lines += ["## Recommendation", "", " ".join(recommendation), ""]
    items = {str(c.get("id")): c for c in top_items(result)}
    sections = {s.cell_id: s for s in report.cells}
    order = list(items) or [s.cell_id for s in report.cells]
    if order:
        lines += ["## Top locations", ""]
    for rank, cell_id in enumerate(order, 1):
        item = items.get(cell_id, {})
        section = sections.get(cell_id) or CellSection(cell_id=cell_id)
        name = item.get("name") or section.name or cell_id
        lines += [f"### {rank}. {name} ({cell_id}){_score(item)}", ""]
        reasons = [s for s in (_sentence(x, allowed) for x in section.reasons[:4]) if s]
        risks = [s for s in (_sentence(x, allowed) for x in section.risks) if s]
        if reasons:
            lines += ["**Why**", "", *(f"- {r}" for r in reasons), ""]
        if risks:
            lines += ["**Risks**", "", *(f"- {r}" for r in risks), ""]
    changes = [
        s for s in (_sentence(x, allowed, required=False) for x in report.what_could_change) if s
    ]
    if changes:
        lines += ["## What could change the recommendation", "", *(f"- {c}" for c in changes), ""]
    unknowns = [s for s in (_sentence(x, allowed, required=False) for x in report.unknowns) if s]
    if unknowns:
        lines += ["## Unknowns", "", *(f"- {u}" for u in unknowns), ""]
    return "\n".join(lines).rstrip() + "\n"


def _risk_sentences(item: dict) -> list[Sentence]:
    out = []
    for risk in item.get("risks") or []:
        if isinstance(risk, dict) and risk.get("text"):
            ids = risk.get("evidence_ids") or [risk.get("evidence_id")]
            severity = risk.get("severity")
            text = f"{severity.capitalize()} risk: {risk['text']}" if severity else risk["text"]
            out.append(Sentence(text=text, evidence_ids=[i for i in ids if i]))
    return out


def _unknown_sentences(items: Sequence[dict]) -> list[Sentence]:
    out = []
    for item in items:
        for unknown in item.get("unknowns") or []:
            if isinstance(unknown, str):
                out.append(Sentence(text=f"{item.get('name') or item.get('id')}: {unknown}"))
            elif isinstance(unknown, dict) and unknown.get("text"):
                check = unknown.get("suggested_validation")
                text = unknown["text"] + (f" (validate: {check})" if check else "")
                ids = unknown.get("evidence_ids") or [unknown.get("evidence_id")]
                out.append(Sentence(text=text, evidence_ids=[i for i in ids if i]))
    return out


def deterministic_report(result: dict, findings: Sequence[Finding]) -> Report:
    """Template report from findings and result; used offline and when the writer call fails."""
    live = [f for f in findings if f.status != "rejected" and f.verification != "unsupported"]
    items = top_items(result)
    sections = []
    for item in items:
        cell_id = str(item.get("id"))
        own = [f for f in live if f.localization == cell_id]
        reasons = [
            Sentence(text=f.claim, evidence_ids=f.evidence_ids)
            for f in own
            if f.claim_type != "risk"
        ]
        risks = _risk_sentences(item) or [
            Sentence(text=f"Risk: {f.claim}", evidence_ids=f.evidence_ids)
            for f in own
            if f.claim_type == "risk"
        ]
        name = str(item.get("name") or "")
        sections.append(CellSection(cell_id=cell_id, name=name, reasons=reasons[:4], risks=risks))
    district = [f for f in live if f.localization in {None, "area"}]
    summary = [Sentence(text=f.claim, evidence_ids=f.evidence_ids) for f in district[:4]]
    if not summary:
        summary = [s.reasons[0] for s in sections if s.reasons][:3]
    return Report(summary=summary, cells=sections, unknowns=_unknown_sentences(items))


def _writer_input(question: str, result: dict, findings: Sequence[Finding]) -> str:
    cells = [
        {
            k: item.get(k)
            for k in (
                "id",
                "name",
                "parent_name",
                "score_present",
                "score_future",
                "confidence",
                "confidence_reasons",
                "modifiers",
                "risks",
                "unknowns",
                "demoted_by",
            )
            if item.get(k) not in (None, [], {})
        }
        for item in top_items(result)
    ]
    facts = [
        {
            "evidence_ids": f.evidence_ids,
            "claim": f.claim,
            "quote": f.quote,
            "claim_type": f.claim_type,
            "localization": f.localization,
        }
        for f in findings
        if f.status != "rejected" and f.verification != "unsupported"
    ]
    return json.dumps(
        {"question": question, "top_cells": cells, "findings": facts},
        ensure_ascii=False,
        indent=1,
        default=str,
    )


async def write_report(
    question: str,
    result: dict,
    findings: list[Finding],
    ledger: EvidenceLedger,
    emit: Emit,
    cfg: ResearchConfig,
    *,
    title: str = "Research report",
) -> str:
    events = Events(emit, "writer", cfg.writer_model)
    live = [f for f in findings if f.status != "rejected" and f.verification != "unsupported"]
    wanted = {e for f in live for e in f.evidence_ids} | _cited_ids(top_items(result))
    entries = [
        e
        for e in ledger.entries()
        if e["evidence_id"] in wanted and entry_status(e) != "unsupported"
    ]
    allowed = {e["evidence_id"] for e in entries}
    report = None
    if not cfg.fake:
        model = chat_model(cfg.writer_model, cfg)
        user = _writer_input(question, result, live)
        report = await structured_call(
            model, cfg.writer_model, prompts.WRITER, user, Report, events, "write report"
        )
    deterministic = report is None
    if report is None:
        report = deterministic_report(result, live)
    stripped = strip_unknown_citations(render(report, result, allowed, title), entries)
    await events(
        "write",
        why="report rendered with ledger citations",
        detail={
            "citations": len(stripped.kept),
            "stripped": list(stripped.stripped),
            "deterministic": deterministic,
        },
    )
    return stripped.text
