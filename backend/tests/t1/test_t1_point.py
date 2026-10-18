"""T1 point path with in-memory readers (no network, no snapshots)."""

import json
import math

import pytest

from app.application.t1 import (
    Poi,
    ReferencePlr,
    StaticReaders,
    build_candidates,
    load_catalog,
    prepare_point,
)
from app.application.t1.catalog import parse_catalog
from app.contracts.candidates import CandidatesDocument
from app.domain import Address, record_to_dict
from app.evidence.ledger import EvidenceLedger
from app.scoring import v2

HOME = (13.4200, 52.4990)
PARK = (13.4020, 52.4730)
SOURCES = {
    role: {
        "dataset_id": dataset,
        "snapshot_id": f"{dataset}-fixture",
        "source_url": "https://example.org",
    }
    for role, dataset in {
        "addresses": "berlin_addresses",
        "boundary": "berlin_lor",
        "census": "zensus_grid",
        "places": "osm_pois",
        "transit": "vbb_gtfs",
    }.items()
}


def mid_rank(value, reference):
    below = sum(1 for r in reference if r < value)
    equal = sum(1 for r in reference if r == value)
    return (below + 0.5 * equal) / len(reference)


@pytest.fixture(autouse=True)
def percentile_stub(monkeypatch):
    monkeypatch.setattr(v2, "percentile_rank", mid_rank)
    monkeypatch.delenv("VALHALLA_URL", raising=False)


def square(lon, lat, d=0.01):
    ring = [
        [lon - d, lat - d],
        [lon + d, lat - d],
        [lon + d, lat + d],
        [lon - d, lat + d],
        [lon - d, lat - d],
    ]
    return {"type": "Polygon", "coordinates": [ring]}


def address(record_id, street, number, postcode, lon, lat, plr="02040501"):
    record = Address(
        source_id="berlin_addresses",
        snapshot_id="berlin_addresses-fixture",
        source_record_id=record_id,
        record_id=f"address:{record_id}",
        geometry={"type": "Point", "coordinates": [lon, lat]},
        street_name=street,
        address_type="H",
        house_number=number,
        postcode=postcode,
        district_code="02",
        district_name="Friedrichshain-Kreuzberg",
        area_code=plr,
        area_name="Wrangelkiez",
    )
    return record_to_dict(record)


def reference():
    plrs = []
    for i in range(20):
        residents = 2500 + 500 * i
        plrs.append(
            ReferencePlr(
                plr_id=f"p{i:02d}",
                residents=residents,
                area_km2=1.0,
                counts={
                    "coffee_shop": i % 7,
                    "restaurant": i % 9,
                    "fast_food": i % 5,
                    "bakery": i % 4,
                    "coworking": i % 2,
                },
                young_adult_share=0.3 + 0.01 * i,
                transit={"morning": 100.0 * i, "evening": 80.0 * i},
            )
        )
    return plrs


def offset(lon, lat, dx_m, dy_m):
    return lon + dx_m / (111_320 * math.cos(math.radians(lat))), lat + dy_m / 110_540


def readers(**overrides):
    near = [offset(*HOME, dx, dy) for dx, dy in ((100, 0), (-150, 80), (0, 200), (300, -100))]
    park_near = [offset(*PARK, dx, dy) for dx, dy in ((120, 0), (-200, 50))]
    values = dict(
        addresses=[
            address("1", "Wrangelstraße", 10, "10997", *HOME),
            address("2", "Hauptstraße", 5, "10827", 13.35, 52.49, plr="07010101"),
            address("3", "Hauptstraße", 5, "13055", 13.50, 52.54, plr="11010101"),
        ],
        plrs=[
            {
                "plr_id": "02040501",
                "name": "Wrangelkiez",
                "geometry": square(*HOME),
                "young_adult_share": 0.45,
            },
            {"plr_id": "02020203", "name": "Tempelhofer Feld", "geometry": square(*PARK)},
        ],
        residents=[(*HOME, 9000.0), (*offset(*HOME, 200, 200), 3000.0)],
        places=[
            Poi("osm:c1", "coffee_shop", *near[0], "Café Eins"),
            Poi("osm:c2", "coffee_shop", *near[1], "Café Zwei"),
            Poi("osm:r1", "restaurant", *near[2], "Lokal"),
            Poi("osm:b1", "bakery", *near[3], "Bäcker"),
            Poi("osm:k1", "hairdresser", *near[0], "Salon"),
            Poi("osm:pc1", "coffee_shop", *park_near[0], "Kiosk-Café"),
            Poi("osm:pc2", "coffee_shop", *park_near[1], "Feld-Café"),
        ],
        stations=[
            {"lon": near[0][0], "lat": near[0][1], "departures": {"morning": 600, "evening": 500}}
        ],
        reference=reference(),
        sources=SOURCES,
    )
    values.update(overrides)
    return StaticReaders(**values)


