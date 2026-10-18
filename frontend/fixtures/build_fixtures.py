#!/usr/bin/env python3
"""Build synthetic Risalite demo fixtures around real PLR boundaries."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

from shapely.geometry import mapping, shape

FIXTURES = Path(__file__).resolve().parent
GEOJSON_URL = "https://gdi.berlin.de/services/wfs/lor_2021"
WFS_URL = (
    "https://gdi.berlin.de/services/wfs/lor_2021?SERVICE=WFS&VERSION=2.0.0"
    "&REQUEST=GetFeature&TYPENAMES=lor_2021:a_lor_plr_2021"
    "&OUTPUTFORMAT=application/json&SORTBY=plr_id"
)
MODELS = {
    "Classifier": "gpt-oss-120b",
    "Planner": "gpt-oss-120b",
    "Data researcher": "tool calls (no LLM)",
    "Web researcher": "Nemotron Lightning",
    "Skeptic": "gpt-oss-120b",
    "Fact-checker": "Tavily extract + fuzzy match ≥0.85",
    "Writer": "Beam",
    "Scorer": "deterministic",
}
KREUZ = {
    "02100101": [0.35, 0.50, 0.30, 0.80, 0.75],
    "02100102": [0.55, 0.40, 0.35, 0.60, 0.45],
    "02100103": [0.45, 0.50, 0.35, 0.55, 0.60],
    "02100104": [0.72, 0.70, 0.45, 0.75, 0.60],
    "02100105": [0.55, 0.55, 0.30, 0.50, 0.45],
    "02100106": [0.65, 0.50, 0.40, 0.45, 0.45],
    "02200207": [0.30, 0.60, 0.25, 0.70, 0.70],
    "02200208": [0.70, 0.60, 0.55, 0.70, 0.60],
    "02200209": [0.65, 0.55, 0.45, 0.55, 0.70],
    "02200210": [0.80, 0.60, 0.50, 0.60, 0.55],
    "02200211": [0.75, 0.65, 0.70, 0.50, 0.80],
    "02200212": [0.85, 0.75, 0.75, 0.55, 0.75],
    "02200213": [0.90, 0.80, 0.60, 0.70, 0.70],
    "02300314": [0.80, 0.85, 0.85, 0.85, 0.65],
    "02300315": [0.95, 0.90, 0.80, 0.75, 0.60],
    "02300316": [0.50, 0.55, 0.25, 0.60, 0.50],
    "02300417": [0.85, 0.80, 0.65, 0.65, 0.55],
    "02300418": [0.90, 0.80, 0.55, 0.55, 0.50],
    "02300419": [0.95, 0.90, 0.90, 0.60, 0.55],
}
NEUKOELLN_CELLS = [
    ("08100101", "Hasenheide", "Schillerpromenade"),
    ("08100102", "Schillerpromenade Nord", "Schillerpromenade"),
    ("08100103", "Schillerpromenade Süd", "Schillerpromenade"),
    ("08100104", "Wartheplatz", "Schillerpromenade"),
    ("08100105", "Silbersteinstraße", "Schillerpromenade"),
    ("08100206", "Flughafenstraße", "Neuköllner Mitte/Zentrum"),
    ("08100207", "Rollberg", "Neuköllner Mitte/Zentrum"),
    ("08100208", "Körnerpark", "Neuköllner Mitte/Zentrum"),
    ("08100209", "Glasower Straße", "Neuköllner Mitte/Zentrum"),
    ("08100310", "Maybachufer", "Reuterstraße"),
    ("08100311", "Reuterplatz", "Reuterstraße"),
    ("08100312", "Weichselplatz", "Reuterstraße"),
    ("08100313", "Bouchéstraße", "Reuterstraße"),
    ("08100314", "Donaustraße", "Reuterstraße"),
    ("08100415", "Ganghoferstraße", "Rixdorf"),
    ("08100416", "Alt-Rixdorf", "Rixdorf"),
    ("08100417", "Braunschweiger Straße", "Rixdorf"),
    ("08100418", "Hertzbergplatz", "Rixdorf"),
    ("08100419", "Treptower Straße Nord", "Rixdorf"),
    ("08100520", "Weiße Siedlung", "Köllnische Heide"),
    ("08100521", "Schulenburgpark", "Köllnische Heide"),
]
LAYERS = [
    {
        "id": "demand_density",
        "label": "Population density",
        "direction": "+",
        "weight_default": 0.30,
        "evidence_id": "e2",
        "why": "Cafés need resident footfall; density is the cheapest proxy.",
        "unit": "residents/ha",
    },
    {
        "id": "young_share",
        "label": "Share aged 18–45",
        "direction": "+",
        "weight_default": 0.15,
        "evidence_id": "e2",
        "why": "IHK report: specialty coffee grows with a young audience.",
        "unit": "share",
    },
    {
        "id": "competition_ihk",
        "label": "Cafés registered at IHK (56114/561141)",
        "direction": "-",
        "weight_default": 0.125,
        "evidence_id": "e3",
        "why": "Registered café inventory; kept separate from OSM.",
        "unit": "cafés",
    },
    {
        "id": "competition_osm",
        "label": "Cafés in OSM (amenity=cafe)",
        "direction": "-",
        "weight_default": 0.125,
        "evidence_id": "e4",
        "why": "Mapped café inventory; kept separate from IHK.",
        "unit": "cafés",
    },
    {
        "id": "transit",
        "label": "Transit access",
        "direction": "+",
        "weight_default": 0.15,
        "evidence_id": "e5",
        "why": "Passing commuters add non-resident footfall.",
    },
    {
        "id": "cost",
        "label": "Land value (cost proxy)",
        "direction": "-",
        "weight_default": 0.15,
        "evidence_id": "e6",
        "why": "High standard land value indicates pressure on ground-floor rents.",
        "unit": "relative land value",
    },
]


def compute_scores(cells: list[dict], layers: list[dict]) -> dict[str, dict[str, float]]:
    """Normalize the layer mix and add future modifiers without risk demotion."""
    means = {}
    for layer in layers:
        values = [
            cell["values"].get(layer["id"])
            for cell in cells
            if cell["values"].get(layer["id"]) is not None
        ]
        means[layer["id"]] = sum(values) / len(values) if values else 0
    raw_scores = []
    for cell in cells:
        raw = 0.0
        for layer in layers:
            value = cell["values"].get(layer["id"])
            if value is None:
                value = means[layer["id"]]
            direction = 1 if layer["direction"] == "+" else -1
            raw += layer["weight_default"] * direction * value
        raw_scores.append(raw)

    minimum = min(raw_scores)
    maximum = max(raw_scores)
    scores: dict[str, dict[str, float]] = {}
    for cell, raw in zip(cells, raw_scores, strict=True):
        present = (raw - minimum) / (maximum - minimum) if maximum > minimum else 0.5
        future_bonus = sum(modifier["bonus"] for modifier in cell["modifiers"])
        scores[cell["id"]] = {
            "present": max(0.0, min(1.0, present)),
            "future": max(0.0, min(1.0, present + future_bonus)),
        }
    return scores


def _rank_cells(cells: list[dict], scores: dict[str, dict[str, float]], mode: str) -> list[dict]:
    base = sorted(
        cells,
        key=lambda cell: (-scores[cell["id"]][mode], str(cell["id"])),
    )
    rank_keys = []
    for index, cell in enumerate(base):
        if not cell.get("demoted_by"):
            rank_keys.append(index)
            continue
        next_rank = index + 1
        while next_rank < len(base) and base[next_rank].get("demoted_by"):
            next_rank += 1
        rank_keys.append(next_rank + 0.5 if next_rank < len(base) else index)
    return [
        entry
        for _, _, entry in sorted(
            (rank_keys[index], index, cell) for index, cell in enumerate(base)
        )
    ]


def _fixture_cells() -> list[dict]:
    """Use the checked-in synthetic PLR attributes as the generator's input."""
    data = json.loads((FIXTURES / "cells.json").read_text(encoding="utf-8"))
    return [
        {"id": cell["id"], "name": cell["name"], "bzr": cell.get("parent_name", "")}
        for cell in data["cells"]
    ]


