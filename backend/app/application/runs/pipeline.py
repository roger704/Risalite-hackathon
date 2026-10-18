"""Post-approval run stages (Deep_Research_Spec §4–§5): data → score → wave 1 → wave 2 →
fact-check → merge → rescore → skeptic → fact-check risks → rank → writer → files.

T1 (point): hypotheses → research per hypothesis → fact-check → fit → skeptic per hypothesis →
fact-check risks → candidates → writer.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import inspect
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.contracts.evidence import EvidenceEntry, Finding
from app.evidence.citations import strip_unknown_citations
from app.evidence.factcheck import fact_check_run
from app.evidence.ledger import EvidenceLedger, entry_status, utc_now

from .events import EventLog, metrics
from .planning import FUTURE_UNITS

FUTURE_CLAIMS = {"future", "future_project"}
UNITS = {"dwellings", "kita_places", "children_0_6"}
ACCEPTED = {"verified", "partial"}
SEVERITIES = {"low", "medium", "high"}
RUN_MAX_SEARCHES = 40  # hard cap for one run, shared by every agent (§4.4)
RUN_MAX_EXTRACTS = 40
SMALL_BUDGET = {"max_searches": 3, "max_extracts": 3}  # wave 2 and skeptic calls
# Research ``kind`` → template ``future_effects`` key; the first key the template knows wins.
KIND_ALIASES: dict[str, tuple[str, ...]] = {
    "housing": ("housing_growth", "bplan_housing"),
    "bplan": ("bplan_housing", "bplan_kita_site"),
    "population": ("population_growth", "children_forecast_increase"),
    "children": ("children_forecast_increase",),
    "cafe_opening": ("cafe_opening",),
    "kita_opening": ("kita_opening",),
    "kita_closure": ("kita_closure",),
}


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
    t1_readers: Callable[[], Any] | None = None
    connector_factory: Callable[[Any], Any] | None = None


@dataclass
class Context:
    run: dict[str, Any]
    run_dir: Path
    log: EventLog
    ledger: EvidenceLedger
    deps: Deps
    findings: list[dict[str, Any]] = field(default_factory=list)
    cfg: Any = None

    @property
    def approved_subquestions(self) -> list[dict[str, Any]]:
        plan = self.run.get("plan") or {}
        subs = plan.get("sub_questions", [])
        approved = self.run.get("approval", {}).get("subquestions")
        return [s for s in subs if approved is None or s["id"] in approved][:3]

    @property
    def question(self) -> str:
        subs = [s["text"] for s in self.approved_subquestions]
        return self.run["question"] + (
            "\n\nApproved sub-questions:\n" + "\n".join(f"- {s}" for s in subs) if subs else ""
        )


async def _maybe(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def _guard(ctx: Context, agent: str, call: Awaitable[Any], default: Any) -> Any:
    try:
        return await call
    except Exception as exc:
        ctx.log.emit(
            "tool_error",
            agent,
            {"error": f"{type(exc).__name__}: {exc}"},
            why="Agent failed; its recorded findings are kept",
        )
        return default


def _with(cfg: Any, **changes: Any) -> Any:
    """``dataclasses.replace`` for ResearchConfig; plain namespaces (tests) are copied."""
    if dataclasses.is_dataclass(cfg) and not isinstance(cfg, type):
        return dataclasses.replace(cfg, **changes)
    return SimpleNamespace(**{**vars(cfg), **changes})


def small(cfg: Any) -> Any:
    return _with(cfg, **SMALL_BUDGET)


def _berlin_reference_timeout() -> float:
    try:
        return max(0.0, float(os.environ.get("RISALITE_BERLIN_REF_TIMEOUT_S", "90")))
    except ValueError:
        return 90.0


def _finding_dict(finding: Any) -> dict[str, Any]:
    data = finding.model_dump(by_alias=True) if hasattr(finding, "model_dump") else dict(finding)
    return Finding.model_validate(data).model_dump(exclude_none=False)


def _add_findings(ctx: Context, rows: list[Any], **tags: Any) -> list[dict[str, Any]]:
    """Append new findings (dedup by id and by claim+evidence) and return the added dicts."""
    seen_ids = {f["finding_id"] for f in ctx.findings}
    seen_claims = {(f.get("claim"), tuple(f.get("evidence_ids") or ())) for f in ctx.findings}
    added = []
    for row in rows:
        item = _finding_dict(row)
        for key, value in tags.items():
            if value is not None and not item.get(key):
                item[key] = value
        claim = (item.get("claim"), tuple(item.get("evidence_ids") or ()))
        if item["finding_id"] in seen_ids or claim in seen_claims:
            continue
        seen_ids.add(item["finding_id"])
        seen_claims.add(claim)
        ctx.findings.append(item)
        added.append(item)
    return added


def accepted(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Only fact-checked verified/partial findings may change scores or reach the writer."""
    return [
        f
        for f in findings
        if f.get("status") != "rejected"
        and f.get("verification") in ACCEPTED
        and f.get("evidence_ids")
    ]


