import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import runs_live
from app.application.area_data import AreaUnit, FixtureAreaData, SummaryAreaData
from app.application.runs import Deps, RunService
from app.contracts.api import RunResult
from app.contracts.cells import CellsDocument
from app.contracts.evidence import Finding
from app.evidence.factcheck import FactChecker
from app.scoring import v2

LAYERS = (
    v2.LayerSpec("residents_block_density", "Density", "+", 0.5, required=True),
    v2.LayerSpec("cafe_saturation", "Cafés", "-", 0.5, required=True),
)
TEMPLATE = v2.Template(
    "coffee_v2",
    LAYERS,
    future_effect={"housing_growth": "+"},
    future_units={"housing_growth": "dwellings"},
)
PLRS = {
    f"0810010{i}": {
        "id": f"0810010{i}",
        "name": f"Cell {i}",
        "parent_name": "Neuköllner Mitte",
        "residents": 3000 + i,
        "raw": {"residents_block_density": 1000.0 * i, "cafe_saturation": 10.0 - i},
    }
    for i in range(1, 6)
}
IDS = tuple(PLRS)
UNITS = [
    AreaUnit("bezirk:08", "bezirk", "Neukölln", "Neukölln", IDS),
    AreaUnit("ortsteil:0810", "ortsteil", "Neukölln", "Neukölln", IDS),
    AreaUnit("ortsteil:0820", "ortsteil", "Britz", "Neukölln", IDS[:2]),
]


CALLS: list = []


def score_area(
    cells, template, *, berlin_reference=None, modifiers=(), extra_confidence_reasons=None, **kwargs
):
    CALLS.append(("score_area", sorted(kwargs), [m.kind for m in modifiers]))
    bonus = {m.cell_id: 0.1 for m in modifiers}
    scored = []
    for cell in cells:
        present = cell.raw["residents_block_density"] / 10000
        scored.append(
            v2.ScoredCell(
                cell.cell_id,
                cell.name,
                {k: 0.5 for k in cell.raw},
                dict(cell.raw),
                present,
                None,
                present + bonus.get(cell.cell_id, 0.0),
                modifiers=[
                    {"label": m.label, "evidence_id": m.evidence_id, "quote": m.quote}
                    for m in modifiers
                    if m.cell_id == cell.cell_id
                ],
                parent_name=cell.parent_name,
            )
        )
    return v2.ScoreResult(template.id, scored, [])


def rank(cells, mode="present", *, high_risk=None, tie_band=0.02):
    ordered = sorted(cells, key=lambda c: -c.score_present)
    for cell in ordered:
        cell.demoted_by = list((high_risk or {}).get(cell.cell_id, []))
    return sorted(ordered, key=lambda c: bool(c.demoted_by))


def to_cells_document(result, area, template, *, mode_default="present"):
    return CellsDocument.model_validate(
        {
            "area": {
                "area_id": area["area_id"],
                "name": area["name"],
                "level": area["level"],
                "plr_count": area["plr_count"],
            },
            "layers": [
                {
                    "id": s.id,
                    "label": s.label,
                    "direction": s.direction,
                    "weight_default": s.weight,
                    "evidence_id": "x",
                }
                for s in template.layers
            ],
            "cells": [
                {
                    "id": c.cell_id,
                    "name": c.name,
                    "values": c.values,
                    "raw_values": c.raw_values,
                    "score_present": c.score_present,
                    "score_future": c.score_future,
                    "risks": c.risks,
                    "demoted_by": c.demoted_by,
                    "modifiers": [
                        {"label": m["label"], "quote": m["quote"], "evidence_id": m["evidence_id"]}
                        for m in c.modifiers
                    ],
                }
                for c in result.cells
            ],
        }
    )


def fake_scoring(**overrides):
    funcs = dict(
        FutureModifier=v2.FutureModifier,
        load_template=lambda _id: TEMPLATE,
        score_area=score_area,
        rank=rank,
        to_cells_document=to_cells_document,
    )
    return SimpleNamespace(**{**funcs, **overrides})


def _web(ledger, eid, text):
    ledger.append(
        {
            "evidence_id": eid,
            "method": "tavily-search",
            "url": f"https://example.org/{eid}",
            "title": eid,
            "passage": text,
            "status": "unverified",
        }
    )


