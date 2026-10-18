"""Rule-based classify (T1/T2/T3), area matching and plan building (Deep_Research_Spec §5.1)."""

from __future__ import annotations

import re
from typing import Any

from app.application.area_data import LEVEL_ORDER, AreaUnit
from app.scoring.v2 import Template

# Street words start upper-case ("Wiener Straße 12", "Karl-Marx-Allee 3") so leading prose
# ("Was passt an die …") is not captured; the street suffix itself is case-insensitive.
ADDRESS = re.compile(
    r"\b[A-ZÄÖÜ][\wäöüß.-]*(?:\s+[A-ZÄÖÜ][\wäöüß.-]*){0,3}?\s*"
    r"(?i:straße|strasse|str\.?|platz|damm|allee|ufer|weg|ring|chaussee|gasse|markt)\s+\d+[a-zA-Z]?\b"
)
ADDRESS_LOOSE = re.compile(  # lower-case input: at most one word before the street suffix
    r"\b[\wäöüß.-]*\s*(?:straße|strasse|str\.?|platz|damm|allee|ufer|weg|ring|chaussee|gasse|markt)\s+\d+[a-z]?\b",
    re.IGNORECASE,
)
KITA = re.compile(r"\b(kitas?|kindergarten|kindertagesstätte|childcare|day ?care)\b", re.IGNORECASE)
POINT_HINT = re.compile(r"\b(storefront|this space|this shop|ladenlokal)\b", re.IGNORECASE)
MATCH_LEVELS = ("bezirk", "ortsteil")
TEMPLATES = {"location": "coffee_v2", "gap": "kita_gap_v1"}
FUTURE_UNITS = {"location": "dwellings", "gap": "kita_places"}
CLARIFY_AREA = "Which Berlin district or neighbourhood should I research?"

SUB_QUESTIONS = {
    "location": (
        (
            "s1",
            "Planning and policy: Zentrenkonzept, IHK/Handelsverband, gastronomy rules in {area}",
        ),
        (
            "s2",
            "Future demand: residential and office projects in {area} dated after today "
            "(completion dates, unit numbers)",
        ),
        ("s3", "Market: commercial rents, café openings/closures and saturation in {area}"),
    ),
    "gap": (
        ("s1", "Kita development plan (Kitaentwicklungsplanung) of the Bezirk for {area}"),
        ("s2", "Announced Kita openings, expansions and closures in {area} (places, dates)"),
        ("s3", "Housing projects and child population growth in {area} dated after today"),
    ),
}
LAYER_WHY = {
    "residents_block_density": "Residents within walking distance are the base demand.",
    "young_adult_share": "18–45 year olds are the core café audience.",
    "footfall_proxy": "Bakeries, restaurants and takeaways signal existing footfall.",
    "transit": "Departures in the morning and evening peaks bring passing customers.",
    "centre_proximity": "Nearby retail centres provide commercial context; this is not a footfall measurement.",
    "cafe_saturation": "Many cafés per resident means a saturated market.",
    "cafe_crosscheck": "IHK register is only a cross-check for the OSM café count.",
}


def classify(question: str, template_hint: str | None = None) -> dict[str, Any]:
    text = f"{question} {template_hint or ''}"
    address = ADDRESS.search(question) or ADDRESS_LOOSE.search(question)
    if address or POINT_HINT.search(text):
        return {
            "shape": "point",
            "template": "t1_catalog_v1",
            "why": "The question names an address or a specific space → score business "
            "hypotheses for that point (T1).",
            "address": address.group(0).strip() if address else None,
        }
    if KITA.search(text):
        return {
            "shape": "gap",
            "template": TEMPLATES["gap"],
            "why": "The question is about childcare supply → rank PLR cells by Kita gap (T3).",
        }
    return {
        "shape": "location",
        "template": TEMPLATES["location"],
        "why": "The question asks where in an area to open a business → rank PLR cells (T2).",
    }


def _word(name: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w-]){re.escape(name.casefold())}(?![\w-])")