def _evidence() -> list[dict]:
    entries = [
        (
            "Official PLR geometries (lor_2021)",
            WFS_URL,
            "19 Planungsräume in Kreuzberg",
            "wfs",
            "official",
            "high",
            "verified",
        ),
        (
            "Population density & age groups (ua_einwohnerdichte_2025)",
            "https://gdi.berlin.de/services/wfs/ua_einwohnerdichte_2025",
            "EW_DICHTE (residents/ha) and EW_18_45 share, aggregated to PLR",
            "wfs",
            "official",
            "high",
            "verified",
        ),
        (
            "IHK Berlin registered businesses (CSV)",
            "https://media.githubusercontent.com/media/IHKBerlin/IHKBerlin_Gewerbedaten/75957615265716ec8549d04d63ed76bd6d6c547a/data/IHKBerlin_Gewerbedaten.csv",
            "Count of registered café businesses (56114/561141), aggregated to PLR",
            "csv",
            "official",
            "high",
            "partial",
        ),
        (
            "OpenStreetMap café inventory (Geofabrik Berlin PBF)",
            "https://download.geofabrik.de/europe/germany/berlin-261017.osm.pbf",
            "amenity=cafe features in the Berlin PBF extract, aggregated to PLR",
            "osm",
            "community",
            "medium",
            "partial",
        ),
        (
            "Undisturbed public transport access (oepnv_ungestoert)",
            "https://gdi.berlin.de/services/wfs/oepnv_ungestoert",
            "share of area within 300 m of a stop",
            "wfs",
            "official",
            "high",
            "verified",
        ),
        (
            "Standard land values 2026 (brw2026)",
            "https://gdi.berlin.de/services/wfs/brw2026",
            "BRW €/m² (zone mean per PLR) — cost proxy",
            "wfs",
            "official",
            "high",
            "verified",
        ),
        (
            "IHK Berlin — Gastronomie-Report",
            "https://www.ihk.de/berlin/branchen/gastronomie/gastgewerbe-report",
            "„…Spezialitätenkaffee wächst vor allem in dicht bewohnten Kiezen mit jungem Publikum…“",
            "web",
            "institutional",
            "medium",
            "partial",
        ),
        (
            "Zentren- und Einzelhandelskonzept Friedrichshain-Kreuzberg",
            "https://www.berlin.de/ba-friedrichshain-kreuzberg/politik-und-verwaltung/aemter/stadtentwicklungsamt/zentrenkonzept.pdf",
            "„…Stärkung der Nahversorgungszentren Moritzplatz und Südstern…“",
            "web",
            "institutional",
            "medium",
            "partial",
        ),
        (
            "News: office campus at Moritzplatz",
            "https://www.example-berlin-news.de/kreuzberg/buero-campus-moritzplatz",
            "„…rund 1.200 Arbeitsplätze, Fertigstellung 2027…“",
            "web",
            "press",
            "medium",
            "partial",
        ),
        (
            "News: housing project Köpenicker Straße",
            "https://www.example-berlin-news.de/kreuzberg/wohnungen-koepenicker-strasse",
            "„…300 Wohnungen sollen bis 2028 bezugsfertig sein…“",
            "web",
            "press",
            "medium",
            "partial",
        ),
        (
            "Commercial rent report Berlin (retail, ground floor)",
            "https://www.example-immo-report.de/berlin/gewerbemieten-2026",
            "„…Erdgeschossmieten im Wrangelkiez +18 % gegenüber Vorjahr…“",
            "web",
            "press",
            "medium",
            "partial",
        ),
    ]
    ledger = [
        {
            "evidence_id": f"e{index}",
            "title": title,
            "url": url,
            "quote": quote,
            "method": method,
            "tier": tier,
            "confidence": confidence,
            "status": status,
        }
        for index, (title, url, quote, method, tier, confidence, status) in enumerate(entries, 1)
    ]
    ledger.extend(
        [
            {
                "evidence_id": "e12",
                "title": "Rejected footfall claim (mockup fact-checker)",
                "url": "https://www.example-food-blog.de/kreuzberg-hotspots",
                "quote": "Quote not found on re-fetch (best fuzzy match 0.38 < 0.85) → dropped, planner re-planned web search for footfall",
                "method": "web",
                "tier": "press",
                "confidence": "low",
                "status": "unsupported",
                "claim": "Footfall on Oranienstraße rose 30 % in 2026",
                "reason": "Quote not found on re-fetch (best fuzzy match 0.38 < 0.85)",
            },
            {
                "evidence_id": "e13",
                "title": "Address geocode (adressen_berlin)",
                "url": "https://gdi.berlin.de/services/wfs/adressen_berlin?CQL_FILTER=str_name%3D%27Wiener%20Stra%C3%9Fe%27%20AND%20hnr%3D12",
                "quote": "Wiener Straße 12, 10999 Berlin → PLR 02300315 Lausitzer Platz",
                "method": "wfs",
                "tier": "official",
                "confidence": "high",
                "status": "verified",
            },
            {
                "evidence_id": "e14",
                "title": "Competitors within 500 m (gewerbedaten, by NACE)",
                "url": "https://gdi.berlin.de/services/wfs/gewerbedaten?BBOX=...500m",
                "quote": "NACE 10.71 bakeries: 4 · 56.30 bars: 11 · 47.11 grocery: 3",
                "method": "wfs",
                "tier": "official",
                "confidence": "high",
                "status": "partial",
            },
            {
                "evidence_id": "e15",
                "title": "OSM shops & amenities within 500 m",
                "url": "https://download.geofabrik.de/europe/germany/berlin-261017.osm.pbf",
                "quote": "shop=bakery 3 · amenity=bar 13 · shop=organic 1",
                "method": "osm",
                "tier": "community",
                "confidence": "medium",
                "status": "partial",
            },
            {
                "evidence_id": "e16",
                "title": "Residential location quality (wohnlagenadr2026)",
                "url": "https://gdi.berlin.de/services/wfs/wohnlagenadr2026",
                "quote": "Wohnlage: mittel",
                "method": "wfs",
                "tier": "official",
                "confidence": "high",
                "status": "verified",
            },
            {
                "evidence_id": "e17",
                "title": "Standard land value (brw2026)",
                "url": "https://gdi.berlin.de/services/wfs/brw2026",
                "quote": "BRW zone 2,900 €/m²",
                "method": "wfs",
                "tier": "official",
                "confidence": "high",
                "status": "verified",
            },
            {
                "evidence_id": "e18",
                "title": "Neighbourhood bakery closes (local press)",
                "url": "https://www.example-berlin-news.de/kreuzberg/baeckerei-schliesst",
                "quote": "„…die letzte Handwerksbäckerei am Lausitzer Platz hat zum Jahresende geschlossen…“",
                "method": "web",
                "tier": "press",
                "confidence": "medium",
                "status": "partial",
            },
            {
                "evidence_id": "e19",
                "title": "Bezirk: Ausschank & Außengastronomie rules",
                "url": "https://www.berlin.de/ba-friedrichshain-kreuzberg/politik-und-verwaltung/aemter/ordnungsamt/gaststaetten/",
                "quote": "„…Sondernutzungserlaubnis für Außengastronomie erforderlich…“",
                "method": "web",
                "tier": "official",
                "confidence": "high",
                "status": "partial",
            },
            {
                "evidence_id": "e20",
                "title": "Competitor menus & prices (3 natural-wine bars)",
                "url": "https://www.example-bar.de/karte",
                "quote": "glass €7–9, open Wed–Sun from 18:00",
                "method": "web",
                "tier": "weak",
                "confidence": "low",
                "status": "partial",
            },
            {
                "evidence_id": "e21",
                "title": "Population density & age groups (ua_einwohnerdichte_2025)",
                "url": "https://gdi.berlin.de/services/wfs/ua_einwohnerdichte_2025",
                "quote": "Lausitzer Platz: 18–45 share in top decile of the district",
                "method": "wfs",
                "tier": "official",
                "confidence": "high",
                "status": "verified",
            },
            {
                "evidence_id": "e22",
                "title": "Rejected rent-doubling claim (mockup fact-checker)",
                "url": "https://www.example-forum.de/thread/123",
                "quote": "Unsupported: forum post, no figure on re-fetch",
                "method": "web",
                "tier": "community",
                "confidence": "low",
                "status": "unsupported",
                "claim": "Rents on Wiener Straße doubled since 2023",
                "reason": "Unsupported: forum post, no figure on re-fetch",
            },
        ]
    )
    for entry in ledger:
        entry["synthetic"] = True
    return ledger