async def research_area(question, area, cells, ledger, emit, cfg, **options):
    CALLS.append((options["agent"], cfg.max_searches if hasattr(cfg, "max_searches") else None))
    _web(ledger, "ev_housing", "Bis 2028 entstehen 400 Wohnungen am Standort.")
    emit("search", {"why": "housing", "query": "Wohnungsbau Neukölln"})
    return [
        Finding(
            finding_id="f_future",
            claim="400 dwellings by 2028",
            quote="Bis 2028 entstehen 400 Wohnungen",
            evidence_ids=["ev_housing"],
            claim_type="future_project",
            localization=f"plr:{cells[0]['id']}",
            agent=options["agent"],
            status="accepted",
            unit="dwellings",
            amount=400,
            kind="housing",
        ),
        Finding(
            finding_id="f_fake",
            claim="invented",
            quote="this sentence is not on the page at all",
            evidence_ids=["ev_housing"],
            claim_type="fact",
            localization="area",
            agent=options["agent"],
            status="accepted",
        ),
    ]


async def research_cell(question, cell, ledger, emit, cfg, **options):
    CALLS.append((options["agent"], cfg.max_searches, cfg.max_extracts))
    emit("model_call", {"model": "fake", "tokens_in": 10, "tokens_out": 5})
    return []


async def skeptic(question, cell, findings, ledger, emit, cfg, **options):
    CALLS.append((options["agent"], cfg.max_searches, cfg.max_extracts))
    _web(ledger, f"ev_risk{cell['id']}", "Die Miete hat sich verdoppelt.")
    return [
        {
            "severity": "high",
            "text": "Rent doubled",
            "quote": "Die Miete hat sich verdoppelt",
            "evidence_id": f"ev_risk{cell['id']}",
            "finding_id": f"f_risk_{cell['id']}",
        }
    ]


async def write_report(question, result, findings, ledger, emit, cfg, **options):
    emit("write", {"why": "report"})
    return "Neukölln has new housing [ev_housing] and a bogus ref [ev_nope]."


def fake_research(**overrides):
    funcs = dict(
        research_area=research_area,
        research_cell=research_cell,
        skeptic=skeptic,
        write_report=write_report,
    )
    funcs.update(overrides)
    return SimpleNamespace(**funcs)


@pytest.fixture
def make_client(tmp_path):
    clients = []

    def make(scoring=None, research=None, t1=None):
        deps = Deps(
            scoring=scoring or fake_scoring(),
            research=research or fake_research(),
            port=FixtureAreaData(UNITS, PLRS),
            checker_factory=lambda: FactChecker(offline=True),
            config_factory=lambda: SimpleNamespace(fake=True, max_searches=8, max_extracts=6),
            t1=t1,
        )
        runs_live.configure(RunService(deps, tmp_path))
        app = FastAPI()
        app.include_router(runs_live.router)
        client = TestClient(app).__enter__()
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.__exit__(None, None, None)
    runs_live.configure(None)


def _events(client, run_id, auth, last=None):
    headers = dict(auth, **({"Last-Event-ID": str(last)} if last is not None else {}))
    with client.stream("GET", f"/runs/{run_id}/events", headers=headers) as response:
        assert response.status_code == 200
        body = "".join(response.iter_text())
    return [json.loads(line[5:]) for line in body.splitlines() if line.startswith("data:")]