def test_ambiguous_address_returns_clarify_candidates():
    result = prepare_point("Hauptstraße 5", readers=readers())
    assert "hypotheses" not in result
    assert {c["postcode"] for c in result["clarify"]} == {"10827", "13055"}
    assert all({"address", "lat", "lon"} <= set(c) for c in result["clarify"])


def test_unknown_address_returns_empty_clarify():
    result = prepare_point("Nichtvorhandenweg 1", readers=readers())
    assert result["clarify"] == []


def test_resolved_address_gives_space_hypotheses_and_evidence(tmp_path):
    ledger = EvidenceLedger(tmp_path)
    result = prepare_point("Wrangelstraße 10", readers=readers(), ledger=ledger)
    space = result["space"]
    assert space["cell_id"] == "02040501"
    assert space["evidence_id"] and ledger.get(space["evidence_id"])
    assert len(result["hypotheses"]) == 3
    assert result["low_resident_catchment"] is False
    ids = {e["evidence_id"] for e in ledger.entries()}
    for h in result["hypotheses"]:
        assert [d["name"] for d in h["dimensions"]] == [
            "Demand",
            "Competition gap",
            "Footfall/Transit",
        ]
        for d in h["dimensions"]:
            assert 0 <= d["score"] <= 10 and d["why"]
            assert d["evidence_ids"] and set(d["evidence_ids"]) <= ids
    cafe_competitors = [c for c in result["competitors"] if c["hypothesis_id"] == "cafe"]
    if cafe_competitors:
        assert {c["name"] for c in cafe_competitors} == {"Café Eins", "Café Zwei"}


def test_dimensions_without_ledger_have_no_evidence_ids():
    result = prepare_point("Wrangelstraße 10", readers=readers())
    assert all(not d["evidence_ids"] for h in result["hypotheses"] for d in h["dimensions"])
    assert result["space"]["evidence_id"] is None


def test_zero_resident_catchment_is_finite_flagged_and_low_confidence():
    result = prepare_point(lat=PARK[1], lon=PARK[0], readers=readers())
    assert result["catchment"]["residents"] == 0
    assert result["catchment"]["denominator"] == 2000
    assert result["low_resident_catchment"] is True
    assert result["confidence"] == "low"
    cafe = next(
        h
        for h in prepare_point(
            lat=PARK[1],
            lon=PARK[0],
            readers=readers(),
            operator_profile={"preferred_types": ["cafe"]},
        )["hypotheses"]
        if h["id"] == "cafe"
    )
    assert cafe["metrics"]["competitors"] == 2
    assert cafe["metrics"]["competitors_per_10k"] == 10.0
    for h in result["hypotheses"] + [cafe]:
        assert h["confidence"] == "low"
        assert any("low_resident_catchment" in r for r in h["confidence_reasons"])
        for d in h["dimensions"]:
            assert math.isfinite(d["score"]) and 0 <= d["score"] <= 10
        demand = h["dimensions"][0]
        assert "residents only" in demand["why"]
    doc = build_candidates(result, [], {})
    assert doc.low_resident_catchment is True
    assert any("2,000 residents" in u.text for h in doc.hypotheses for u in h.unknowns)


def test_ext_types_are_never_offered():
    catalog = load_catalog()
    ext = {t.id for t in catalog.types if t.mapping == "ext"}
    assert ext == {"convenience", "hairdresser", "fitness", "bicycle"}
    assert not ext & {t.id for t in catalog.offered()}
    result = prepare_point(
        "Wrangelstraße 10",
        readers=readers(),
        operator_profile={
            "preferred_types": sorted(ext),
            "experience": "Friseur, Fahrrad, Yoga, Späti",
        },
        top_n=10,
    )
    offered = {h["id"] for h in result["hypotheses"]} | {c["id"] for c in result["considered"]}
    assert not offered & ext
    assert ext <= {s["id"] for s in result["skipped_types"]}


def test_osm_v1_type_with_unmapped_category_is_not_offered():
    data = json.loads(
        (
            load_catalog.__wrapped__.__globals__["CONFIG_DIR"] / "templates/t1_catalog_v1.json"
        ).read_text()
    )
    catalog = parse_catalog(data, frozenset({"coffee_shop", "bakery", "fast_food", "restaurant"}))
    assert "coworking" not in {t.id for t in catalog.offered()}
    assert "cafe" not in {t.id for t in catalog.offered()}  # complement `coworking` unmapped


def test_operator_profile_preference_and_exclusion():
    result = prepare_point(
        "Wrangelstraße 10",
        readers=readers(),
        operator_profile={"experience": "10 years as a baker", "excluded_types": ["cafe"]},
    )
    ids = [h["id"] for h in result["hypotheses"]]
    assert ids[0] == "bakery_cafe"
    assert "cafe" not in ids


def test_prepare_point_is_deterministic():
    a = prepare_point("Wrangelstraße 10", readers=readers())
    b = prepare_point("Wrangelstraße 10", readers=readers())
    assert a == b


def test_missing_transit_and_reference_stay_finite():
    result = prepare_point("Wrangelstraße 10", readers=readers(stations=None, reference=[]))
    assert result["confidence"] == "low"
    for h in result["hypotheses"]:
        assert all(d["score"] == 5 for d in h["dimensions"][1:])


