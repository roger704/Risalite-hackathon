"""Post-approval run stages (Deep_Research_Spec §4–§5): data → score → waves → fact-check →
merge → rescore → skeptic → fact-check risks → rank → writer → files."""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.contracts.evidence import EvidenceEntry, Finding
from app.evidence.citations import strip_unknown_citations
from app.evidence.ledger import EvidenceLedger

from .events import EventLog, metrics
from .planning import FUTURE_UNITS

FUTURE_CLAIMS = {"future", "future_project"}
UNITS = {"dwellings", "kita_places", "children_0_6"}


class RunFailure(Exception):
    """Expected, user-facing failure (reported in the ``error`` event)."""


@dataclass
class Deps:
    scoring: Any
    research: Any
    port: Any
    checker_factory: Callable[[], Any]
    config_factory: Callable[[], Any]
    t1: Any = None


@dataclass
class Context:
    run: dict[str, Any]
    run_dir: Path
    log: EventLog
    ledger: EvidenceLedger
    deps: Deps
    findings: list[dict[str, Any]] = field(default_factory=list)

    @property
    def question(self) -> str:
        plan = self.run.get("plan") or {}
        approved = set(self.run.get("approval", {}).get("subquestions") or [s["id"] for s in plan.get("sub_questions", [])])
        subs = [s["text"] for s in plan.get("sub_questions", []) if s["id"] in approved]
        return self.run["question"] + ("\n\nApproved sub-questions:\n" + "\n".join(f"- {s}" for s in subs) if subs else "")