def test_full_flow_clarify_approve_stream_result(make_client, tmp_path):
    client = make_client()
    created = client.post(
        "/research", json={"question": "Where should I open a café in Neukölln?"}
    ).json()
    assert created["status"] == "needs_clarification" and created["mode"] == "live"
    assert "Bezirk" in created["clarify_question"] and "Ortsteil" in created["clarify_question"]
    auth = {"Authorization": f"Bearer {created['run_token']}"}
    run_id = created["run_id"]

    planned = client.post(
        f"/runs/{run_id}/clarify", json={"answer": "the Ortsteil"}, headers=auth
    ).json()
    assert planned["status"] == "awaiting_approval"
    plan = planned["plan"]
    assert plan["area"] == {
        "area_id": "ortsteil:0810",
        "level": "ortsteil",
        "name": "Neukölln",
        "plr_count": 5,
    }
    assert {c["area_id"] for c in plan["area_candidates"]} >= {"ortsteil:0810", "bezirk:08"}
    assert [s["id"] for s in plan["sub_questions"]] == ["s1", "s2", "s3"]
    assert (
        client.get(f"/runs/{run_id}/areas?q=britz", headers=auth).json()[0]["area_id"]
        == "ortsteil:0820"
    )
    assert client.get(f"/runs/{run_id}/result", headers=auth).status_code == 202

    bad = client.post(
        f"/runs/{run_id}/approve",
        json={"layers": [{"id": "nope", "enabled": True, "weight": 0.5}]},
        headers=auth,
    )
    assert bad.status_code == 422
    approved = client.post(
        f"/runs/{run_id}/approve",
        json={
            "subquestions": ["s1", "s2"],
            "layers": [
                {"id": "residents_block_density", "enabled": True, "weight": 0.6},
                {"id": "cafe_saturation", "enabled": False, "weight": 0.4},
            ],
        },
        headers=auth,
    )
    assert approved.json() == {"status": "approved"}

    events = _events(client, run_id, auth)
    types = [e["type"] for e in events]
    assert [e["seq"] for e in events] == list(range(len(events)))
    assert all(e["run_id"] == run_id and e["ts"] for e in events)
    for expected in (
        "classify",
        "clarify",
        "area",
        "plan",
        "approved",
        "layer",
        "score",
        "search",
        "fact_check",
        "finding_rejected",
        "merge",
        "rescore",
        "skeptic_risk",
        "write",
        "metrics",
    ):
        assert expected in types, expected
    assert events[-1]["detail"] == {"status": "done"}
    replay = _events(client, run_id, auth, last=events[-3]["seq"])
    assert [e["seq"] for e in replay] == [events[-2]["seq"], events[-1]["seq"]]

    response = client.get(f"/runs/{run_id}/result", headers=auth)
    assert response.status_code == 200
    result = RunResult.model_validate(response.json())
    assert result.status == "done" and result.shape == "location"
    top = result.cells.cells[0]
    assert top.id != "08100105" and result.cells.cells[-1].demoted_by
    housing = next(c for c in result.cells.cells if c.id == "08100101")
    assert housing.modifiers and housing.modifiers[0].evidence_id == "ev_housing"
    layers = {layer.id: layer for layer in result.cells.layers}
    assert (
        layers["cafe_saturation"].enabled is False
        and layers["residents_block_density"].weight_default == 0.6
    )
    findings = {f.finding_id: f for f in result.findings}
    assert findings["f_future"].verification == "verified"
    assert findings["f_fake"].status == "rejected"
    assert "[ev_housing]" in result.report_md and "ev_nope" not in result.report_md
    assert any(e.method == "dataset" for e in result.evidence)

    run_dir = tmp_path / run_id
    for name in (
        "cells.json",
        "evidence.jsonl",
        "findings.jsonl",
        "report.md",
        "events.jsonl",
        "plan.json",
    ):
        assert (run_dir / name).exists(), name
    assert "token_hash" in json.loads((run_dir / "run.json").read_text())
    assert created["run_token"] not in (run_dir / "run.json").read_text()


def test_auth_rejection(make_client):
    client = make_client()
    created = client.post("/research", json={"question": "Café in Britz?"}).json()
    assert created["status"] == "awaiting_approval"
    run_id = created["run_id"]
    for headers in ({}, {"Authorization": "Bearer wrong"}):
        assert client.get(f"/runs/{run_id}/result", headers=headers).status_code == 404
        assert client.post(f"/runs/{run_id}/approve", json={}, headers=headers).status_code == 404
        assert client.get(f"/runs/{run_id}/events", headers=headers).status_code == 404
    assert (
        client.get(
            "/runs/unknown/result", headers={"Authorization": f"Bearer {created['run_token']}"}
        ).status_code
        == 404
    )