# --- fact-check ----------------------------------------------------------------------------


class _Trace:
    """``TraceWriter`` adapter so fact-check events go through the run's EventLog."""

    def __init__(self, log: EventLog):
        self.log = log

    def emit(self, type: str, agent: str, why: str | None = None, **fields: Any) -> dict[str, Any]:
        detail = fields.pop("detail", None)
        return self.log.emit(type, agent, detail, why=why, **fields)


def pass_through_datasets(ledger: EvidenceLedger) -> int:
    """Mark dataset entries derived in this run as ``partial`` before the fact-checker runs.

    They are aggregates computed from a pinned snapshot (layer values, T1 catchment counts);
    the checker cannot re-read them from the snapshot record, so instead of letting them become
    ``unsupported`` they pass through as "dataset, not re-verified" (§5.4 partial).
    """
    count = 0
    for entry in ledger.entries():
        if entry.get("method") == "dataset" and entry_status(entry) == "unverified":
            reason = f"dataset value derived in this run from snapshot {entry.get('snapshot_id') or '?'}; not re-verified"
            ledger.set_status(
                entry["evidence_id"],
                "partial",
                {"checked_at": utc_now(), "ratio": None, "reason": reason},
            )
            count += 1
    return count


async def fact_check_stage(ctx: Context, checker: Any) -> list[dict[str, Any]]:
    """Write findings.jsonl, run ``app.evidence.fact_check_run`` (writes factcheck.json), read
    the verified findings back. Unsupported ones are marked rejected and logged."""
    ctx.run_dir.mkdir(parents=True, exist_ok=True)
    pass_through_datasets(ctx.ledger)
    pending = [
        f for f in ctx.findings if not f.get("verification") and f.get("status") != "rejected"
    ]
    _write_jsonl(ctx.run_dir / "findings.jsonl", ctx.findings)
    cited_ids = {
        evidence_id
        for finding in ctx.findings
        if finding.get("status") != "rejected"
        for evidence_id in (finding.get("cited_evidence_ids") or finding.get("evidence_ids") or [])
    }
    await fact_check_run(ctx.run_dir, checker, trace=_Trace(ctx.log), only_ids=cited_ids)
    ctx.findings = _read_jsonl(ctx.run_dir / "findings.jsonl")
    fresh = {f["finding_id"] for f in pending}
    for item in ctx.findings:
        if item["finding_id"] in fresh and item.get("verification") == "unsupported":
            item["status"] = "rejected"
            item["reject_reason"] = item.get("fact_check_reason") or "unsupported on re-check"
            ctx.log.emit(
                "finding_rejected",
                "factcheck",
                {"finding_id": item["finding_id"], "claim": item.get("claim")},
                why="unsupported on re-check",
            )
    _write_jsonl(ctx.run_dir / "findings.jsonl", ctx.findings)
    return accepted(ctx.findings)


# --- area (T2/T3) --------------------------------------------------------------------------


def _cell_of(localization: str | None, cell_ids: set[str]) -> str | None:
    if not localization:
        return None
    value = localization.removeprefix("plr:")
    return value if value in cell_ids else None


def future_kind(kind: str | None, unit: str | None, template: Any) -> str | None:
    effects = getattr(template, "future_effect", {}) or {}
    if kind in effects:
        return kind
    if unit == "children_0_6" and "children_forecast_increase" in effects:
        return "children_forecast_increase"
    return next((key for key in KIND_ALIASES.get(str(kind), ()) if key in effects), None)