def _gap_hash(value: str) -> float:
    """Match the mockup's 32-bit JavaScript FNV-1a hash for ASCII identifiers."""
    value_hash = 2166136261
    for character in value:
        value_hash = ((value_hash ^ ord(character)) * 16777619) & 0xFFFFFFFF
    return (value_hash % 10000) / 10000


def _gap_layers() -> list[dict]:
    return [
        {
            "id": "demand",
            "label": "Children under 6",
            "direction": "+",
            "weight_default": 0.40,
            "evidence_id": "e2",
            "why": "Demand: every child under 6 is a potential place.",
            "unit": "children",
        },
        {
            "id": "supply",
            "label": "Kita places per child",
            "direction": "-",
            "weight_default": 0.35,
            "evidence_id": "e3",
            "why": "Supply: fewer places per child → larger gap.",
            "unit": "places per child",
        },
        {
            "id": "transit",
            "label": "Transit access",
            "direction": "-",
            "weight_default": 0.10,
            "evidence_id": "e4",
            "why": "Poor access makes existing places further out effectively unavailable.",
            "unit": "share within 300 m",
        },
        {
            "id": "status",
            "label": "Low social status (MSS)",
            "direction": "+",
            "weight_default": 0.15,
            "evidence_id": "e5",
            "why": "Equity: Bezirk planning prioritises low-status areas.",
            "unit": "status index",
        },
    ]


def _gap_evidence() -> list[dict]:
    geometry_url = (
        f"{WFS_URL}&SRSNAME=EPSG%3A4326&CQL_FILTER="
        "plr_id%20IN%20(%27"
        + "%27%2C%27".join(cell[0] for cell in NEUKOELLN_CELLS)
        + "%27)"
    )
    entries = [
        (
            "Official PLR geometries (lor_2021)",
            geometry_url,
            "21 Planungsräume in Neukölln",
            "wfs",
            "official",
            "high",
            "verified",
        ),
        (
            "Population by age group (ua_einwohnerdichte_2025)",
            "https://gdi.berlin.de/services/wfs/ua_einwohnerdichte_2025?service=WFS&version=2.0.0&request=GetFeature&typeNames=ua_einwohnerdichte_2025",
            "EW_U6 — residents under 6, per block, aggregated to PLR",
            "wfs",
            "official",
            "high",
            "verified",
        ),
        (
            "Kita locations & approved places (kita)",
            "https://gdi.berlin.de/services/wfs/kita?service=WFS&version=2.0.0&request=GetFeature&typeNames=kita",
            "genehmigte Plätze per facility, aggregated to PLR",
            "wfs",
            "official",
            "high",
            "verified",
        ),
        (
            "Undisturbed public transport access (oepnv_ungestoert)",
            "https://gdi.berlin.de/services/wfs/oepnv_ungestoert?service=WFS&version=2.0.0&request=GetFeature&typeNames=oepnv_ungestoert",
            "share of area within 300 m of a stop",
            "wfs",
            "official",
            "high",
            "verified",
        ),
        (
            "Social monitoring 2025 (mss_2025)",
            "https://gdi.berlin.de/services/wfs/mss_2025?service=WFS&version=2.0.0&request=GetFeature&typeNames=mss_2025",
            "Status-Index per PLR",
            "wfs",
            "official",
            "high",
            "verified",
        ),
        (
            "Kita-Entwicklungsplanung Bezirk Neukölln",
            "https://www.berlin.de/ba-neukoelln/politik-und-verwaltung/aemter/jugendamt/kita-entwicklungsplanung.pdf",
            "„…in den Planungsräumen nördlich der Ringbahn besteht ein rechnerisches Defizit an Betreuungsplätzen…“",
            "pdf",
            "institutional",
            "medium",
            "partial",
        ),
        (
            "Planned housing — StEP Wohnen 2040 (step_wo_2040)",
            "https://gdi.berlin.de/services/wfs/step_wo_2040?service=WFS&version=2.0.0&request=GetFeature&typeNames=step_wo_2040",
            "Wohnungsbaupotenzial ca. 420 WE, Realisierung bis 2029",
            "web",
            "official",
            "high",
            "partial",
        ),
        (
            "Local news: Kita operator closes two sites",
            "https://www.example-berlin-news.de/neukoelln/kita-schliessung",
            "„…zwei Einrichtungen mit zusammen 85 Plätzen schließen zum Sommer 2027…“",
            "web",
            "press",
            "medium",
            "partial",
        ),
    ]
    ledger = [
        {
            "evidence_id": f"e{index}",
            "title": title,
            "url": url,
            "quote": quote,
            "method": method,
            "tier": tier,
            "confidence": confidence,
            "status": status,
            "synthetic": True,
        }
        for index, (title, url, quote, method, tier, confidence, status) in enumerate(entries, 1)
    ]
    ledger.append(
        {
            "evidence_id": "e9",
            "title": "Rejected city-wide Kita coverage claim (mockup fact-checker)",
            "url": "https://www.example-parenting-blog.de/kita-ranking",
            "quote": "Quote not found on re-fetch (best fuzzy match 0.41 < 0.85)",
            "method": "web",
            "tier": "press",
            "confidence": "low",
            "status": "unsupported",
            "claim": "“Neukölln has the lowest Kita coverage in Berlin”",
            "reason": "Quote not found on re-fetch (best fuzzy match 0.41 < 0.85)",
            "synthetic": True,
        }
    )
    return ledger


def _gap_cells() -> list[dict]:
    layers = _gap_layers()
    cells = []
    for cell_id, name, parent_name in NEUKOELLN_CELLS:
        values = {
            layer["id"]: math.floor(
                (0.15 + 0.8 * _gap_hash(f"{cell_id}{layer['id']}gap")) * 100 + 0.5
            )
            / 100
            for layer in layers
        }
        modifiers = []
        if cell_id == "08100312":
            modifiers.append(
                {
                    "type": "future",
                    "label": "Planned housing: ~420 units by 2029",
                    "quote": "Wohnungsbaupotenzial ca. 420 WE, Realisierung bis 2029",
                    "evidence_id": "e7",
                    "bonus": 0.10,
                    "magnitude": {"units": 420},
                }
            )
        if cell_id == "08100207":
            modifiers.append(
                {
                    "type": "future",
                    "label": "Two Kitas (85 places) closing 2027",
                    "quote": "…zwei Einrichtungen mit zusammen 85 Plätzen schließen zum Sommer 2027…",
                    "evidence_id": "e8",
                    "bonus": 0.10,
                    "magnitude": {"places": 85},
                }
            )
        cells.append(
            {
                "id": cell_id,
                "name": name,
                "geometry_ref": cell_id,
                "parent_name": parent_name,
                "values": values,
                "modifiers": modifiers,
                "risks": [],
                "demoted_by": [],
                "unknowns": [],
            }
        )

    scores = compute_scores(cells, layers)
    for cell in cells:
        cell["score_present"] = scores[cell["id"]]["present"]
        cell["score_future"] = scores[cell["id"]]["future"]
    return cells