def test_area_change_regenerates_plan_and_events_need_approval(make_client):
    client = make_client()
    created = client.post("/research", json={"question": "Café in Britz?"}).json()
    auth = {"Authorization": f"Bearer {created['run_token']}"}
    run_id = created["run_id"]
    assert client.get(f"/runs/{run_id}/events", headers=auth).status_code == 409
    changed = client.post(
        f"/runs/{run_id}/approve", json={"area_id": "bezirk:08"}, headers=auth
    ).json()
    assert (
        changed["status"] == "awaiting_approval"
        and changed["plan"]["area"]["area_id"] == "bezirk:08"
    )
    assert (
        client.post(
            f"/runs/{run_id}/approve", json={"area_id": "bezirk:99"}, headers=auth
        ).status_code
        == 422
    )


def test_failure_emits_error_and_failed_status(make_client):
    def broken(*args, **kwargs):
        raise RuntimeError("scoring exploded")

    client = make_client(scoring=SimpleNamespace(**{**vars(fake_scoring()), "score_area": broken}))
    created = client.post("/research", json={"question": "Café in Britz?"}).json()
    auth = {"Authorization": f"Bearer {created['run_token']}"}
    assert client.post(f"/runs/{created['run_id']}/approve", json={}, headers=auth).json() == {
        "status": "approved"
    }
    events = _events(client, created["run_id"], auth)
    error = next(e for e in events if e["type"] == "error")
    assert "scoring exploded" in error["detail"]["error"]
    assert events[-1]["detail"] == {"status": "failed"}
    result = client.get(f"/runs/{created['run_id']}/result", headers=auth).json()
    assert result["status"] == "failed" and "scoring exploded" in result["error"]


def test_researcher_failure_is_contained(make_client):
    async def boom(*args, **kwargs):
        raise TimeoutError("agent timeout")

    client = make_client(research=fake_research(research_area=boom))
    created = client.post("/research", json={"question": "Café in Britz?"}).json()
    auth = {"Authorization": f"Bearer {created['run_token']}"}
    client.post(f"/runs/{created['run_id']}/approve", json={"wave2": False}, headers=auth)
    events = _events(client, created["run_id"], auth)
    assert any(e["type"] == "tool_error" for e in events)
    assert client.get(f"/runs/{created['run_id']}/result", headers=auth).json()["status"] == "done"


def test_point_without_t1_is_not_available(make_client):
    client = make_client()
    response = client.post("/research", json={"question": "Which business fits Oranienstraße 25?"})
    assert response.status_code == 501
    assert "not available" in response.json()["detail"]


