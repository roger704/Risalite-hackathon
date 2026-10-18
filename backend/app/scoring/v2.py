"""Scoring v2 (Deep_Research_Spec §5.8): eligibility, transforms, percentiles, future modifiers,
confidence, tiers, rank() and stability. Deterministic; no LLM touches a number."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from app.scoring.future import (
    BONUS_CAP,
    MAX_COMPLETION_YEAR,
    bonus_size,
    capped_sum,
    future_score,
    has_number,
    signed_bonus,
)
from app.scoring.normalize import (
    MIN_PERCENTILE_CELLS,
    cap_set,
    directional,
    min_max,
    percentile_rank,
    transform,
)
from app.scoring.ranking import RankItem, rank_order, tiers, top3_shares, top_ids, weighted_score

Direction = Literal["+", "-"]
Mode = Literal["present", "future"]
Unit = Literal["dwellings", "kita_places", "children_0_6"]
Transform = Literal["identity", "log1p", "per_10k_residents", "cap_p99"]

TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "config" / "templates"
CONFIDENCE_LEVELS: tuple[str, ...] = ("high", "medium", "low")
RESIDENTS_CONFIDENCE_FLOOR = 3000
TOP3_SHARE_FLOOR = 0.5
BERLIN_BANDS = {"above": 0.70, "below": 0.40}

REASON_OPTIONAL_MISSING = "optional layer missing: {layers}"
REASON_FALLBACK = "fallback layer used: {layers}"
REASON_CROSSCHECK = "OSM/IHK café counts deviate (OSM {osm}, IHK {ihk})"
REASON_RESIDENTS = "residents below 3,000"
REASON_GAP_RANGE = "Kita capacity partly imputed (gap range)"
REASON_MEDIUM_RISK = "verified medium risk"
REASON_TOP3_SHARE = "top-3 share below 0.5 under weight perturbation"

__all__ = [
    "CellInput",
    "FutureModifier",
    "LayerSpec",
    "ScoreResult",
    "ScoredCell",
    "Template",
    "bonus_size",
    "load_template",
    "percentile_rank",
    "rank",
    "score_area",
    "to_cells_document",
    "top3",
]


@dataclass(frozen=True)
class LayerSpec:
    id: str
    label: str
    direction: Direction
    weight: float
    transform: Transform = "identity"
    required: bool = False
    unit: str | None = None
    source: str | None = None
    why: str | None = None


@dataclass(frozen=True)
class Template:
    id: str
    layers: tuple[LayerSpec, ...]
    min_population: int = 2000
    tie_band: float = 0.02
    # modifier kind -> effect direction on the score (e.g. T3 "kita_opening": "-")
    future_effect: dict[str, Direction] = field(default_factory=dict)
    label: str = ""
    future_units: dict[str, str | None] = field(default_factory=dict)
    bonus_cap: float = BONUS_CAP
    max_completion_year: int = MAX_COMPLETION_YEAR
    crosschecks: tuple[dict, ...] = ()
    params: dict[str, Any] = field(default_factory=dict)
    unknowns: dict[str, dict[str, str]] = field(default_factory=dict)


@dataclass
class CellInput:
    cell_id: str
    name: str
    residents: float | None
    raw: dict[str, float | None]
    parent_name: str | None = None
    incomplete: bool = False
    gap_low: float | None = None
    gap_high: float | None = None
    layer_evidence: dict[str, str] = field(default_factory=dict)  # layer id -> ledger id
    fallback_layers: list[str] = field(default_factory=list)  # layers computed by a fallback


@dataclass
class FutureModifier:
    cell_id: str
    kind: str
    unit: Unit
    amount: float  # None/NaN = the source states no number → size "small"
    evidence_id: str
    label: str
    quote: str
    completion_year: int | None = None  # > template.max_completion_year → text only (bonus 0)
    subquestion_id: str | None = None


@dataclass
class ScoredCell:
    cell_id: str
    name: str
    values: dict[str, float | None]  # directional 0..1 within area (1 = good)
    raw_values: dict[str, float | None]
    score_present: float | None
    score_berlin: float | None
    score_future: float | None
    modifiers: list[dict] = field(default_factory=list)
    confidence: Literal["high", "medium", "low"] = "high"
    confidence_reasons: list[str] = field(default_factory=list)
    tier: int = 0
    top3_share: float = 0.0
    incomplete: bool = False
    gap_low: float | None = None
    gap_high: float | None = None
    risks: list[dict] = field(default_factory=list)
    demoted_by: list[str] = field(default_factory=list)
    parent_name: str | None = None
    unknowns: list[dict] = field(default_factory=list)  # {"text", "suggested_validation"}
    future_bonus: float = 0.0  # capped Σ bonus
    residents: float | None = None


@dataclass
class ScoreResult:
    template_id: str
    cells: list[ScoredCell]
    excluded: list[dict]  # {"cell_id", "name", "reason"}
    weights: dict[str, float] = field(default_factory=dict)
    layer_evidence: dict[str, str] = field(default_factory=dict)
    layer_reference: dict[str, dict] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def _layer_from_json(data: dict) -> LayerSpec:
    return LayerSpec(
        id=data["id"],
        label=data.get("label", data["id"]),
        direction=data["direction"],
        weight=float(data.get("weight", 0.0)),
        transform=data.get("transform", "identity"),
        required=bool(data.get("required", False)),
        unit=data.get("unit"),
        source=data.get("source"),
        why=data.get("why"),
    )


def template_from_dict(data: dict) -> Template:
    effects = data.get("future_effects", {})
    return Template(
        id=data["id"],
        layers=tuple(_layer_from_json(layer) for layer in data["layers"]),
        min_population=int(data.get("min_population", 2000)),
        tie_band=float(data.get("tie_band", 0.02)),
        future_effect={
            kind: (spec["direction"] if isinstance(spec, dict) else spec)
            for kind, spec in effects.items()
        },
        label=data.get("label", ""),
        future_units={
            kind: spec.get("unit") for kind, spec in effects.items() if isinstance(spec, dict)
        },
        bonus_cap=float(data.get("bonus_cap", BONUS_CAP)),
        max_completion_year=int(data.get("max_completion_year", MAX_COMPLETION_YEAR)),
        crosschecks=tuple(data.get("crosschecks", ())),
        params=dict(data.get("params", {})),
        unknowns=dict(data.get("unknowns", {})),
    )


def load_template(template_id: str) -> Template:
    """Read backend/config/templates/<template_id>.json."""
    if not template_id or "/" in template_id or "\\" in template_id or template_id.startswith("."):
        raise ValueError(f"invalid template id: {template_id!r}")
    path = TEMPLATE_DIR / f"{template_id}.json"
    if not path.is_file():
        raise FileNotFoundError(f"unknown template: {template_id}")
    return template_from_dict(json.loads(path.read_text(encoding="utf-8")))


def _weights(template: Template, overrides: Mapping[str, float] | None) -> dict[str, float]:
    weights = {layer.id: layer.weight for layer in template.layers}
    for layer_id, weight in (overrides or {}).items():
        if layer_id not in weights:
            raise ValueError(f"unknown layer: {layer_id}")
        if weight < 0:
            raise ValueError(f"negative weight for {layer_id}")
        weights[layer_id] = float(weight)
    return weights


def _exclusion_reason(
    cell: CellInput, template: Template, weights: Mapping[str, float]
) -> str | None:
    if cell.residents is None:
        return "residents unknown"
    if cell.residents < template.min_population:
        return f"residents below {template.min_population:,} ({cell.residents:,.0f})"
    missing = [
        layer.id
        for layer in template.layers
        if layer.required and weights[layer.id] > 0 and cell.raw.get(layer.id) is None
    ]
    if missing:
        return "required layer missing: " + ", ".join(missing)
    return None


def _split(
    cells: Sequence[CellInput], template: Template, weights: Mapping[str, float]
) -> tuple[list[CellInput], list[dict]]:
    eligible: list[CellInput] = []
    excluded: list[dict] = []
    for cell in cells:
        reason = _exclusion_reason(cell, template, weights)
        if reason is None:
            eligible.append(cell)
        else:
            excluded.append({"cell_id": cell.cell_id, "name": cell.name, "reason": reason})
    return eligible, excluded


def _transformed(
    cells: Sequence[CellInput], layer: LayerSpec, cap: float | None = None
) -> tuple[dict[str, float | None], float | None]:
    values = {
        cell.cell_id: transform(layer.transform, cell.raw.get(layer.id), cell.residents)
        for cell in cells
    }
    if cap is None:
        cap = cap_set(layer.transform, [v for v in values.values() if v is not None])
    if cap is not None:
        values = {key: (min(v, cap) if v is not None else None) for key, v in values.items()}
    return values, cap


def build_reference(
    reference: Sequence[CellInput], template: Template, weights: Mapping[str, float] | None = None
) -> dict[str, tuple[list[float], float | None]]:
    """Berlin reference per layer: (transformed values of eligible PLR, cap) — cacheable."""
    eligible, _ = _split(reference, template, _weights(template, weights))
    result: dict[str, tuple[list[float], float | None]] = {}
    for layer in template.layers:
        values, cap = _transformed(eligible, layer)
        result[layer.id] = (sorted(v for v in values.values() if v is not None), cap)
    return result


def crosscheck_deviates(osm: int, ihk: int, *, min_count: int = 3, max_ratio: float = 2.0) -> bool:
    """§5.8.2: both counts ≥ min_count and max/min > max_ratio. Counts are never summed."""
    if osm < min_count or ihk < min_count:
        return False
    return max(osm, ihk) / min(osm, ihk) > max_ratio


def _modifier_entries(
    modifiers: Sequence[FutureModifier], template: Template, cell_ids: set[str], notes: list[str]
) -> dict[str, list[dict]]:
    entries: dict[str, list[dict]] = {}
    for modifier in modifiers:
        if modifier.cell_id not in cell_ids:
            continue
        direction = template.future_effect.get(modifier.kind)
        if direction is None:
            notes.append(f"modifier kind {modifier.kind!r} not in {template.id}; ignored")
            continue
        counted = (
            modifier.completion_year is None
            or modifier.completion_year <= template.max_completion_year
        )
        size, magnitude = bonus_size(modifier.unit, modifier.amount)
        numeric = has_number(modifier.amount)
        entries.setdefault(modifier.cell_id, []).append(
            {
                "type": "future",
                "kind": modifier.kind,
                "label": modifier.label,
                "quote": modifier.quote,
                "evidence_id": modifier.evidence_id,
                "localization": "plr",
                "effect": direction,
                "size": size,
                "bonus": signed_bonus(direction, magnitude) if counted else 0.0,
                "counted": counted,
                "magnitude": (
                    {
                        "value": modifier.amount,
                        "size": size,
                        **({"unit": modifier.unit} if modifier.unit else {}),
                    }
                    if numeric
                    else None
                ),
                "completion_year": modifier.completion_year,
                "subquestion_id": modifier.subquestion_id,
            }
        )
    return entries


def _crosscheck_rule(template: Template) -> dict:
    return next((c for c in template.crosschecks if c.get("id") == "cafe_crosscheck"), {})


def _confidence(
    cell: ScoredCell,
    source: CellInput,
    template: Template,
    weights: Mapping[str, float],
    crosscheck: Mapping[str, tuple[int, int]],
    extra: Sequence[str],
    in_top3: bool,
) -> list[str]:
    reasons: list[str] = []
    missing = [
        layer.id
        for layer in template.layers
        if weights[layer.id] > 0 and cell.values.get(layer.id) is None
    ]
    if missing:
        reasons.append(REASON_OPTIONAL_MISSING.format(layers=", ".join(missing)))
    fallback = [layer_id for layer_id in source.fallback_layers if weights.get(layer_id, 0) > 0]
    if fallback:
        reasons.append(REASON_FALLBACK.format(layers=", ".join(fallback)))
    if cell.cell_id in crosscheck:
        osm, ihk = crosscheck[cell.cell_id]
        rule = _crosscheck_rule(template)
        if crosscheck_deviates(
            osm, ihk, min_count=rule.get("min_count", 3), max_ratio=rule.get("max_ratio", 2.0)
        ):
            reasons.append(REASON_CROSSCHECK.format(osm=osm, ihk=ihk))
            unknown = template.unknowns.get("crosscheck")
            if unknown:
                cell.unknowns.append(
                    {
                        "text": unknown["text"].format(osm=osm, ihk=ihk),
                        "suggested_validation": unknown["suggested_validation"],
                    }
                )
    if source.residents is not None and source.residents < RESIDENTS_CONFIDENCE_FLOOR:
        reasons.append(REASON_RESIDENTS)
    if cell.gap_low is not None and cell.gap_high is not None and cell.gap_low != cell.gap_high:
        reasons.append(REASON_GAP_RANGE)
    if any(risk.get("severity") == "medium" for risk in cell.risks):
        reasons.append(REASON_MEDIUM_RISK)
    for reason in extra:
        if reason not in reasons:
            reasons.append(reason)
    if in_top3 and cell.top3_share < TOP3_SHARE_FLOOR:
        reasons.append(REASON_TOP3_SHARE)
    return reasons


def score_area(
    cells: list[CellInput],
    template: Template,
    *,
    berlin_reference: list[CellInput] | None = None,
    modifiers: Sequence[FutureModifier] = (),
    extra_confidence_reasons: dict[str, list[str]] | None = None,
    weights: Mapping[str, float] | None = None,
    crosscheck: Mapping[str, tuple[int, int]] | None = None,
    risks: Mapping[str, list[dict]] | None = None,
) -> ScoreResult:
    """Score the selected area. `weights` overrides template weights (0 = unticked);
    `crosscheck` is PLR → (OSM, IHK) café counts; `risks` is PLR → verified risks
    ({severity, text, quote, evidence_id, finding_id}); high → demoted_by, medium → confidence −1."""
    weight_map = _weights(template, weights)
    eligible, excluded = _split(cells, template, weight_map)
    notes: list[str] = []
    values: dict[str, dict[str, float | None]] = {cell.cell_id: {} for cell in eligible}
    berlin_values: dict[str, dict[str, float | None]] = {cell.cell_id: {} for cell in eligible}
    reference = (
        build_reference(berlin_reference, template, weight_map) if berlin_reference else None
    )
    layer_reference: dict[str, dict] = {}
    for layer in template.layers:
        area_values, _ = _transformed(eligible, layer)
        present = [v for v in area_values.values() if v is not None]
        method = "percentile" if len(present) >= MIN_PERCENTILE_CELLS else "min_max"
        if present and method == "min_max":
            notes.append(f"{layer.id}: n={len(present)} < {MIN_PERCENTILE_CELLS}, min–max used")
        normalize = percentile_rank if method == "percentile" else min_max
        ref_values, ref_cap = reference[layer.id] if reference else ([], None)
        berlin_raw, _ = _transformed(eligible, layer, ref_cap) if reference else ({}, None)
        for cell in eligible:
            value = area_values[cell.cell_id]
            values[cell.cell_id][layer.id] = (
                None if value is None else directional(normalize(value, present), layer.direction)
            )
            ref_value = berlin_raw.get(cell.cell_id)
            berlin_values[cell.cell_id][layer.id] = (
                None
                if ref_value is None or not ref_values
                else directional(percentile_rank(ref_value, ref_values), layer.direction)
            )
        layer_reference[layer.id] = {
            "normalization": method,
            "area_cells": len(present),
            "berlin_cells": len(ref_values) if reference else None,
        }

    modifier_map = _modifier_entries(modifiers, template, set(values), notes)
    scored: list[ScoredCell] = []
    for cell in eligible:
        present = weighted_score(values[cell.cell_id], weight_map)
        cell_modifiers = modifier_map.get(cell.cell_id, [])
        bonuses = [entry["bonus"] for entry in cell_modifiers]
        future = future_score(present, bonuses, template.bonus_cap)
        cell_risks = [dict(risk) for risk in (risks or {}).get(cell.cell_id, [])]
        demoted = [
            str(risk.get("finding_id") or risk["evidence_id"])
            for risk in cell_risks
            if risk.get("severity") == "high"
        ]
        scored_cell = ScoredCell(
            cell_id=cell.cell_id,
            name=cell.name,
            values=values[cell.cell_id],
            raw_values={layer.id: cell.raw.get(layer.id) for layer in template.layers},
            score_present=present,
            score_berlin=(
                weighted_score(berlin_values[cell.cell_id], weight_map) if reference else None
            ),
            score_future=future,
            modifiers=cell_modifiers,
            incomplete=cell.incomplete,
            gap_low=cell.gap_low,
            gap_high=cell.gap_high,
            risks=cell_risks,
            demoted_by=list(dict.fromkeys(demoted)),
            parent_name=cell.parent_name,
            future_bonus=capped_sum(bonuses, template.bonus_cap),
            residents=cell.residents,
        )
        if cell.incomplete and "incomplete" in template.unknowns:
            scored_cell.unknowns.append(dict(template.unknowns["incomplete"]))
        scored.append(scored_cell)

    ranked_pool = {
        cell.cell_id: cell.values
        for cell in scored
        if not cell.incomplete and cell.score_present is not None
    }
    shares = top3_shares(ranked_pool, weight_map)
    current_top3 = set(
        top_ids(
            {cell.cell_id: cell.score_present for cell in scored if cell.cell_id in ranked_pool}
        )
    )
    sources = {cell.cell_id: cell for cell in eligible}
    for cell in scored:
        cell.top3_share = shares.get(cell.cell_id, 0.0)
        cell.confidence_reasons = _confidence(
            cell,
            sources[cell.cell_id],
            template,
            weight_map,
            crosscheck or {},
            (extra_confidence_reasons or {}).get(cell.cell_id, []),
            cell.cell_id in current_top3,
        )
        level = min(len(cell.confidence_reasons), len(CONFIDENCE_LEVELS) - 1)
        cell.confidence = CONFIDENCE_LEVELS[level]  # type: ignore[assignment]

    layer_evidence: dict[str, str] = {}
    for cell in cells:
        for layer_id, evidence_id in cell.layer_evidence.items():
            layer_evidence.setdefault(layer_id, evidence_id)
    return ScoreResult(
        template_id=template.id,
        cells=rank(scored, "present", tie_band=template.tie_band),
        excluded=excluded,
        weights=weight_map,
        layer_evidence=layer_evidence,
        layer_reference=layer_reference,
        notes=notes,
    )


def _mode_score(cell: ScoredCell, mode: Mode) -> float | None:
    if mode == "future" and cell.score_future is not None:
        return cell.score_future
    return cell.score_present


def rank(
    cells: list[ScoredCell],
    mode: Mode = "present",
    *,
    high_risk: dict[str, list[str]] | None = None,
    tie_band: float = 0.02,
) -> list[ScoredCell]:
    """§5.5/§5.8.6: sets demoted_by, incomplete cells after all eligible ones.

    `high_risk` (cell id → finding ids) is merged into `demoted_by`; scores stay unchanged;
    `tier` is recomputed for `mode` on the score order."""
    for cell in cells:
        ids = (high_risk or {}).get(cell.cell_id)
        if ids:
            cell.demoted_by = list(dict.fromkeys([*cell.demoted_by, *ids]))
    items = [
        RankItem(cell.cell_id, _mode_score(cell, mode), not cell.incomplete, bool(cell.demoted_by))
        for cell in cells
    ]
    for index, tier in tiers(items, tie_band).items():
        cells[index].tier = tier
    return [cells[index] for index in rank_order(items)]


def top3(ranked: Sequence[ScoredCell]) -> list[ScoredCell]:
    """First three eligible (not incomplete) cells of a ranked list."""
    return [cell for cell in ranked if not cell.incomplete][:3]


def berlin_band(score: float | None) -> str | None:
    if score is None:
        return None
    if score >= BERLIN_BANDS["above"]:
        return "above"
    return "average" if score >= BERLIN_BANDS["below"] else "below"


def _round(value: float | None, digits: int = 6) -> float | None:
    return None if value is None else round(value, digits)


def to_cells_document(
    result: ScoreResult, area: dict, template: Template, *, mode_default: Mode = "present"
):
    """Build app.contracts.cells.CellsDocument (+ §5.8.7 extra fields)."""
    from app.contracts.cells import Area, Cell, CellsDocument, Layer, Modifier, Risk

    weights = result.weights or {layer.id: layer.weight for layer in template.layers}
    layers = [
        Layer(
            id=layer.id,
            label=layer.label,
            direction=layer.direction,
            weight_default=weights.get(layer.id, layer.weight),
            source=layer.source,
            evidence_id=result.layer_evidence.get(layer.id, f"ev:layer:{layer.id}"),
            why=layer.why,
            unit=layer.unit,
            transform=layer.transform,
            required=layer.required,
            reference=result.layer_reference.get(layer.id),
        )
        for layer in template.layers
    ]
    cells = []
    copies = [replace(cell) for cell in result.cells]
    for cell in rank(copies, mode_default, tie_band=template.tie_band):
        cells.append(
            Cell(
                id=cell.cell_id,
                name=cell.name,
                geometry_ref=cell.cell_id,
                values={key: _round(value) for key, value in cell.values.items()},
                modifiers=[Modifier.model_validate(entry) for entry in cell.modifiers],
                score_present=_round(cell.score_present),
                score_future=_round(cell.score_future),
                raw_values=dict(cell.raw_values),
                risks=[Risk.model_validate(risk) for risk in cell.risks],
                demoted_by=list(cell.demoted_by),
                unknowns=[unknown["text"] for unknown in cell.unknowns],
                parent_name=cell.parent_name,
                score_berlin=_round(cell.score_berlin),
                score_berlin_band=berlin_band(cell.score_berlin),
                confidence=cell.confidence,
                confidence_reasons=list(cell.confidence_reasons),
                tier=cell.tier,
                top3_share=_round(cell.top3_share),
                incomplete=cell.incomplete,
                gap_low=_round(cell.gap_low),
                gap_high=_round(cell.gap_high),
                future_bonus=_round(cell.future_bonus),
                unknown_details=[dict(unknown) for unknown in cell.unknowns],
            )
        )
    area_fields = dict(area)
    area_fields.setdefault("name", area_fields.get("area_id", "area"))
    area_fields.setdefault("plr_count", len(result.cells) + len(result.excluded))
    area_fields.setdefault("cell_count", len(result.cells))
    return CellsDocument(
        area=Area.model_validate(area_fields),
        mode_default=mode_default,
        layers=layers,
        cells=cells,
        template_id=result.template_id,
        excluded_cells=[
            {"id": item["cell_id"], "name": item["name"], "reason": item["reason"]}
            for item in result.excluded
        ],
        values_directional=True,
        tie_band=template.tie_band,
        bonus_cap=template.bonus_cap,
        score_berlin_bands=dict(BERLIN_BANDS),
        notes=list(result.notes),
    )