def _gap_trace() -> list[dict]:
    raw_events = [
        ("Classifier", "classify", "shape=gap", "Kita shortage in Neukölln → compare PLR demand and supply.", 820, 1.1, []),
        ("Planner", "plan", "4 layers · 5 sub-questions", "Use childcare demand, supply, transit and social-status evidence.", 1760, 2.8, []),
        ("Data researcher", "tool:wfs", "PLR geometries (lor_2021)", "Load the 21 official Neukölln planning-room boundaries.", 0, 0.9, ["e1"]),
        ("Data researcher", "tool:wfs", "Population by age group (ua_einwohnerdichte_2025)", "Aggregate children under six to the PLR.", 0, 1.2, ["e2"]),
        ("Data researcher", "tool:wfs", "Kita locations & approved places (kita)", "Aggregate approved Kita places to the PLR.", 0, 1.4, ["e3"]),
        ("Data researcher", "tool:wfs", "Transit access and MSS social-status index", "Compare access and social need across cells.", 0, 1.5, ["e4", "e5"]),
        ("Web researcher", "tavily:search", "Bezirk Neukölln Kita-Entwicklungsplanung", "Check the district's planning evidence for northern PLR deficits.", 1480, 2.4, ["e6"]),
        ("Web researcher", "tavily:search", "StEP Wohnen 2040 and Kita closure reporting", "Find prospective housing and closures with source quotations.", 1510, 2.6, ["e7", "e8"]),
        ("Fact-checker", "reject", "“Neukölln has the lowest Kita coverage in Berlin”", "Quote not found on re-fetch (best fuzzy match 0.41 < 0.85)", 0, 1.2, ["e9"]),
        ("Planner", "replan", "Drop city-wide coverage ranking; retain PLR supply-demand evidence", "Replan without the unverifiable city-wide claim.", 610, 1.0, ["e9", "e2", "e3"]),
        ("Skeptic", "challenge", "Review default top-3 Kita gap cells", "Check directionality, future claims and data limitations.", 1920, 3.5, ["e2", "e3", "e6", "e7", "e8"]),
        ("Writer", "write", "Gap summary, top cells and citations", "Write the report using only the gap evidence ledger.", 2900, 4.4, ["e1", "e2", "e3", "e6", "e7", "e8", "e9"]),
    ]
    events = [
        _event_payload(seq, "mock-t3", role, event_type, text, why, tokens, seconds, evidence_ids)
        for seq, (role, event_type, text, why, tokens, seconds, evidence_ids) in enumerate(raw_events)
    ]
    events.append(_event_payload(len(events), "mock-t3", "Writer", "done", "Run complete", "", 0, 0.0, []))
    return events


def _gap_plan_document(evidence: list[dict], cells: list[dict]) -> dict:
    return {
        "synthetic": True,
        "run_id": "mock-t3",
        "status": "awaiting_approval",
        "question": "Where are Kitas missing in Neukölln?",
        "classification": {
            "shape": "gap",
            "why": "The question asks where childcare is missing → compute a PLR supply-vs-demand gap score.",
            "area": "Neukölln",
        },
        "plan": {
            "area": {
                "area_id": "ortsteil:0801",
                "level": "ortsteil",
                "name": "Neukölln",
                "plr_count": len(cells),
            },
            "area_candidates": [
                {
                    "area_id": "ortsteil:0801",
                    "level": "ortsteil",
                    "name": "Neukölln",
                    "bezirk": "Neukölln",
                },
                {
                    "area_id": "bezirk:08",
                    "level": "bezirk",
                    "name": "Neukölln",
                    "bezirk": "Neukölln",
                },
            ],
            "area_cells": len(cells),
            "sub_questions": [
                {
                    "id": "s1",
                    "text": "How many children under 6 live in each PLR?",
                    "source_class": "official geodata",
                    "sources": "ua_einwohnerdichte_2025",
                    "layer_ids": ["demand"],
                },
                {
                    "id": "s2",
                    "text": "How many approved Kita places exist per PLR?",
                    "source_class": "official geodata",
                    "sources": "kita",
                    "layer_ids": ["supply"],
                },
                {
                    "id": "s3",
                    "text": "What does the Bezirk Kita plan say about deficits?",
                    "source_class": "web (Tavily)",
                    "sources": "berlin.de, *.berlin.de",
                    "layer_ids": [],
                },
                {
                    "id": "s4",
                    "text": "Which closures or openings are announced after today?",
                    "source_class": "web (Tavily)",
                    "sources": "berlin.de, local press",
                    "layer_ids": [],
                },
                {
                    "id": "s5",
                    "text": "Where is new housing planned?",
                    "source_class": "official geodata",
                    "sources": "step_wo_2040",
                    "layer_ids": [],
                },
            ],
            "layers": _gap_layers(),
            "hypotheses": [],
            "paid_sources": [],
            "evidence_titles": {entry["evidence_id"]: entry["title"] for entry in evidence},
            "synthetic": True,
        },
    }


def _gap_result_document(
    evidence: list[dict], cells: list[dict], events: list[dict]
) -> dict:
    scores = compute_scores(cells, _gap_layers())
    ranked = _rank_cells(cells, scores, "present")
    top = ranked[:3]
    return {
        "synthetic": True,
        "run_id": "mock-t3",
        "status": "done",
        "question": "Where are Kitas missing in Neukölln?",
        "role": "urban planner",
        "shape": "gap",
        "report_md": (
            f"Across {len(cells)} Planungsräume in Neukölln [1], the largest Kita gap is in "
            f"**{top[0]['name']}** [2] [3], followed by **{top[1]['name']}** [2] [3] and "
            f"**{top[2]['name']}** [2] [3]. These cells combine many children under six with "
            "relatively few approved places per child. The district's Kita planning reports a "
            "deficit in northern planning areas [6]. Announced changes include ~420 planned "
            "housing units at Weichselplatz [7] and two Kita closures affecting 85 places in "
            "Rollberg [8] in Present + future mode. The claim that Neukölln has the lowest Kita "
            "coverage in Berlin could not be verified and was dropped [9]."
        ),
        "models": MODELS,
        "self_grade": [
            {"ok": True, "text": "The report ranks all 21 Neukölln PLR cells using the approved layer mix."},
            {"ok": True, "text": "Future developments and the two Kita closures cite their source entries."},
            {"ok": False, "text": "A city-wide Kita coverage ranking could not be verified and was rejected."},
        ],
    }


def _gap_findings() -> list[dict]:
    return [
        {
            "finding_id": "f_neukoelln_plan_deficit",
            "claim": "The district Kita plan identifies a calculated childcare-place deficit in PLR north of the Ringbahn.",
            "quote": "„…in den Planungsräumen nördlich der Ringbahn besteht ein rechnerisches Defizit an Betreuungsplätzen…“",
            "evidence_ids": ["e6"],
            "claim_type": "demand",
            "localization": "Northern Neukölln PLR",
            "agent": "web_researcher",
            "status": "accepted",
            "verification": "partial",
            "synthetic": True,
        },
        {
            "finding_id": "f_neukoelln_coverage_rejected",
            "claim": "“Neukölln has the lowest Kita coverage in Berlin”",
            "quote": "Quote not found on re-fetch (best fuzzy match 0.41 < 0.85)",
            "evidence_ids": ["e9"],
            "claim_type": "unsupported",
            "localization": "Neukölln",
            "agent": "fact_checker",
            "status": "rejected",
            "verification": "unsupported",
            "reject_reason": "Quote not found on re-fetch (best fuzzy match 0.41 < 0.85)",
            "synthetic": True,
        },
    ]