def match_areas(text: str, units: list[AreaUnit]) -> list[AreaUnit]:
    """Bezirk/Ortsteil units named in the text (Bezirksregion as fallback), smallest first."""
    lowered = text.casefold()
    hits = [u for u in units if u.level in MATCH_LEVELS and _word(u.name).search(lowered)]
    if not hits:
        hits = [u for u in units if u.level == "bezirksregion" and _word(u.name).search(lowered)]
    hits.sort(key=lambda u: (LEVEL_ORDER[u.level], -len(u.name), u.area_id))
    return hits


def ambiguous(hits: list[AreaUnit]) -> list[AreaUnit]:
    """Units sharing the best match's name at different levels (e.g. Neukölln Bezirk/Ortsteil)."""
    if not hits:
        return []
    same = [u for u in hits if u.name.casefold() == hits[0].name.casefold()]
    return same if len({u.level for u in same}) > 1 else []


def clarify_question(options: list[AreaUnit]) -> str:
    labels = " or ".join(f"the {u.level.capitalize()} {u.name}" for u in options)
    return (
        f"'{options[0].name}' is ambiguous: do you mean {labels}? Answer e.g. '{options[0].level}'."
    )


def pick_from_answer(answer: str, options: list[AreaUnit]) -> AreaUnit | None:
    value = answer.strip().casefold()
    for unit in options:
        if value == unit.area_id.casefold():
            return unit
    synonyms = {
        "bezirk": ("bezirk", "district", "borough"),
        "ortsteil": ("ortsteil", "neighbourhood", "neighborhood", "locality"),
    }
    for unit in options:
        if any(word in value for word in synonyms.get(unit.level, (unit.level,))):
            return unit
    return None


def candidates(
    selected: AreaUnit, hits: list[AreaUnit], units: list[AreaUnit]
) -> list[dict[str, Any]]:
    by_id = {u.area_id: u for u in units}
    chosen: list[AreaUnit] = [selected, *[u for u in hits if u.area_id != selected.area_id]]
    for unit in list(chosen):
        if unit.level == "bezirk":
            continue
        parent = next(
            (
                u
                for u in by_id.values()
                if u.level == "bezirk" and set(unit.plr_ids) <= set(u.plr_ids)
            ),
            None,
        )
        if parent and parent not in chosen:
            chosen.append(parent)
    return [u.candidate() for u in chosen]


def build_plan(
    shape: str,
    area: AreaUnit,
    area_candidates: list[dict[str, Any]],
    template: Template,
    unmapped: list[str],
) -> dict[str, Any]:
    layers = [
        {
            "id": layer.id,
            "label": layer.label,
            "direction": layer.direction,
            "weight_default": layer.weight,
            "enabled": layer.id not in unmapped and layer.weight > 0,
            "required": layer.required,
            "transform": layer.transform,
            "unit": layer.unit,
            "why": LAYER_WHY.get(layer.id, ""),
            "evidence_id": "",
        }
        for layer in template.layers
    ]
    plan: dict[str, Any] = {
        "area": {
            "area_id": area.area_id,
            "level": area.level,
            "name": area.name,
            "plr_count": len(area.plr_ids),
        },
        "area_candidates": area_candidates,
        "sub_questions": [
            {
                "id": sid,
                "text": text.format(area=area.name),
                "source_class": "web",
                "sources": "Tavily web search",
                "layer_ids": [],
            }
            for sid, text in SUB_QUESTIONS.get(shape, SUB_QUESTIONS["location"])
        ],
        "layers": layers,
        "hypotheses": [],
        "paid_sources": [
            {"name": "hystreet.com", "used_for": "pedestrian footfall counts"},
            {"name": "Google Places", "used_for": "competitor ratings & opening hours"},
        ],
        "template_id": template.id,
    }
    if unmapped:
        labels = {layer.id: layer.label for layer in template.layers}
        plan["note"] = (
            "Not included in scoring: "
            + ", ".join(labels.get(layer_id, layer_id.replace("_", " ")) for layer_id in unmapped)
            + ". Data is unavailable for this run."
        )
    return plan