def future_modifiers(
    ctx: Context, findings: list[dict[str, Any]], cell_ids: set[str], template: Any
) -> list[Any]:
    FutureModifier = ctx.deps.scoring.FutureModifier
    units = getattr(template, "future_units", {}) or {}
    modifiers, seen, ignored = [], set(), []
    for item in findings:
        cell = _cell_of(item.get("localization"), cell_ids)
        if item.get("claim_type") not in FUTURE_CLAIMS or cell is None or not item["evidence_ids"]:
            continue
        unit = item.get("unit") if item.get("unit") in UNITS else None
        kind = future_kind(item.get("kind"), unit, template)
        if kind is None:
            ignored.append(item["finding_id"])
            continue
        key = (cell, kind, item["evidence_ids"][0])
        if key in seen:
            continue
        seen.add(key)
        try:
            amount = None if item.get("amount") is None else float(item["amount"])
        except (TypeError, ValueError):
            amount = None
        year = item.get("completion_year")
        modifiers.append(
            FutureModifier(
                cell_id=cell,
                kind=kind,
                unit=unit or units.get(kind) or FUTURE_UNITS.get(ctx.run["shape"]),
                amount=amount,
                evidence_id=item["evidence_ids"][0],
                label=item["claim"],
                quote=item["quote"],
                completion_year=int(year) if isinstance(year, (int, float)) else None,
                subquestion_id=item.get("subquestion_id"),
            )
        )
    if ignored:
        ctx.log.emit(
            "merge",
            "orchestrator",
            {"ignored_future_findings": ignored},
            why="Future kind has no effect in this template",
        )
    return modifiers


def _template(ctx: Context) -> Any:
    template = ctx.deps.scoring.load_template(ctx.run["plan"]["template_id"])
    approved = {layer["id"]: layer for layer in ctx.run.get("approval", {}).get("layers") or []}
    layers = tuple(
        dataclasses.replace(
            layer,
            weight=float(approved[layer.id]["weight"]) if approved[layer.id]["enabled"] else 0.0,
        )
        if layer.id in approved
        else layer
        for layer in template.layers
    )
    return dataclasses.replace(template, layers=layers)


def _weights(template: Any) -> dict[str, float]:
    return {layer.id: float(layer.weight) for layer in template.layers}


def _cells_payload(cells: list[Any]) -> list[dict[str, Any]]:
    return [{"id": c.cell_id, "name": c.name, "parent_name": c.parent_name} for c in cells]


def _cell_payload(cell: Any) -> dict[str, Any]:
    return {
        "id": cell.cell_id,
        "name": cell.name,
        "parent_name": cell.parent_name,
        "raw_values": dict(cell.raw_values),
    }


def _require_data(ctx: Context, template: Any, inputs: list[Any], area: dict[str, Any]) -> None:
    unmapped = set(ctx.deps.port.unmapped_layers(template))
    required = [layer.id for layer in template.layers if getattr(layer, "required", False)]
    missing = [r for r in required if r in unmapped or all(c.raw.get(r) is None for c in inputs)]
    if ctx.run["shape"] == "gap" and (missing or not inputs):
        raise RunFailure(
            f"Kita gap research ({template.id}) is not available yet: no data for "
            f"{', '.join(missing or required) or 'required layers'} in {area['name']} "
            "(needs berlin_population + berlin_kitas ingestion, #38)."
        )
    if not inputs:
        raise RunFailure(f"No PLR cells with data for {area['name']} ({area['area_id']})")