def _normalized_cells(source_cells: list[dict]) -> list[dict]:
    cells = []
    for index, cell in enumerate(source_cells):
        demand, young, competition, transit, cost = KREUZ[cell["id"]]
        ihk = round(min(1.0, competition + ((index % 3) - 1) * 0.02), 2)
        osm = round(min(1.0, max(0.0, competition - ((index % 4) - 1.5) * 0.02)), 2)
        missing_osm = cell["id"] == "02200209"
        modifiers = []
        risks = []
        unknowns = []
        demoted_by = []
        if cell["id"] == "02100104":
            modifiers.append(
                {
                    "type": "future",
                    "label": "Office campus: ~1,200 workplaces by 2027",
                    "quote": "…rund 1.200 Arbeitsplätze, Fertigstellung 2027…",
                    "evidence_id": "e9",
                    "bonus": 0.10,
                    "magnitude": {"jobs": 1200},
                }
            )
        if cell["id"] == "02300316":
            modifiers.append(
                {
                    "type": "future",
                    "label": "Planned housing: 300 units at Köpenicker Straße",
                    "quote": "…300 Wohnungen sollen bis 2028 bezugsfertig sein…",
                    "evidence_id": "e10",
                    "bonus": 0.10,
                    "magnitude": {"units": 300},
                }
            )
        if cell["id"] == "02300419":
            risks.append(
                {
                    "severity": "high",
                    "text": "Ground-floor rents rising fast (+18 % in Wrangelkiez)",
                    "quote": "…Erdgeschossmieten im Wrangelkiez +18 % gegenüber Vorjahr…",
                    "evidence_id": "e11",
                }
            )
            demoted_by.append("f_rent_wrangelkiez")
        if cell["id"] == "02200213":
            unknowns.append(
                "Actual footfall on Graefestraße — hystreet slot skipped (no key). Validate: 2 × 30-min manual counts."
            )
        if cell["id"] == "02300418":
            unknowns.append(
                "Vacancy of ground-floor units unknown. Validate: walk Reichenberger Str. / check listings."
            )
        values = {
            "demand_density": demand,
            "young_share": young,
            "competition_ihk": ihk,
            "competition_osm": None if missing_osm else osm,
            "transit": transit,
            "cost": cost,
        }
        raw_values = {
            "demand_density": round(40 + demand * 180, 1),
            "young_share": round(0.30 + young * 0.35, 3),
            "competition_ihk": 2 + (index * 7 % 24),
            "competition_osm": None if missing_osm else 2 + (index * 11 % 24),
            "transit": round(0.15 + transit * 0.75, 3),
            "cost": round(1800 + cost * 3200),
        }
        cells.append(
            {
                "id": cell["id"],
                "name": cell["name"],
                "geometry_ref": cell["id"],
                "parent_name": cell["bzr"],
                "values": values,
                "raw_values": raw_values,
                "modifiers": modifiers,
                "risks": risks,
                "demoted_by": demoted_by,
                "unknowns": unknowns,
            }
        )

    scores = compute_scores(cells, LAYERS)
    for cell in cells:
        cell["score_present"] = scores[cell["id"]]["present"]
        cell["score_future"] = scores[cell["id"]]["future"]
    return cells


def _event_payload(
    seq: int,
    run_id: str,
    role: str,
    event_type: str,
    text: str,
    why: str,
    tokens: int,
    seconds: float,
    evidence_ids: list[str],
) -> dict:
    kinds = {
        "skip": "status",
        "tool:wfs": "layer",
        "tool:csv": "layer",
        "tool:osm": "layer",
        "tavily:search": "search",
        "tavily:extract": "extract",
        "challenge": "review",
        "reject": "finding_rejected",
        "replan": "plan",
        "verify": "fact_check",
        "done": "status",
    }
    return {
        "seq": seq,
        "run_id": run_id,
        "ts": "2026-01-01T00:00:00Z",
        "type": kinds.get(event_type, event_type),
        "agent": role,
        "why": why,
        "detail": {
            "summary": text,
            "evidence_ids": evidence_ids,
            **({"status": "done"} if event_type == "done" else {}),
        },
        "model": MODELS[role],
        "tokens_in": tokens,
        "tokens_out": 0,
        "seconds": seconds,
        "synthetic": True,
    }


def _geojson_source(cache_path: Path | None, ids: set[str]) -> dict:
    if cache_path is not None:
        collection = json.loads(cache_path.read_text(encoding="utf-8"))
    else:
        params = {
            "SERVICE": "WFS",
            "VERSION": "2.0.0",
            "REQUEST": "GetFeature",
            "TYPENAMES": "lor_2021:a_lor_plr_2021",
            "OUTPUTFORMAT": "application/json",
            "SRSNAME": "EPSG:4326",
            "CQL_FILTER": "plr_id IN (" + ",".join(f"'{area_id}'" for area_id in sorted(ids)) + ")",
        }
        request_url = f"{GEOJSON_URL}?{urlencode(params)}"
        with urlopen(request_url, timeout=60) as response:
            collection = json.loads(response.read())
    if collection.get("type") != "FeatureCollection":
        raise ValueError("WFS input is not a GeoJSON FeatureCollection")
    return collection


def _round_coordinates(value):
    if isinstance(value, list):
        return [_round_coordinates(item) for item in value]
    return round(value, 6) if isinstance(value, float) else value


def build_geojson(collection: dict, cells: list[dict]) -> dict:
    wanted = {cell["id"]: cell["name"] for cell in cells}
    features = []
    for feature in collection["features"]:
        properties = feature.get("properties") or {}
        area_id = str(properties.get("plr_id", feature.get("id", "")).split(".")[-1])
        if area_id not in wanted:
            continue
        geometry = shape(feature["geometry"]).simplify(0.00005, preserve_topology=True)
        geometry_mapping = mapping(geometry)
        geometry_mapping["coordinates"] = _round_coordinates(geometry_mapping["coordinates"])
        features.append(
            {
                "type": "Feature",
                "id": area_id,
                "properties": {"id": area_id, "name": wanted[area_id]},
                "geometry": geometry_mapping,
            }
        )
    features.sort(key=lambda feature: feature["id"])
    missing = set(wanted) - {feature["id"] for feature in features}
    if missing:
        raise ValueError(f"WFS input is missing PLR features: {', '.join(sorted(missing))}")
    return {"type": "FeatureCollection", "synthetic": True, "features": features}


def _research_plan(shape_name: str, evidence: list[dict], cells: list[dict]) -> dict:
    evidence_by_id = {entry["evidence_id"]: entry for entry in evidence}
    if shape_name == "location":
        sub_questions = [
            (
                "Where do many (young) residents live?",
                "official geodata",
                "ua_einwohnerdichte_2025",
                ["demand_density", "young_share"],
            ),
            (
                "Where are cafés already concentrated?",
                "official geodata + OSM",
                "IHK 56114/561141 CSV, Geofabrik amenity=cafe",
                ["competition_ihk", "competition_osm"],
            ),
            (
                "Which cells have good transit access?",
                "official geodata",
                "oepnv_ungestoert",
                ["transit"],
            ),
            (
                "What does it cost to rent there?",
                "official geodata + web",
                "brw2026, rent reports",
                ["cost"],
            ),
            (
                "What do IHK / the Bezirk say about gastronomy?",
                "web (Tavily)",
                "ihk.de, berlin.de",
                [],
            ),
            (
                "Which residential/office projects are coming after today?",
                "web (Tavily)",
                "berlin.de, local press",
                [],
            ),
        ]
        layers = LAYERS
        paid = [
            {"name": "hystreet.com", "used_for": "pedestrian footfall counts"},
            {"name": "Google Places", "used_for": "competitor ratings & opening hours"},
        ]
        hypotheses = []
    else:
        sub_questions = [
            (
                "Where exactly is the space, and which PLR is it in?",
                "official geodata",
                "adressen_berlin, lor_2021",
                [],
            ),
            (
                "Which competitors are within 500 m, per hypothesis?",
                "official geodata + OSM",
                "gewerbedaten (NACE), Geofabrik",
                [],
            ),
            (
                "What is the land value / Wohnlage?",
                "official geodata",
                "brw2026, wohnlagenadr2026",
                [],
            ),
            (
                "FOR / AGAINST: Craft bakery-café",
                "web (Tavily)",
                "competitor sites, menus, IHK, local press",
                [],
            ),
            (
                "FOR / AGAINST: Natural-wine bar",
                "web (Tavily)",
                "competitor sites, menus, IHK, local press",
                [],
            ),
            (
                "FOR / AGAINST: Zero-waste grocery",
                "web (Tavily)",
                "competitor sites, menus, IHK, local press",
                [],
            ),
            ("Which rules apply (Außengastronomie, hours)?", "web (Tavily)", "berlin.de", []),
        ]
        layers = []
        paid = []
        hypotheses = _point_hypotheses()

    return {
        "sub_questions": [
            {
                "id": f"s{index}",
                "text": text,
                "source_class": source_class,
                "sources": sources,
                "layer_ids": layer_ids,
            }
            for index, (text, source_class, sources, layer_ids) in enumerate(sub_questions[:3], 1)
        ],
        "layers": layers,
        "hypotheses": hypotheses,
        "paid_sources": paid,
        "area": (
            {
                "area_id": "ortsteil:0202",
                "level": "ortsteil",
                "name": "Kreuzberg",
                "plr_count": len(cells),
            }
            if shape_name == "location"
            else None
        ),
        "area_candidates": (
            [
                {
                    "area_id": "ortsteil:0202",
                    "level": "ortsteil",
                    "name": "Kreuzberg",
                    "bezirk": "Friedrichshain-Kreuzberg",
                },
                {
                    "area_id": "bezirk:02",
                    "level": "bezirk",
                    "name": "Friedrichshain-Kreuzberg",
                    "bezirk": "Friedrichshain-Kreuzberg",
                },
            ]
            if shape_name == "location"
            else []
        ),
        "area_cells": len(cells) if shape_name == "location" else None,
        "synthetic": True,
        "evidence_titles": {
            entry["evidence_id"]: entry["title"] for entry in evidence_by_id.values()
        },
    }