def test_summary_area_data_maps_layers_and_records_ledger(tmp_path):
    from app.evidence.ledger import EvidenceLedger

    src = [{"dataset_id": "berlin_population", "snapshot_id": "p1"}]

    def field(value):
        return {"value": value, "sources": src}

    def summary(plr, category="coffee_shop", include_geometry=True):
        counts = {"coffee_shop": 4, "bakery": 2, "restaurant": 3, "fast_food": 1}
        return {
            "area": {
                "name": {"value": "Moritzplatz"},
                "area_km2": field(0.5),
                "boundary": {
                    "type": "Polygon",
                    "coordinates": [[[13.4, 52.5], [13.41, 52.5], [13.41, 52.51], [13.4, 52.5]]],
                },
            },
            "residents": {
                "total": field(4000),
                "density": field(8000.0),
                "age_bands": [
                    {"age_min": 18, "age_max_exclusive": 27, "count": field(800)},
                    {"age_min": 27, "age_max_exclusive": 45, "count": field(1200)},
                ],
            },
            "competitors": {
                "sources": [
                    {
                        "inventory": "mapped_places",
                        "count": {
                            "value": counts[category],
                            "sources": [{"dataset_id": "osm_pois", "snapshot_id": "o1"}],
                        },
                    },
                    {"inventory": "registered_businesses", "count": {"value": 12, "sources": []}},
                ]
            },
            "transit": {
                "windows": [
                    {
                        "window_start": "07:00",
                        "window_end": "10:00",
                        "scheduled_departures": field(100),
                    },
                    {
                        "window_start": "16:00",
                        "window_end": "19:00",
                        "scheduled_departures": field(50),
                    },
                    {
                        "window_start": "20:00",
                        "window_end": "23:00",
                        "scheduled_departures": field(999),
                    },
                ]
            },
        }

    names = {
        "02100104": {
            "name": "Moritzplatz",
            "bzr_name": "Südliche Friedrichstadt",
            "bzr_id": "021001",
            "pgr_id": "0210",
            "pgr_name": "Kreuzberg Nord",
        }
    }
    port = SummaryAreaData(summary, names)
    assert {"bezirk:02", "ortsteil:0210", "bezirksregion:021001", "plr:02100104"} <= {
        u.area_id for u in port.units()
    }
    assert port.resolve("ortsteil:0210")["name"] == "Kreuzberg"
    template = v2.Template(
        "coffee_v2",
        (
            *LAYERS,
            v2.LayerSpec("young_adult_share", "18–45", "+", 0.1),
            v2.LayerSpec("footfall_proxy", "POI", "+", 0.1),
            v2.LayerSpec("transit", "Transit", "+", 0.1),
            v2.LayerSpec("centre_proximity", "Centre", "-", 0.1),
        ),
    )
    ledger = EvidenceLedger(tmp_path)
    [cell] = port.cells("ortsteil:0210", template, ledger=ledger)
    assert cell.raw == {
        "residents_block_density": 8000.0,
        "cafe_saturation": 10.0,
        "young_adult_share": 0.5,
        "footfall_proxy": 12.0,
        "transit": 150.0,
        "centre_proximity": None,
    }
    assert port.unmapped_layers(template) == ["centre_proximity"]
    assert set(cell.layer_evidence) == {layer.id for layer in template.layers}
    assert ledger.get(cell.layer_evidence["cafe_saturation"])["dataset_id"] == "osm_pois"
    reasons = port.confidence_reasons("ortsteil:0210")["02100104"]
    assert any("fallback" in r for r in reasons)
    assert port.crosscheck("ortsteil:0210") == {"02100104": (4, 12)}
    assert port.geojson("ortsteil:0210")["features"][0]["id"] == "02100104"

    wired = SummaryAreaData(
        summary,
        names,
        block_densities={"02100104": 12400.0},
        block_snapshot_id="blocks-1",
        center_geometries=[{"type": "Point", "coordinates": [13.42, 52.52]}],
        center_snapshot_id="centers-1",
    )
    [wired_cell] = wired.cells("plr:02100104", template)
    assert wired_cell.raw["residents_block_density"] == 12400.0
    assert wired_cell.raw["centre_proximity"] > 0
    assert wired.unmapped_layers(template) == []
    wired_ledger = EvidenceLedger(tmp_path / "wired")
    [wired_cell] = wired.cells("plr:02100104", template, ledger=wired_ledger)
    density_evidence = wired_ledger.get(wired_cell.layer_evidence["residents_block_density"])
    center_evidence = wired_ledger.get(wired_cell.layer_evidence["centre_proximity"])
    assert (density_evidence["dataset_id"], density_evidence["snapshot_id"]) == (
        "berlin_population_density_blocks",
        "blocks-1",
    )
    assert (center_evidence["dataset_id"], center_evidence["snapshot_id"]) == (
        "berlin_centers",
        "centers-1",
    )


def test_area_run_names_budgets_and_factcheck_file(make_client, tmp_path):
    CALLS.clear()
    client = make_client()
    created = client.post("/research", json={"question": "Café in Britz?"}).json()
    auth = {"Authorization": f"Bearer {created['run_token']}"}
    assert client.post(f"/runs/{created['run_id']}/approve", json={}, headers=auth).json() == {
        "status": "approved"
    }
    _events(client, created["run_id"], auth)
    agents = [c[0] for c in CALLS if c[0] != "score_area"]
    assert {"researcher:wave1:1", "researcher:wave1:2", "researcher:wave1:3"} <= set(agents)
    assert all(c[1:] == (3, 3) for c in CALLS if c[0].startswith(("researcher:wave2:", "skeptic:")))
    assert any(c[0].startswith("researcher:wave2:") for c in CALLS) and any(
        c[0].startswith("skeptic:") for c in CALLS
    )
    scored = [c for c in CALLS if c[0] == "score_area"]
    assert all({"weights", "crosscheck", "risks"} <= set(c[1]) for c in scored)
    assert any(c[2] == ["housing_growth"] for c in scored)
    run_dir = tmp_path / created["run_id"]
    report = json.loads((run_dir / "factcheck.json").read_text())
    assert report["counts"]
    rows = [json.loads(line) for line in (run_dir / "findings.jsonl").read_text().splitlines()]
    risks = [r for r in rows if r["claim_type"] == "risk"]
    assert risks and all(r["agent"].startswith("skeptic:") and r["verification"] for r in risks)