async def run_area(ctx: Context) -> dict[str, Any]:
    scoring, research, port = ctx.deps.scoring, ctx.deps.research, ctx.deps.port
    cfg = ctx.cfg
    approval = ctx.run.get("approval", {})
    area_id = ctx.run["plan"]["area"]["area_id"]
    template = _template(ctx)
    weights = _weights(template)

    area = port.resolve(area_id)
    if ctx.run["shape"] == "gap" and set(port.unmapped_layers(template)) >= {
        layer.id for layer in template.layers if getattr(layer, "required", False)
    }:
        _require_data(ctx, template, [], area)
    inputs = await asyncio.to_thread(port.cells, area_id, template, ledger=ctx.ledger)
    _require_data(ctx, template, inputs, area)
    for layer in template.layers:
        present = sum(1 for c in inputs if c.raw.get(layer.id) is not None)
        ctx.log.emit(
            "layer",
            "data",
            {
                "layer_id": layer.id,
                "cells": len(inputs),
                "present": present,
                "weight": layer.weight,
            },
            why="Layer values per PLR from pinned snapshots",
        )
    timeout_s = _berlin_reference_timeout()
    try:
        reference = (
            await asyncio.wait_for(
                asyncio.to_thread(port.berlin_reference, template), timeout=timeout_s
            )
            or None
        )
    except TimeoutError:
        reference = None
        ctx.log.emit(
            "tool_error",
            "data",
            {
                "stage": "berlin_reference",
                "error": f"Timed out after {timeout_s:g} seconds",
            },
            why="Berlin reference unavailable; scoring continues without it",
        )
        ctx.log.emit(
            "status",
            "orchestrator",
            {"stage": "berlin_reference", "status": "timeout", "score_berlin": None},
        )
    reasons = port.confidence_reasons(area_id)
    crosscheck = getattr(port, "crosscheck", None)
    crosscheck = crosscheck(area_id) if crosscheck else {}

    def score(
        modifiers: list[Any] = (), risks: dict[str, list[dict[str, Any]]] | None = None
    ) -> Any:
        return scoring.score_area(
            inputs,
            template,
            berlin_reference=reference,
            modifiers=modifiers,
            extra_confidence_reasons={k: list(v) for k, v in reasons.items()},
            weights=weights,
            crosscheck=crosscheck,
            risks=risks,
        )

    def top(result: Any) -> list[Any]:
        ranked = scoring.rank(result.cells, "present", tie_band=template.tie_band)
        return [c for c in ranked if not c.incomplete][:3]

    v0 = score()
    top3 = top(v0)
    ctx.log.emit(
        "score",
        "scorer",
        {"version": "v0", "top3": [c.cell_id for c in top3], "excluded": v0.excluded},
        why="Top-3 by score_present drive wave 2",
    )

    by_id = {c.cell_id: c for c in inputs}
    area_payload = {"area_id": area_id, "name": area["name"]}
    cells_payload = _cells_payload(inputs)
    subs = ctx.approved_subquestions or [{"id": None, "text": ctx.run["question"]}]
    wave1 = await asyncio.gather(
        *[
            _guard(
                ctx,
                f"researcher:wave1:{n}",
                research.research_area(
                    ctx.question,
                    area_payload,
                    cells_payload,
                    ctx.ledger,
                    ctx.log.emitter(f"researcher:wave1:{n}"),
                    cfg,
                    subquestions=[sub["text"]],
                    agent=f"researcher:wave1:{n}",
                    subquestion_id=sub["id"],
                ),
                [],
            )
            for n, sub in enumerate(subs, 1)
        ]
    )
    for sub, rows in zip(subs, wave1):
        _add_findings(ctx, rows, subquestion_id=sub["id"])
    if approval.get("wave2", True) is not False:
        wave2 = await asyncio.gather(
            *[
                _guard(
                    ctx,
                    f"researcher:wave2:{c.cell_id}",
                    research.research_cell(
                        ctx.question,
                        _cell_payload(c),
                        ctx.ledger,
                        ctx.log.emitter(f"researcher:wave2:{c.cell_id}"),
                        small(cfg),
                        context=[
                            Finding.model_validate(f)
                            for f in ctx.findings
                            if _cell_of(f.get("localization"), {c.cell_id})
                        ],
                        agent=f"researcher:wave2:{c.cell_id}",
                    ),
                    [],
                )
                for c in top3
            ]
        )
        for rows in wave2:
            _add_findings(ctx, rows)
    ctx.log.emit("status", "orchestrator", {"status": "reviewing", "findings": len(ctx.findings)})

    checker = ctx.deps.checker_factory()
    try:
        verified = await fact_check_stage(ctx, checker)
        modifiers = future_modifiers(ctx, verified, set(by_id), template)
        ctx.log.emit(
            "merge",
            "orchestrator",
            {
                "accepted": len(verified),
                "modifiers": [{"cell_id": m.cell_id, "kind": m.kind} for m in modifiers],
            },
            why="Only verified/partial findings reach modifiers",
        )
        v1 = score(modifiers)
        top3 = top(v1)
        ctx.log.emit(
            "rescore",
            "scorer",
            {"version": "v1", "top3": [c.cell_id for c in top3]},
            why="Future modifiers merged",
        )

        skeptics = await asyncio.gather(
            *[
                _guard(
                    ctx,
                    f"skeptic:{c.cell_id}",
                    research.skeptic(
                        ctx.question,
                        _cell_payload(c),
                        [
                            Finding.model_validate(f)
                            for f in verified
                            if _cell_of(f.get("localization"), {c.cell_id})
                        ],
                        ctx.ledger,
                        ctx.log.emitter(f"skeptic:{c.cell_id}"),
                        small(cfg),
                        agent=f"skeptic:{c.cell_id}",
                    ),
                    [],
                )
                for c in top3
            ]
        )
        risk_meta = _add_risks(
            ctx, [(c.cell_id, f"plr:{c.cell_id}", risks) for c, risks in zip(top3, skeptics)]
        )
        await fact_check_stage(ctx, checker)
    finally:
        close = getattr(checker, "aclose", None)
        if close:
            await close()

    high_risk: dict[str, list[str]] = {}
    risks_by_cell: dict[str, list[dict[str, Any]]] = {}
    for item in _verified_risks(ctx, risk_meta):
        cell, severity = (
            risk_meta[item["finding_id"]]["target"],
            risk_meta[item["finding_id"]]["severity"],
        )
        risks_by_cell.setdefault(cell, []).append(
            {
                "severity": severity,
                "text": item["claim"],
                "quote": item["quote"],
                "evidence_id": item["evidence_ids"][0],
                "finding_id": item["finding_id"],
            }
        )
        if severity == "high":
            high_risk.setdefault(cell, []).append(item["finding_id"])
        ctx.log.emit(
            "skeptic_risk",
            f"skeptic:{cell}",
            {
                "finding_id": item["finding_id"],
                "severity": severity,
                "text": item["claim"],
                "verification": item.get("verification"),
            },
            why="Fact-checked risk",
        )

    final = score(modifiers, risks_by_cell)
    final.cells = scoring.rank(
        final.cells, "present", high_risk=high_risk, tie_band=template.tie_band
    )
    ctx.log.emit(
        "rescore",
        "scorer",
        {"version": "final", "demoted": high_risk, "order": [c.cell_id for c in final.cells[:3]]},
        why="High risks demote, scores unchanged",
    )

    doc = scoring.to_cells_document(
        final, area | {"plr_count": len(area.get("plr_ids", []))}, template
    )
    cells = doc.model_dump(by_alias=True) if hasattr(doc, "model_dump") else dict(doc)
    _apply_layer_approval(cells, ctx.run)
    report = await research.write_report(
        ctx.question,
        {"area": area_payload, "cells": cells, "operator": ctx.run.get("profile")},
        [Finding.model_validate(f) for f in accepted(ctx.findings)],
        ctx.ledger,
        ctx.log.emitter("writer"),
        cfg,
    )
    return {"cells": cells, "report_md": report, "cells_geojson": port.geojson(area_id)}