def _point_hypotheses() -> list[dict]:
    dimensions = [
        (
            "Demand",
            [8, 7, 5],
            ["Dense, young PLR [e21]", "Strong evening footfall", "Medium demand signal"],
            ["e21"],
        ),
        (
            "Competition gap",
            [9, 3, 7],
            [
                "Last craft bakery closed [e18]; only 4 registered bakeries [e14]",
                "11 NACE bars nearby [e14]",
                "1 organic shop nearby [e15]",
            ],
            ["e14", "e15", "e18"],
        ),
        (
            "Operator fit",
            [10, 4, 3],
            [
                "6 yrs bakery experience — direct fit",
                "No bar experience in profile",
                "No retail experience",
            ],
            [],
        ),
        (
            "Space fit",
            [7, 8, 6],
            ["~70 m² fits a counter + oven; ventilation unknown", "Layout suits a bar", "Space OK"],
            [],
        ),
        (
            "Cost",
            [6, 6, 5],
            ["BRW 2,900 €/m² [e17] — mid-range", "Mid rents", "Thin margins [e17]"],
            ["e17"],
        ),
        (
            "Regulation / risk",
            [7, 4, 8],
            [
                "Standard Gaststätte rules [e19]",
                "Late-night noise complaints risk",
                "Low regulatory burden",
            ],
            ["e19"],
        ),
    ]
    names = ["Craft bakery-café", "Natural-wine bar", "Zero-waste grocery"]
    scores = [7.8, 5.9, 5.2]
    claim_data = [
        {
            "for": [
                ("Last craft bakery on Lausitzer Platz closed", "e18"),
                ("Few bakeries within 500 m (4)", "e14"),
                ("Operator has 6 yrs bakery experience", None),
            ],
            "against": [
                ("3 OSM bakeries still within 500 m", "e15"),
                ("Morning-only demand peak", None),
            ],
            "unknowns": [
                (
                    "Oven ventilation permitted in this building?",
                    "Ask landlord for Abluft spec; Bauaufsicht check",
                )
            ],
        },
        {
            "for": [
                ("Young evening audience", "e21"),
                ("Competitor prices €7–9 / glass sustain margins", "e20"),
            ],
            "against": [
                ("High bar saturation (11 + 13 OSM)", "e14"),
                ("Operator lacks bar experience", None),
                ("Weak-tier menu evidence cannot carry a recommendation alone", "e20"),
            ],
            "unknowns": [("Licensing hours for this street", "Ordnungsamt enquiry")],
        },
        {
            "for": [("Only one organic shop nearby", "e15")],
            "against": [("Low operator fit", None), ("Margins thin at this rent level", "e17")],
            "unknowns": [
                ("Willingness to pay for zero-waste in this Kiez", "10 short customer interviews")
            ],
        },
    ]
    result = []
    for index, name in enumerate(names):
        result.append(
            {
                "id": f"h{index + 1}",
                "name": name,
                "score": scores[index],
                "rank_initial": index + 1,
                "rank_final": index + 1,
                "dimensions": [
                    {
                        "name": dimension_name,
                        "score": values[index],
                        "why": explanations[index],
                        "evidence_ids": evidence_ids,
                    }
                    for dimension_name, values, explanations, evidence_ids in dimensions
                ],
                "for": [
                    {"text": text, "evidence_id": evidence_id}
                    for text, evidence_id in claim_data[index]["for"]
                ],
                "against": [
                    {"text": text, "evidence_id": evidence_id}
                    for text, evidence_id in claim_data[index]["against"]
                ],
                "unknowns": [
                    {"text": text, "suggested_validation": validation}
                    for text, validation in claim_data[index]["unknowns"]
                ],
            }
        )
    return result


def _candidate_competitors() -> list[dict]:
    points = [
        (-230, 70, "Bäckerei Lausitzer Platz", "h1", "e14"),
        (-150, -80, "Kreuzberger Brotschmiede", "h1", "e15"),
        (-65, 130, "Café am Kanal", "h1", "e15"),
        (95, -105, "Kiez Backstube", "h1", "e14"),
        (210, 55, "Bar Morgenrot", "h2", "e14"),
        (175, 190, "Weinbar Spätlese", "h2", "e15"),
        (-45, -220, "Zum Grünen Glas", "h2", "e15"),
        (-215, -155, "Naturwein Kollektiv", "h2", "e15"),
        (245, -125, "BioKiez Markt", "h3", "e15"),
        (105, 240, "Unverpackt Kreuzberg", "h3", "e15"),
        (-120, 230, "Organic Corner", "h3", "e15"),
        (20, 35, "Späti & Bio", "h3", "e15"),
    ]
    origin_lat, origin_lon = 52.4987, 13.4318
    return [
        {
            "hypothesis_id": hypothesis_id,
            "lat": round(origin_lat + north / 111_320, 6),
            "lon": round(origin_lon + east / (111_320 * 0.608), 6),
            "name": name,
            "evidence_id": evidence_id,
        }
        for east, north, name, hypothesis_id, evidence_id in points
    ]


def _location_trace() -> list[dict]:
    raw_events = [
        (
            "Classifier",
            "classify",
            "shape=location",
            "The question asks where to open a coffee place → rank PLR cells in Kreuzberg.",
            812,
            1.1,
            [],
        ),
        (
            "Planner",
            "plan",
            "6 layers · 6 sub-questions approved",
            "Per-template defaults; weights unchanged",
            2140,
            3.4,
            [],
        ),
        ("Planner", "skip", "hystreet.com, Google Places", "key not provided — skipped", 0, 0, []),
        (
            "Data researcher",
            "tool:wfs",
            "Population density & age groups (ua_einwohnerdichte_2025)",
            "Cafés need resident footfall; density is the cheapest proxy.",
            0,
            1.3,
            ["e2"],
        ),
        (
            "Data researcher",
            "tool:csv",
            "IHK Berlin café registrations (56114/561141)",
            "Keep registered businesses separate from mapped OSM cafés.",
            0,
            1.7,
            ["e3"],
        ),
        (
            "Data researcher",
            "tool:osm",
            "Geofabrik Berlin PBF — amenity=cafe",
            "Complement IHK registrations with the independent OSM inventory.",
            0,
            2.1,
            ["e4"],
        ),
        (
            "Data researcher",
            "tool:wfs",
            "Undisturbed public transport access (oepnv_ungestoert)",
            "Passing commuters add non-resident footfall.",
            0,
            1.5,
            ["e5"],
        ),
        (
            "Data researcher",
            "tool:wfs",
            "Standard land values 2026 (brw2026)",
            "High BRW is a ground-floor rent proxy.",
            0,
            1.2,
            ["e6"],
        ),
        (
            "Scorer",
            "score",
            "19 cells normalised, default mix",
            "min–max within area",
            0,
            0.3,
            ["e1"],
        ),
        (
            "Web researcher",
            "tavily:search",
            '"What does it cost to rent there?" site:berlin.de',
            "Domain-restricted first; widen only if empty",
            1510,
            2.6,
            ["e11"],
        ),
        (
            "Web researcher",
            "tavily:search",
            '"What do IHK / the Bezirk say about gastronomy?" site:ihk.de',
            "Domain-restricted first; widen only if empty",
            1510,
            2.6,
            ["e7", "e8"],
        ),
        (
            "Web researcher",
            "tavily:search",
            '"Which residential/office projects are coming after today?" site:berlin.de',
            "Domain-restricted first; widen only if empty",
            1510,
            2.6,
            ["e9", "e10"],
        ),
        (
            "Web researcher",
            "tavily:extract",
            "verbatim quotes for 5 top cells",
            "Quotes are required for every web claim",
            2900,
            4.2,
            ["e7", "e8", "e9", "e10", "e11"],
        ),
        (
            "Skeptic",
            "challenge",
            "#1 cell",
            "Look for risks with quote + severity",
            2210,
            3.9,
            ["e11"],
        ),
        (
            "Skeptic",
            "rescore",
            "Wrangelkiez demoted (high: rents +18 %)",
            "High-severity risk → demote −0.08",
            0,
            0.1,
            ["e11"],
        ),
        (
            "Fact-checker",
            "reject",
            "Footfall on Oranienstraße rose 30 % in 2026",
            "Quote not found on re-fetch (best fuzzy match 0.38 < 0.85) → dropped, planner re-planned web search for footfall",
            0,
            1.4,
            ["e12"],
        ),
        (
            "Planner",
            "replan",
            "Footfall: rely on transit layer; hystreet still skipped",
            "Unsupported claim removed; no free footfall source",
            610,
            1.0,
            ["e12"],
        ),
        (
            "Fact-checker",
            "verify",
            "12 sources re-fetched",
            "Tavily extract / WFS re-query, fuzzy ≥ 0.85",
            0,
            6.3,
            ["e1", "e2", "e3", "e4", "e5", "e6", "e7", "e8", "e9", "e10", "e11", "e12"],
        ),
        (
            "Writer",
            "write",
            "summary + findings from ledger ids only",
            "Post-processor strips [n] not in ledger",
            3400,
            5.1,
            ["e1", "e2", "e3", "e4", "e7", "e9", "e10", "e11"],
        ),
    ]
    events = []
    for seq, (role, event_type, text, why, tokens, seconds, evidence_ids) in enumerate(raw_events):
        events.append(_event_payload(seq, "mock-t2", role, event_type, text, why, tokens, seconds, evidence_ids))
    events.append(_event_payload(len(events), "mock-t2", "Writer", "done", "Run complete", "", 0, 0.0, []))
    return events