async def _maybe(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def _guard(ctx: Context, agent: str, call: Awaitable[Any], default: Any) -> Any:
    try:
        return await call
    except Exception as exc:
        ctx.log.emit("tool_error", agent, {"error": f"{type(exc).__name__}: {exc}"}, why="Agent failed; its recorded findings are kept")
        return default


def _finding_dict(finding: Any) -> dict[str, Any]:
    data = finding.model_dump(by_alias=True) if hasattr(finding, "model_dump") else dict(finding)
    return Finding.model_validate(data).model_dump(exclude_none=False)


async def fact_check(ctx: Context, items: list[dict[str, Any]], checker: Any) -> list[dict[str, Any]]:
    """Accepted set: verified, and partial restricted to matching ids (§5.4); unsupported dropped."""
    by_id = {e["evidence_id"]: e for e in ctx.ledger.entries()}
    accepted = []
    for item in items:
        result = await checker.check_finding(item, by_id)
        item["cited_evidence_ids"] = list(item.get("evidence_ids") or [])
        item["verification"] = result.status
        item["best_score"] = result.best_score
        item["fact_check_reason"] = result.reason
        ctx.log.emit(
            "fact_check",
            "factcheck",
            {"finding_id": item["finding_id"], "status": result.status, "best_score": result.best_score},
            why=result.reason[:300],
        )
        if result.status == "unsupported":
            item["status"] = "rejected"
            item["reject_reason"] = result.reason
            ctx.log.emit("finding_rejected", "factcheck", {"finding_id": item["finding_id"], "claim": item.get("claim")}, why="unsupported on re-fetch")
            continue
        item["evidence_ids"] = list(result.matched_ids) or item["cited_evidence_ids"]
        accepted.append(item)
    return accepted


def _cell_of(localization: str | None, cell_ids: set[str]) -> str | None:
    if not localization:
        return None
    value = localization.removeprefix("plr:")
    return value if value in cell_ids else None


def future_modifiers(ctx: Context, accepted: list[dict[str, Any]], cell_ids: set[str]) -> list[Any]:
    FutureModifier = ctx.deps.scoring.FutureModifier
    default_unit = FUTURE_UNITS.get(ctx.run["shape"], "dwellings")
    modifiers = []
    for item in accepted:
        cell = _cell_of(item.get("localization"), cell_ids)
        if item.get("claim_type") not in FUTURE_CLAIMS or cell is None or not item["evidence_ids"]:
            continue
        unit = item.get("unit") if item.get("unit") in UNITS else default_unit
        try:
            amount = float(item.get("amount") or 0.0)
        except (TypeError, ValueError):
            amount = 0.0
        modifiers.append(
            FutureModifier(
                cell_id=cell,
                kind=str(item.get("kind") or "future_project"),
                unit=unit,
                amount=amount,
                evidence_id=item["evidence_ids"][0],
                label=item["claim"],
                quote=item["quote"],
            )
        )
    return modifiers


def _template(ctx: Context) -> Any:
    template = ctx.deps.scoring.load_template(ctx.run["plan"]["template_id"])
    approved = {layer["id"]: layer for layer in ctx.run.get("approval", {}).get("layers") or []}
    layers = tuple(
        dataclasses.replace(
            layer, weight=float(approved[layer.id]["weight"]) if approved[layer.id]["enabled"] else 0.0
        )
        if layer.id in approved
        else layer
        for layer in template.layers
    )
    return dataclasses.replace(template, layers=layers)


def _cells_payload(cells: list[Any]) -> list[dict[str, Any]]:
    return [{"id": c.cell_id, "name": c.name, "parent_name": c.parent_name} for c in cells]


def _cell_payload(cell: Any) -> dict[str, Any]:
    return {"id": cell.cell_id, "name": cell.name, "parent_name": cell.parent_name, "raw_values": dict(cell.raw_values)}


async def run_area(ctx: Context) -> dict[str, Any]:
    scoring, research, port = ctx.deps.scoring, ctx.deps.research, ctx.deps.port
    cfg = ctx.deps.config_factory()
    approval = ctx.run.get("approval", {})
    area_id = ctx.run["plan"]["area"]["area_id"]
    template = _template(ctx)

    area = port.resolve(area_id)
    inputs = await asyncio.to_thread(port.cells, area_id, template, ledger=ctx.ledger)
    if not inputs:
        raise RunFailure(f"No PLR cells with data for {area['name']} ({area_id})")
    for layer in template.layers:
        present = sum(1 for c in inputs if c.raw.get(layer.id) is not None)
        ctx.log.emit("layer", "data", {"layer_id": layer.id, "cells": len(inputs), "present": present, "weight": layer.weight}, why="Layer values per PLR from pinned snapshots")
    reference = await asyncio.to_thread(port.berlin_reference, template) or None
    reasons = port.confidence_reasons(area_id)
    v0 = scoring.score_area(inputs, template, berlin_reference=reference, extra_confidence_reasons=reasons)
    ranked = scoring.rank(v0.cells, "present")
    top3 = [c for c in ranked if not c.incomplete][:3]
    ctx.log.emit("score", "scorer", {"version": "v0", "top3": [c.cell_id for c in top3], "excluded": v0.excluded}, why="Top-3 by score_present drive wave 2")

    by_id = {c.cell_id: c for c in inputs}
    area_payload = {"area_id": area_id, "name": area["name"]}
    calls = [
        _guard(ctx, "researcher:wave1", research.research_area(ctx.question, area_payload, _cells_payload(inputs), ctx.ledger, ctx.log.emitter("researcher:wave1"), cfg), [])
    ]
    if approval.get("wave2", True) is not False:
        calls += [
            _guard(ctx, f"researcher:wave2:{c.cell_id}", research.research_cell(ctx.question, _cell_payload(c), ctx.ledger, ctx.log.emitter(f"researcher:wave2:{c.cell_id}"), cfg), [])
            for c in top3
        ]
    waves = await asyncio.gather(*calls)
    ctx.findings = [_finding_dict(f) for wave in waves for f in wave]
    ctx.log.emit("status", "orchestrator", {"status": "reviewing", "findings": len(ctx.findings)})

    checker = ctx.deps.checker_factory()
    try:
        accepted = await fact_check(ctx, [f for f in ctx.findings if f.get("status") != "rejected"], checker)
        modifiers = future_modifiers(ctx, accepted, set(by_id))
        ctx.log.emit("merge", "orchestrator", {"accepted": len(accepted), "modifiers": [m.cell_id for m in modifiers]}, why="Only verified findings reach modifiers")
        v1 = scoring.score_area(inputs, template, berlin_reference=reference, modifiers=modifiers, extra_confidence_reasons=reasons)
        ranked = scoring.rank(v1.cells, "present")
        top3 = [c for c in ranked if not c.incomplete][:3]
        ctx.log.emit("rescore", "scorer", {"version": "v1", "top3": [c.cell_id for c in top3]}, why="Future modifiers merged")

        skeptics = await asyncio.gather(
            *[
                _guard(ctx, f"skeptic:{c.cell_id}", research.skeptic(ctx.question, _cell_payload(c), [Finding.model_validate(f) for f in accepted if _cell_of(f.get("localization"), {c.cell_id})], ctx.ledger, ctx.log.emitter(f"skeptic:{c.cell_id}"), cfg), [])
                for c in top3
            ]
        )
        risk_items = []
        for cell, risks in zip(top3, skeptics):
            for n, risk in enumerate(risks):
                risk_items.append(
                    {
                        "finding_id": risk.get("finding_id") or f"r_{cell.cell_id}_{n}",
                        "claim": risk.get("text", ""),
                        "quote": risk.get("quote") or "",
                        "evidence_ids": [risk["evidence_id"]] if risk.get("evidence_id") else [],
                        "claim_type": "risk",
                        "localization": f"plr:{cell.cell_id}",
                        "agent": f"skeptic:{cell.cell_id}",
                        "status": "accepted",
                        "severity": risk.get("severity", "low"),
                        "_risk": risk,
                    }
                )
        known = {f["finding_id"] for f in ctx.findings}
        verified_risks = await fact_check(ctx, risk_items, checker)
        for item in risk_items:
            if item["finding_id"] not in known:
                ctx.findings.append({k: v for k, v in item.items() if k != "_risk"})
    finally:
        close = getattr(checker, "aclose", None)
        if close:
            await close()

    high_risk: dict[str, list[str]] = {}
    risks_by_cell: dict[str, list[dict[str, Any]]] = {}
    for item in verified_risks:
        cell = item["localization"].removeprefix("plr:")
        severity = item["severity"] if item["severity"] in {"low", "medium", "high"} else "low"
        risks_by_cell.setdefault(cell, []).append(
            {"severity": severity, "text": item["claim"], "quote": item["quote"], "evidence_id": item["evidence_ids"][0], "finding_id": item["finding_id"]}
        )
        if severity == "high":
            high_risk.setdefault(cell, []).append(item["finding_id"])
        elif severity == "medium":
            reasons.setdefault(cell, []).append("verified medium risk")
        ctx.log.emit("skeptic_risk", f"skeptic:{cell}", {"finding_id": item["finding_id"], "severity": severity, "text": item["claim"]})

    final = scoring.score_area(inputs, template, berlin_reference=reference, modifiers=modifiers, extra_confidence_reasons=reasons)
    for cell in final.cells:
        cell.risks = risks_by_cell.get(cell.cell_id, [])
    final.cells = scoring.rank(final.cells, "present", high_risk=high_risk)
    ctx.log.emit("rescore", "scorer", {"version": "final", "demoted": high_risk, "order": [c.cell_id for c in final.cells[:3]]}, why="High risks demote, scores unchanged")

    doc = scoring.to_cells_document(final, area | {"plr_count": len(area.get("plr_ids", []))}, template)
    cells = doc.model_dump(by_alias=True) if hasattr(doc, "model_dump") else dict(doc)
    _apply_layer_approval(cells, ctx.run)
    accepted_all = [f for f in ctx.findings if f.get("status") != "rejected"]
    report = await research.write_report(
        ctx.question, {"area": area_payload, "cells": cells}, [Finding.model_validate(f) for f in accepted_all], ctx.ledger, ctx.log.emitter("writer"), cfg
    )
    return {"cells": cells, "report_md": report, "cells_geojson": port.geojson(area_id)}


def _apply_layer_approval(cells: dict[str, Any], run: dict[str, Any]) -> None:
    approved = {layer["id"]: layer for layer in run.get("approval", {}).get("layers") or []}
    for layer in cells.get("layers", []):
        if layer["id"] in approved:
            layer["enabled"] = approved[layer["id"]]["enabled"]
            layer["weight_default"] = approved[layer["id"]]["weight"]
        else:
            layer.setdefault("enabled", True)


async def run_point(ctx: Context) -> dict[str, Any]:
    t1, research = ctx.deps.t1, ctx.deps.research
    if t1 is None:
        raise RunFailure("T1 point research is not available (app.application.t1 is not installed)")
    cfg = ctx.deps.config_factory()
    prepared = ctx.run["prepared"]
    approved = set(ctx.run.get("approval", {}).get("hypothesis_ids") or [h["id"] for h in prepared.get("hypotheses", [])])
    hypotheses = [h for h in prepared.get("hypotheses", []) if h["id"] in approved]
    waves = await asyncio.gather(
        *[
            _guard(ctx, f"researcher:{h['id']}", research.research_hypothesis(ctx.question, prepared.get("space", {}), h, ctx.ledger, ctx.log.emitter(f"researcher:{h['id']}"), cfg), [])
            for h in hypotheses
        ]
    )
    ctx.findings = [_finding_dict(f) for wave in waves for f in wave]
    checker = ctx.deps.checker_factory()
    try:
        accepted = await fact_check(ctx, list(ctx.findings), checker)
    finally:
        close = getattr(checker, "aclose", None)
        if close:
            await close()
    prepared = {**prepared, "hypotheses": hypotheses}
    candidates = await _maybe(t1.build_candidates(prepared, [Finding.model_validate(f) for f in accepted], {"operator_profile": ctx.run.get("profile")}))
    doc = candidates.model_dump(by_alias=True) if hasattr(candidates, "model_dump") else dict(candidates)
    ctx.log.emit("score", "scorer", {"hypotheses": [h["id"] for h in doc.get("hypotheses", [])]})
    report = await research.write_report(
        ctx.question, {"candidates": doc}, [Finding.model_validate(f) for f in accepted], ctx.ledger, ctx.log.emitter("writer"), cfg
    )
    return {"candidates": doc, "report_md": report, "cells_geojson": prepared.get("cells_geojson")}


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in rows), encoding="utf-8")