def test_candidates_document_validates(tmp_path):
    ledger = EvidenceLedger(tmp_path)
    prepared = prepare_point(
        "Wrangelstraße 10",
        readers=readers(),
        ledger=ledger,
        operator_profile={"experience": "barista", "budget_eur": 60000},
    )
    web = [
        {
            "evidence_id": "ev_rent",
            "method": "tavily-search",
            "url": "https://press.example/rent",
            "tier": "press",
        },
        {
            "evidence_id": "ev_blog",
            "method": "tavily-search",
            "url": "https://blog.example/x",
            "tier": "weak",
        },
        {
            "evidence_id": "ev_risk",
            "method": "tavily-extract",
            "url": "https://berlin.de/x",
            "tier": "official",
        },
    ]
    ledger.extend(web)
    top = prepared["hypotheses"][0]["id"]
    other = prepared["hypotheses"][1]["id"]
    findings = [
        {
            "finding_id": "f1",
            "claim": "Shop rents rose 18 %.",
            "quote": "q",
            "evidence_ids": ["ev_rent"],
            "claim_type": "fact",
            "agent": f"researcher:t1:{top}",
            "status": "accepted",
            "verification": "verified",
        },
        {
            "finding_id": "f2",
            "claim": "Street closed for works until 2028.",
            "quote": "q",
            "evidence_ids": ["ev_risk", "ev_unknown"],
            "matched_ids": ["ev_risk"],
            "claim_type": "risk",
            "agent": f"skeptic:{top}",
            "status": "accepted",
            "verification": "partial",
            "severity": "high",
        },
        {
            "finding_id": "f3",
            "claim": "Unsupported.",
            "quote": "q",
            "evidence_ids": ["ev_rent"],
            "claim_type": "fact",
            "agent": f"researcher:t1:{top}",
            "status": "accepted",
            "verification": "unsupported",
        },
        {
            "finding_id": "f4",
            "claim": "Elsewhere.",
            "quote": "q",
            "evidence_ids": ["ev_rent"],
            "claim_type": "fact",
            "agent": "researcher:t1:hairdresser",
            "status": "accepted",
            "verification": "verified",
        },
    ]
    fit = {
        top: {
            "Cost": {
                "score": 4,
                "why": "Rents rose",
                "evidence_ids": ["ev_rent", prepared["space"]["evidence_id"]],
            },
            "Operator fit": {
                "score": 9,
                "why": "Barista experience",
                "evidence_ids": [],
                "profile_fields": ["experience"],
            },
            "space_fit": {"score": 9, "why": "Blog says spacious", "evidence_ids": ["ev_blog"]},
        },
        other: {"Space fit": {"score": 8, "why": "no ids", "evidence_ids": []}},
    }
    doc = build_candidates(prepared, findings, fit, ledger=ledger)
    again = CandidatesDocument.model_validate(json.loads(doc.model_dump_json(by_alias=True)))
    assert len(again.hypotheses) == 3
    by_id = {h.id: h for h in doc.hypotheses}
    h = by_id[top]
    dims = {d.name: d for d in h.dimensions}
    assert set(dims) == {
        "Demand",
        "Competition gap",
        "Footfall/Transit",
        "Cost",
        "Operator fit",
        "Space fit",
        "Regulation / risk",
    }
    assert dims["Cost"].evidence_ids == ["ev_rent"]
    assert dims["Operator fit"].score == 9
    assert dims["Space fit"].score == 6
    assert dims["Regulation / risk"].score < 5
    assert [c.text for c in h.for_] == ["Shop rents rose 18 %."]
    assert [c.evidence_id for c in h.against] == ["ev_risk"]
    o = {d.name: d for d in by_id[other].dimensions}
    assert o["Space fit"].score == 5 and not o["Space fit"].evidence_ids
    assert any("Space fit" in u.text and u.suggested_validation for u in by_id[other].unknowns)
    assert sorted(x.rank_initial for x in doc.hypotheses) == [1, 2, 3]
    assert sorted(x.rank_final for x in doc.hypotheses) == [1, 2, 3]
    assert all(c.hypothesis_id in by_id for c in doc.competitors)
    assert all(c.evidence_id for c in doc.competitors)


def test_snapshot_readers_without_snapshots_degrade_gracefully(tmp_path):
    from app.application.service import DataService
    from app.application.t1 import SnapshotReaders

    snap = SnapshotReaders(DataService(data_dir=tmp_path))
    assert prepare_point("Wrangelstraße 10", readers=snap)["clarify"] == []
    result = prepare_point(lat=HOME[1], lon=HOME[0], readers=snap)
    assert result["catchment"]["type"] == "circle"
    assert result["confidence"] == "low"
    assert len(result["hypotheses"]) == 3
    doc = build_candidates(result, [], {})
    assert all(0 <= d.score <= 10 for h in doc.hypotheses for d in h.dimensions)