def _point_trace() -> list[dict]:
    raw_events = [
        (
            "Classifier",
            "classify",
            "shape=point",
            "The question asks about a specific storefront at Wiener Straße 12.",
            812,
            1.1,
            [],
        ),
        (
            "Planner",
            "plan",
            "3 hypotheses · 6 dimensions each · 500 m competitor radius",
            "Evaluate business use at the geocoded address.",
            2140,
            3.4,
            [],
        ),
        (
            "Data researcher",
            "tool:wfs",
            'locate("Wiener Straße 12") → PLR 02300315 Lausitzer Platz',
            "Geocode the address before evaluating the storefront.",
            0,
            0.8,
            ["e13"],
        ),
        (
            "Data researcher",
            "tool:csv",
            "IHK bakeries, bars and grocery competitors within 500 m",
            "Keep registered business counts separate from mapped OSM places.",
            0,
            1.6,
            ["e14"],
        ),
        (
            "Data researcher",
            "tool:osm",
            "Geofabrik OSM inventory within 500 m",
            "Report mapped shops and amenities separately from IHK registrations.",
            0,
            2.2,
            ["e15"],
        ),
        (
            "Data researcher",
            "tool:wfs",
            "Population density and age groups for Lausitzer Platz",
            "Assess demand using residential density and age composition.",
            0,
            1.1,
            ["e21"],
        ),
        (
            "Data researcher",
            "tool:wfs",
            "Residential location quality and standard land value at the address",
            "Use Wohnlage and BRW as separate space and cost proxies.",
            0,
            1.2,
            ["e16", "e17"],
        ),
        (
            "Web researcher",
            "tavily:search",
            "Hypothesis signals: bakery closure and natural-wine bar menu prices",
            "Find evidence specific to each proposed storefront use.",
            1620,
            2.9,
            ["e18", "e20"],
        ),
        (
            "Skeptic",
            "challenge",
            "Three hypotheses",
            "Challenge saturation and compare registered competitors with mapped OSM places.",
            1980,
            3.7,
            ["e14", "e15", "e18", "e20"],
        ),
        (
            "Skeptic",
            "rescore",
            "Craft bakery-café remains the leading hypothesis",
            "Reassess the six dimensions without summing the IHK and OSM inventories.",
            0,
            0.8,
            ["e14", "e15", "e17", "e18", "e21"],
        ),
        (
            "Fact-checker",
            "reject",
            "“Rents on Wiener Straße doubled since 2023”",
            "Unsupported forum claim; no rent figure appeared on re-fetch.",
            0,
            1.2,
            ["e22"],
        ),
        (
            "Planner",
            "replan",
            "Use the official standard land value instead of the rejected rent claim",
            "Ground the cost assessment in BRW evidence.",
            540,
            0.9,
            ["e17", "e22"],
        ),
        (
            "Scorer",
            "score",
            "6 dimensions × 3 hypotheses rescored",
            "Keep IHK registrations and OSM places as independent competitor inventories.",
            1100,
            1.4,
            ["e14", "e15", "e16", "e17", "e18", "e20", "e21"],
        ),
        (
            "Fact-checker",
            "verify",
            "10 sources re-fetched",
            "Recheck source quotes and retain the rejected rent-doubling claim as unsupported.",
            0,
            5.2,
            [f"e{evidence_number}" for evidence_number in range(13, 23)],
        ),
        (
            "Writer",
            "write",
            "summary and hypothesis findings from ledger ids only",
            "Post-processor strips citations not present in the evidence ledger.",
            3400,
            5.1,
            ["e13", "e14", "e15", "e17", "e18", "e21", "e22"],
        ),
    ]
    events = []
    for seq, (role, event_type, text, why, tokens, seconds, evidence_ids) in enumerate(raw_events):
        events.append(_event_payload(seq, "mock-t1", role, event_type, text, why, tokens, seconds, evidence_ids))
    events.append(_event_payload(len(events), "mock-t1", "Writer", "done", "Run complete", "", 0, 0.0, []))
    return events


def _plan_document(shape_name: str, evidence: list[dict], cells: list[dict]) -> dict:
    if shape_name == "location":
        question = "Where is it smart to open a coffee place in Kreuzberg?"
        classification = {
            "shape": "location",
            "why": "The question asks where to open a coffee place → rank PLR cells in Kreuzberg.",
            "area": "Kreuzberg",
        }
        run_id = "mock-t2"
    else:
        question = (
            "This storefront at Wiener Straße 12 just became available — what is the best use?"
        )
        classification = {
            "shape": "point",
            "why": "The question is about one specific storefront → evaluate business hypotheses.",
            "address": "Wiener Straße 12",
        }
        run_id = "mock-t1"
    return {
        "synthetic": True,
        "run_id": run_id,
        "status": "awaiting_approval",
        "question": question,
        "classification": classification,
        "plan": _research_plan(shape_name, evidence, cells),
    }