def test_future_kind_maps_to_template_effects():
    from app.application.runs.pipeline import future_kind

    coffee, kita = v2.load_template("coffee_v2"), v2.load_template("kita_gap_v1")
    assert future_kind("housing", "dwellings", coffee) == "housing_growth"
    assert future_kind("cafe_opening", None, coffee) == "cafe_opening"
    assert future_kind("office", None, coffee) is None
    assert future_kind("kita_opening", "kita_places", kita) == "kita_opening"
    assert future_kind("housing", "children_0_6", kita) == "children_forecast_increase"


def test_gap_without_kita_data_reports_not_available(make_client):
    gap = v2.load_template("kita_gap_v1")
    client = make_client(scoring=fake_scoring(load_template=lambda _id: gap))
    created = client.post("/research", json={"question": "Wo fehlen Kitas in Britz?"}).json()
    assert created["shape"] == "gap" and created["status"] == "awaiting_approval"
    auth = {"Authorization": f"Bearer {created['run_token']}"}
    client.post(f"/runs/{created['run_id']}/approve", json={}, headers=auth)
    events = _events(client, created["run_id"], auth)
    error = next(e for e in events if e["type"] == "error")
    assert "not available yet" in error["detail"]["error"]
    assert events[-1]["detail"] == {"status": "failed"}


def test_point_flow_with_static_readers(tmp_path):
    from app import research
    from app.application import t1
    from app.application.t1.demo import demo_readers

    deps = Deps(
        scoring=v2,
        research=research,
        port=FixtureAreaData(UNITS, PLRS),
        checker_factory=lambda: FactChecker(offline=True),
        config_factory=lambda: research.ResearchConfig(fake=True),
        t1=t1,
        t1_readers=demo_readers,
    )
    runs_live.configure(RunService(deps, tmp_path))
    app = FastAPI()
    app.include_router(runs_live.router)
    try:
        with TestClient(app) as client:
            unknown = client.post(
                "/research", json={"question": "Was passt an die Wiener Straße 99?"}
            ).json()
            assert unknown["status"] == "needs_clarification" and unknown["shape"] == "point"
            created = client.post(
                "/research", json={"question": "Was passt an die Wiener Straße 12?"}
            ).json()
            assert created["shape"] == "point" and created["status"] == "awaiting_approval", created
            hypotheses = [h["id"] for h in created["plan"]["hypotheses"]]
            assert hypotheses
            auth = {"Authorization": f"Bearer {created['run_token']}"}
            client.post(
                f"/runs/{created['run_id']}/approve",
                json={"hypothesis_ids": hypotheses[:2]},
                headers=auth,
            )
            events = _events(client, created["run_id"], auth)
            assert events[-1]["detail"] == {"status": "done"}, [
                e for e in events if e["type"] == "error"
            ]
            agents = {e["agent"] for e in events}
            assert {f"researcher:t1:{h}" for h in hypotheses[:2]} <= agents
            assert {f"skeptic:{h}" for h in hypotheses[:2]} <= agents
            result = RunResult.model_validate(
                client.get(f"/runs/{created['run_id']}/result", headers=auth).json()
            )
            assert [h.id for h in result.candidates.hypotheses] and len(
                result.candidates.hypotheses
            ) == 2
            assert all(
                f.hypothesis_id in hypotheses[:2]
                for f in result.findings
                if f.claim_type != "dataset"
            )
            run_dir = tmp_path / created["run_id"]
            doc = json.loads((run_dir / "candidates.json").read_text())
            assert {"hypotheses"} <= set(doc) and (run_dir / "factcheck.json").exists()
            ledger = [
                json.loads(line) for line in (run_dir / "evidence.jsonl").read_text().splitlines()
            ]
            latest = {e["evidence_id"]: e for e in ledger}
            datasets = [e for e in latest.values() if e.get("method") == "dataset"]
            assert datasets and all(e["status"] == "partial" for e in datasets)
            fit_dims = {d.name for h in result.candidates.hypotheses for d in h.dimensions}
            assert {"Cost", "Operator fit"} <= fit_dims
    finally:
        runs_live.configure(None)