def self_grade(evidence: list[dict[str, Any]], findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    verified = sum(1 for f in findings if f.get("verification") == "verified")
    partial = sum(1 for f in findings if f.get("verification") == "partial")
    unsupported = sum(1 for f in findings if f.get("verification") == "unsupported")
    return [
        {"ok": unsupported == 0, "text": f"Findings: {verified} verified, {partial} partial, {unsupported} unsupported (dropped)"},
        {"ok": bool(evidence), "text": f"{len(evidence)} ledger entries back the layers and findings"},
    ]


async def execute(ctx: Context) -> dict[str, Any]:
    """Run all stages and write ``runs/<id>/…``; returns the ``RunResult`` payload."""
    run = ctx.run
    ctx.log.emit("status", "orchestrator", {"status": "running"}, why="Plan approved; research started")
    output = await (run_point(ctx) if run["shape"] == "point" else run_area(ctx))
    entries = ctx.ledger.entries()
    report = strip_unknown_citations(output.get("report_md") or "", entries)
    if report.stripped:
        ctx.log.emit("write", "writer", {"stripped_citations": list(report.stripped)}, why="Citations not in the ledger were removed")
    ctx.run_dir.mkdir(parents=True, exist_ok=True)
    (ctx.run_dir / "report.md").write_text(report.text, encoding="utf-8")
    _write_jsonl(ctx.run_dir / "findings.jsonl", ctx.findings)
    if output.get("cells") is not None:
        (ctx.run_dir / "cells.json").write_text(json.dumps(output["cells"], ensure_ascii=False, indent=2), encoding="utf-8")
    if output.get("candidates") is not None:
        (ctx.run_dir / "candidates.json").write_text(json.dumps(output["candidates"], ensure_ascii=False, indent=2), encoding="utf-8")
    ctx.log.emit("write", "orchestrator", {"files": sorted(p.name for p in ctx.run_dir.iterdir())})
    evidence = [EvidenceEntry.model_validate(e).model_dump(exclude_none=True) for e in entries]
    stats = metrics(ctx.log.events)
    (ctx.run_dir / "metrics.json").write_text(json.dumps(stats, indent=2), encoding="utf-8")
    ctx.log.emit("metrics", "orchestrator", stats)
    ctx.log.emit("status", "orchestrator", {"status": "done"})
    return {
        "run_id": run["run_id"],
        "status": "done",
        "report_md": report.text,
        "cells": output.get("cells"),
        "evidence": evidence,
        "findings": ctx.findings,
        "metrics": stats,
        "question": run["question"],
        "role": run.get("role"),
        "shape": run["shape"],
        "plan": run.get("plan"),
        "cells_geojson": output.get("cells_geojson"),
        "candidates": output.get("candidates"),
        "self_grade": self_grade(evidence, ctx.findings),
        "trace": list(ctx.log.events),
    }