def _add_risks(
    ctx: Context, groups: list[tuple[str, str, list[dict[str, Any]]]], **tags: Any
) -> dict[str, dict[str, Any]]:
    """Skeptic risks → findings.jsonl rows (claim_type risk); returns finding_id → target/severity."""
    meta: dict[str, dict[str, Any]] = {}
    for target, localization, risks in groups:
        for n, risk in enumerate(risks):
            row = dict(risk.get("finding") or {})
            if not row:
                row = {
                    "finding_id": risk.get("finding_id") or f"r_{target}_{n}",
                    "claim": risk.get("text", ""),
                    "quote": risk.get("quote") or "",
                    "evidence_ids": list(
                        risk.get("evidence_ids")
                        or ([risk["evidence_id"]] if risk.get("evidence_id") else [])
                    ),
                    "agent": risk.get("agent") or f"skeptic:{target}",
                    "status": "accepted",
                }
            severity = risk.get("severity") if risk.get("severity") in SEVERITIES else "low"
            row.update(claim_type="risk", severity=severity, localization=localization, **tags)
            row.setdefault("agent", f"skeptic:{target}")
            for added in _add_findings(ctx, [row]):
                meta[added["finding_id"]] = {"target": target, "severity": severity}
    return meta


def _verified_risks(ctx: Context, meta: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [f for f in accepted(ctx.findings) if f["finding_id"] in meta]


def _apply_layer_approval(cells: dict[str, Any], run: dict[str, Any]) -> None:
    approved = {layer["id"]: layer for layer in run.get("approval", {}).get("layers") or []}
    for layer in cells.get("layers", []):
        if layer["id"] in approved:
            layer["enabled"] = approved[layer["id"]]["enabled"]
            layer["weight_default"] = approved[layer["id"]]["weight"]
        else:
            layer.setdefault("enabled", True)


# --- point (T1) ----------------------------------------------------------------------------


async def run_point(ctx: Context) -> dict[str, Any]:
    t1, research = ctx.deps.t1, ctx.deps.research
    if t1 is None:
        raise RunFailure("T1 point research is not available (app.application.t1 is not installed)")
    cfg = ctx.cfg
    prepared = ctx.run["prepared"]
    approved = set(
        ctx.run.get("approval", {}).get("hypothesis_ids")
        or [h["id"] for h in prepared.get("hypotheses", [])]
    )
    hypotheses = [h for h in prepared.get("hypotheses", []) if h["id"] in approved]
    if not hypotheses:
        raise RunFailure("No approved hypotheses for this address")
    space = prepared.get("space", {})
    waves = await asyncio.gather(
        *[
            _guard(
                ctx,
                f"researcher:t1:{h['id']}",
                research.research_hypothesis(
                    ctx.question,
                    space,
                    h,
                    ctx.ledger,
                    ctx.log.emitter(f"researcher:t1:{h['id']}"),
                    cfg,
                    agent=f"researcher:t1:{h['id']}",
                ),
                [],
            )
            for h in hypotheses
        ]
    )
    for h, rows in zip(hypotheses, waves):
        for item in _add_findings(ctx, rows, hypothesis_id=h["id"]):
            item["hypothesis_id"] = h["id"]
    ctx.log.emit("status", "orchestrator", {"status": "reviewing", "findings": len(ctx.findings)})
    prepared = {**prepared, "hypotheses": hypotheses}

    checker = ctx.deps.checker_factory()
    try:
        verified = await fact_check_stage(ctx, checker)
        fit = (
            await _guard(
                ctx,
                "fit",
                research.derive_fit(
                    ctx.question,
                    prepared,
                    [Finding.model_validate(f) for f in verified],
                    ctx.log.emitter("fit"),
                    cfg,
                ),
                {},
            )
            if hasattr(research, "derive_fit")
            else {}
        )
        ctx.log.emit(
            "merge",
            "orchestrator",
            {"accepted": len(verified), "fit": {h: sorted(d) for h, d in (fit or {}).items()}},
            why="Fit from verified findings only",
        )
        skeptics = await asyncio.gather(
            *[
                _guard(
                    ctx,
                    f"skeptic:{h['id']}",
                    research.skeptic(
                        ctx.question,
                        {
                            "id": h["id"],
                            "name": f"{h.get('name', h['id'])} at {space.get('address', 'the address')}",
                            "raw_values": dict(h.get("metrics") or {}),
                        },
                        [
                            Finding.model_validate(f)
                            for f in verified
                            if f.get("hypothesis_id") == h["id"]
                        ],
                        ctx.ledger,
                        ctx.log.emitter(f"skeptic:{h['id']}"),
                        small(cfg),
                        agent=f"skeptic:{h['id']}",
                    ),
                    [],
                )
                for h in hypotheses
            ]
        )
        for h, risks in zip(hypotheses, skeptics):
            _add_risks(ctx, [(h["id"], "address", risks)], hypothesis_id=h["id"], stance="against")
        await fact_check_stage(ctx, checker)
    finally:
        close = getattr(checker, "aclose", None)
        if close:
            await close()

    candidates = await _maybe(
        t1.build_candidates(prepared, accepted(ctx.findings), fit or {}, ledger=ctx.ledger)
    )
    doc = (
        candidates.model_dump(by_alias=True)
        if hasattr(candidates, "model_dump")
        else dict(candidates)
    )
    ctx.log.emit(
        "score",
        "scorer",
        {
            "hypotheses": [
                {"id": h["id"], "score": h.get("score"), "rank_final": h.get("rank_final")}
                for h in doc.get("hypotheses", [])
            ]
        },
        why="Candidates from data pre-score, fit and verified claims",
    )
    report = await research.write_report(
        ctx.question,
        {"space": space, "candidates": doc, "operator": ctx.run.get("profile")},
        [Finding.model_validate(f) for f in accepted(ctx.findings)],
        ctx.ledger,
        ctx.log.emitter("writer"),
        cfg,
    )
    return {"candidates": doc, "report_md": report, "cells_geojson": prepared.get("cells_geojson")}


# --- files ---------------------------------------------------------------------------------


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in rows),
        encoding="utf-8",
    )


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def self_grade(
    evidence: list[dict[str, Any]], findings: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    verified = sum(1 for f in findings if f.get("verification") == "verified")
    partial = sum(1 for f in findings if f.get("verification") == "partial")
    unsupported = sum(1 for f in findings if f.get("verification") == "unsupported")
    return [
        {
            "ok": unsupported == 0,
            "text": f"Findings: {verified} verified, {partial} partial, {unsupported} unsupported (dropped)",
        },
        {
            "ok": bool(evidence),
            "text": f"{len(evidence)} ledger entries back the layers and findings",
        },
    ]


@contextlib.asynccontextmanager
async def run_config(ctx: Context) -> AsyncIterator[Any]:
    """One ResearchConfig per run; outside fake mode one shared Tavily client (run-level cap of
    ``RUN_MAX_SEARCHES`` searches) is passed to every agent as ``cfg.connector``."""
    cfg = ctx.deps.config_factory()
    if getattr(cfg, "fake", False) or getattr(cfg, "connector", None) is not None:
        yield cfg
        return
    factory = ctx.deps.connector_factory or tavily_connector
    async with factory(cfg) as connector:
        ctx.log.emit(
            "status",
            "orchestrator",
            {"connector": type(connector).__name__, "max_searches": RUN_MAX_SEARCHES},
            why="One shared search client per run",
        )
        yield _with(cfg, connector=connector)


@contextlib.asynccontextmanager
async def tavily_connector(cfg: Any) -> AsyncIterator[Any]:
    from app.connectors.tavily import TavilyClient, TavilyConfig
    from app.domain.research import ResearchBudget

    budget = ResearchBudget(
        max_searches=int(os.environ.get("RISALITE_RUN_MAX_SEARCHES", RUN_MAX_SEARCHES)),
        max_extract_urls=int(os.environ.get("RISALITE_RUN_MAX_EXTRACTS", RUN_MAX_EXTRACTS)),
        max_inference_calls=0,
        max_tokens=0,
    )
    async with TavilyClient(
        TavilyConfig.from_env(max_results=getattr(cfg, "max_results", 5)), budget
    ) as client:
        yield client


async def execute(ctx: Context) -> dict[str, Any]:
    """Run all stages and write ``runs/<id>/…``; returns the ``RunResult`` payload."""
    run = ctx.run
    ctx.log.emit(
        "status", "orchestrator", {"status": "running"}, why="Plan approved; research started"
    )
    async with run_config(ctx) as cfg:
        ctx.cfg = cfg
        output = await (run_point(ctx) if run["shape"] == "point" else run_area(ctx))
    entries = ctx.ledger.entries()
    report = strip_unknown_citations(output.get("report_md") or "", entries)
    if report.stripped:
        ctx.log.emit(
            "write",
            "writer",
            {"stripped_citations": list(report.stripped)},
            why="Citations not in the ledger were removed",
        )
    ctx.run_dir.mkdir(parents=True, exist_ok=True)
    (ctx.run_dir / "report.md").write_text(report.text, encoding="utf-8")
    _write_jsonl(ctx.run_dir / "findings.jsonl", ctx.findings)
    # factcheck.json was written by fact_check_run before the writer (§5.7); the report keeps
    # [ev_…]/[ds_…] id markers (the UI resolves ids; fact_check_run would renumber them).
    report_md = report.text
    if output.get("cells") is not None:
        (ctx.run_dir / "cells.json").write_text(
            json.dumps(output["cells"], ensure_ascii=False, indent=2), encoding="utf-8"
        )
    if output.get("candidates") is not None:
        (ctx.run_dir / "candidates.json").write_text(
            json.dumps(output["candidates"], ensure_ascii=False, indent=2), encoding="utf-8"
        )
    ctx.log.emit("write", "orchestrator", {"files": sorted(p.name for p in ctx.run_dir.iterdir())})
    evidence = [
        EvidenceEntry.model_validate(e).model_dump(exclude_none=True) for e in ctx.ledger.entries()
    ]
    stats = metrics(ctx.log.events)
    (ctx.run_dir / "metrics.json").write_text(json.dumps(stats, indent=2), encoding="utf-8")
    ctx.log.emit("metrics", "orchestrator", stats)
    ctx.log.emit("status", "orchestrator", {"status": "done"})
    return {
        "run_id": run["run_id"],
        "status": "done",
        "report_md": report_md,
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