def _result_documents(evidence: list[dict], cells: list[dict]) -> tuple[dict, dict]:
    scores = compute_scores(cells, LAYERS)
    ranked_cells = _rank_cells(cells, scores, "present")
    top_names = [cell["name"] for cell in ranked_cells[:3]]
    location_result = {
        "synthetic": True,
        "run_id": "mock-t2",
        "status": "done",
        "question": "Where is it smart to open a coffee place in Kreuzberg?",
        "role": "coffee operator",
        "shape": "location",
        "report_md": (
            f"The strongest default locations are **{top_names[0]}**, {top_names[1]}, and "
            f"{top_names[2]}, ranked from the PLR cell mix [1]. Dense, younger cells "
            "score well on demand, while the IHK-registered and OSM café inventories "
            "remain separate measures of competition [2] [3] [4].\n\n"
            "The announced Moritzplatz office campus and Köpenicker Straße housing "
            "project raise future potential in their respective cells [9] [10]. "
            "Wrangelkiez has a high-severity rent risk reflecting the reported 18 % rise [11]."
        ),
        "models": MODELS,
        "self_grade": [
            {"ok": True, "text": "All inline citations resolve to the evidence ledger."},
            {"ok": True, "text": "Missing OSM data is represented as null, not zero."},
            {"ok": False, "text": "Actual pedestrian footfall is unknown; hystreet was skipped."},
        ],
    }
    point_result = {
        "synthetic": True,
        "run_id": "mock-t1",
        "status": "done",
        "question": "This storefront at Wiener Straße 12 just became available — what is the best use?",
        "role": "operator: 6 yrs bakery experience, budget €80k, 2 staff",
        "shape": "point",
        "report_md": (
            "Wiener Straße 12 is in PLR Lausitzer Platz, where the demo identifies a "
            "young, dense population [13] [21]. The craft bakery-café ranks first: "
            "the fixture cites a recent closure and four registered bakeries within "
            "500 m, while OSM records three mapped bakeries [18] [14] [15]. A "
            "natural-wine bar faces 11 registered bars and 13 mapped bars nearby "
            "[14] [15].\n\n"
            "Oven ventilation remains an important unverified property constraint, "
            "and the unsupported rent-doubling claim was rejected [22]."
        ),
        "models": MODELS,
        "self_grade": [
            {"ok": True, "text": "The property was geocoded to a Berlin PLR."},
            {"ok": True, "text": "Three hypotheses include six dimensions each."},
            {
                "ok": False,
                "text": "Building ventilation and current vacancies need field validation.",
            },
        ],
    }
    return location_result, point_result


def _findings() -> list[dict]:
    return [
        {
            "finding_id": "f_rent_wrangelkiez",
            "claim": "Ground-floor rents in Wrangelkiez rose 18% year over year.",
            "quote": "…Erdgeschossmieten im Wrangelkiez +18 % gegenüber Vorjahr…",
            "evidence_ids": ["e11"],
            "claim_type": "risk",
            "localization": "PLR 02300419",
            "agent": "skeptic",
            "status": "accepted",
            "verification": "partial",
            "synthetic": True,
        },
        {
            "finding_id": "f_footfall_rejected",
            "claim": "Footfall on Oranienstraße rose 30% in 2026.",
            "quote": "Quote not found on re-fetch (best fuzzy match 0.38 < 0.85).",
            "evidence_ids": ["e12"],
            "claim_type": "demand",
            "localization": "Oranienstraße",
            "agent": "fact_checker",
            "status": "rejected",
            "verification": "unsupported",
            "reject_reason": "The quote was not found on re-fetch.",
            "synthetic": True,
        },
        {
            "finding_id": "f_rent_claim_rejected",
            "claim": "Rents on Wiener Straße doubled since 2023.",
            "quote": "Unsupported: forum post, no figure on re-fetch.",
            "evidence_ids": ["e22"],
            "claim_type": "cost",
            "localization": "Wiener Straße 12",
            "agent": "fact_checker",
            "status": "rejected",
            "verification": "unsupported",
            "reject_reason": "No rent figure appeared on re-fetch.",
            "synthetic": True,
        },
    ]


def _metrics(events: list[dict]) -> dict[str, dict]:
    metrics = {}
    for event in events:
        role = event["agent"]
        item = metrics.setdefault(
            role,
            {
                "model": event["model"],
                "calls": 0,
                "tokens_in": 0,
                "tokens_out": 0,
                "seconds": 0.0,
            },
        )
        item["calls"] += 1
        item["tokens_in"] += event["tokens_in"] or 0
        item["tokens_out"] += event["tokens_out"] or 0
        item["seconds"] += event["seconds"] or 0.0
    return metrics


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build(geojson_from: Path | None = None, gap_geojson_from: Path | None = None) -> None:
    geo_cells = _fixture_cells()
    cells = _normalized_cells(geo_cells)
    evidence = _evidence()
    source_path = geojson_from or (FIXTURES / "cells.geojson")
    geojson = build_geojson(_geojson_source(source_path, {cell["id"] for cell in cells}), cells)
    gap_cells = _gap_cells()
    gap_evidence = _gap_evidence()
    gap_source_path = gap_geojson_from or (
        FIXTURES / "cells_gap.geojson" if (FIXTURES / "cells_gap.geojson").exists() else None
    )
    gap_geojson = build_geojson(
        _geojson_source(gap_source_path, {cell["id"] for cell in gap_cells}),
        gap_cells,
    )

    _write_json(
        FIXTURES / "cells.json",
        {
            "synthetic": True,
            "area": {
                "area_id": "ortsteil:0202",
                "name": "Kreuzberg",
                "level": "ortsteil",
                "plr_count": len(cells),
                "cell_count": len(cells),
            },
            "mode_default": "present",
            "layers": LAYERS,
            "cells": cells,
        },
    )
    _write_json(FIXTURES / "cells.geojson", geojson)
    (FIXTURES / "evidence.jsonl").write_text(
        "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in evidence),
        encoding="utf-8",
    )
    location_trace = _location_trace()
    point_trace = _point_trace()
    (FIXTURES / "trace.jsonl").write_text(
        "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in location_trace),
        encoding="utf-8",
    )
    (FIXTURES / "trace_point.jsonl").write_text(
        "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in point_trace),
        encoding="utf-8",
    )
    _write_json(
        FIXTURES / "candidates.json",
        {
            "synthetic": True,
            "space": {
                "address": "Wiener Straße 12, 10999 Berlin",
                "lat": 52.4987,
                "lon": 13.4318,
                "cell_id": "02300315",
                "cell_name": "Lausitzer Platz",
                "evidence_id": "e13",
            },
            "operator": {
                "experience": "6 yrs bakery experience",
                "budget_eur": 80000,
                "staff": 2,
            },
            "hypotheses": _point_hypotheses(),
            "competitors": _candidate_competitors(),
            "radius_m": 500,
        },
    )
    _write_json(FIXTURES / "plan_location.json", _plan_document("location", evidence, cells))
    _write_json(FIXTURES / "plan_point.json", _plan_document("point", evidence, cells))
    location_result, point_result = _result_documents(evidence, cells)
    _write_json(FIXTURES / "result_location.json", location_result)
    _write_json(FIXTURES / "result_point.json", point_result)
    (FIXTURES / "findings.jsonl").write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in _findings()),
        encoding="utf-8",
    )
    _write_json(FIXTURES / "metrics.json", _metrics(location_trace))
    _write_json(FIXTURES / "metrics_point.json", _metrics(point_trace))

    gap_trace = _gap_trace()
    _write_json(
        FIXTURES / "cells_gap.json",
        {
            "synthetic": True,
            "area": {
                "area_id": "ortsteil:0801",
                "name": "Neukölln",
                "level": "ortsteil",
                "plr_count": len(gap_cells),
                "cell_count": len(gap_cells),
            },
            "mode_default": "present",
            "layers": _gap_layers(),
            "cells": gap_cells,
        },
    )
    _write_json(FIXTURES / "cells_gap.geojson", gap_geojson)
    (FIXTURES / "evidence_gap.jsonl").write_text(
        "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in gap_evidence),
        encoding="utf-8",
    )
    _write_json(FIXTURES / "plan_gap.json", _gap_plan_document(gap_evidence, gap_cells))
    _write_json(
        FIXTURES / "result_gap.json",
        _gap_result_document(gap_evidence, gap_cells, gap_trace),
    )
    (FIXTURES / "trace_gap.jsonl").write_text(
        "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in gap_trace),
        encoding="utf-8",
    )
    (FIXTURES / "findings_gap.jsonl").write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in _gap_findings()),
        encoding="utf-8",
    )
    _write_json(FIXTURES / "metrics_gap.json", _metrics(gap_trace))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--geojson-from",
        type=Path,
        help="Use a cached WFS FeatureCollection instead of making a network request.",
    )
    parser.add_argument(
        "--gap-geojson-from",
        type=Path,
        help="Use cached Neukölln PLR geometry instead of the checked-in GeoJSON.",
    )
    arguments = parser.parse_args()
    build(arguments.geojson_from, arguments.gap_geojson_from)


if __name__ == "__main__":
    main()
