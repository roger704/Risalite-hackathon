import json
import re
import runpy
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.main import create_app
from app.contracts.api import ResearchResponse, RunResult
from app.contracts.candidates import CandidatesDocument
from app.contracts.cells import CellsDocument
from app.contracts.events import ResearchEvent
from app.contracts.evidence import EvidenceEntry

FIXTURES = Path(__file__).resolve().parents[3] / "frontend" / "fixtures"
APP = FIXTURES.parent / "app"


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_frontend_static_routes_and_health(tmp_path, monkeypatch):
    frontend_root = tmp_path / "frontend"
    frontend_root.mkdir()
    (frontend_root / "app").symlink_to(APP, target_is_directory=True)
    (frontend_root / "fixtures").symlink_to(FIXTURES, target_is_directory=True)
    monkeypatch.setenv("RISALITE_FRONTEND_DIR", str(frontend_root))
    client = TestClient(create_app(data_dir=tmp_path))

    response = client.get("/")
    assert response.status_code == 200
    assert "Risalite" in response.text
    assert client.get("/fixtures/cells.json").status_code == 200
    for asset in (
        "/app.js",
        "/app.css",
        "/scoring.js",
        "/vendor/leaflet/leaflet.js",
        "/vendor/leaflet/leaflet.css",
        "/vendor/leaflet/images/layers.png",
        "/vendor/leaflet/images/layers-2x.png",
        "/vendor/leaflet/images/marker-icon.png",
        "/vendor/leaflet/images/marker-icon-2x.png",
        "/vendor/leaflet/images/marker-shadow.png",
    ):
        assert client.get(asset).status_code == 200
    assert client.get("/health").json() == {"status": "ok"}


def test_fixture_models_validate_and_cross_references_are_complete():
    cells_raw = _json(FIXTURES / "cells.json")
    cells = CellsDocument.model_validate(cells_raw)
    evidence_raw = _jsonl(FIXTURES / "evidence.jsonl")
    evidence = [EvidenceEntry.model_validate(entry) for entry in evidence_raw]
    events = [ResearchEvent.model_validate(event) for event in _jsonl(FIXTURES / "trace.jsonl")]
    point_events_raw = _jsonl(FIXTURES / "trace_point.jsonl")
    point_events = [ResearchEvent.model_validate(event) for event in point_events_raw]
    candidates_raw = _json(FIXTURES / "candidates.json")
    candidates = CandidatesDocument.model_validate(candidates_raw)
    plans = [
        ResearchResponse.model_validate(_json(FIXTURES / f"plan_{shape}.json"))
        for shape in ("location", "point")
    ]
    plan_raw = [_json(FIXTURES / f"plan_{shape}.json") for shape in ("location", "point")]
    results_raw = [_json(FIXTURES / f"result_{shape}.json") for shape in ("location", "point")]
    results = [RunResult.model_validate(result) for result in results_raw]

    evidence_ids = {entry.evidence_id for entry in evidence}
    references = {layer.evidence_id for layer in cells.layers}
    for cell in cells.cells:
        references.update(modifier.evidence_id for modifier in cell.modifiers)
        references.update(risk.evidence_id for risk in cell.risks)
        assert all(value is None or 0 <= value <= 1 for value in cell.values.values())
    references.update(event_id for event in events for event_id in event.evidence_ids)
    references.update(event_id for event in point_events for event_id in event.evidence_ids)
    references.add(candidates.space.evidence_id)
    references.update(competitor.evidence_id for competitor in candidates.competitors)
    for hypothesis in candidates.hypotheses:
        for dimension in hypothesis.dimensions:
            references.update(dimension.evidence_ids)
        references.update(
            claim.evidence_id
            for claim in [*hypothesis.for_, *hypothesis.against]
            if claim.evidence_id is not None
        )
    for plan in plans:
        if plan.plan:
            references.update(layer.evidence_id for layer in plan.plan.layers)
    for document in plan_raw:
        for hypothesis in document["plan"]["hypotheses"]:
            for dimension in hypothesis["dimensions"]:
                references.update(dimension["evidence_ids"])
            for claim in [*hypothesis["for"], *hypothesis["against"]]:
                if claim["evidence_id"] is not None:
                    references.add(claim["evidence_id"])
    for result in results:
        references.update(re.findall(r"\[(e\d+)\]", result.summary))
    assert references <= evidence_ids

    assert cells.area.cell_count == len(cells.cells)
    geojson = _json(FIXTURES / "cells.geojson")
    feature_ids = {feature["id"] for feature in geojson["features"]}
    assert {cell.id for cell in cells.cells} <= feature_ids
    assert all(feature["properties"]["id"] == feature["id"] for feature in geojson["features"])

    scoring = runpy.run_path(str(FIXTURES / "build_fixtures.py"))["compute_scores"]
    expected = scoring(cells_raw["cells"], cells_raw["layers"])
    for cell in cells.cells:
        assert abs(cell.score_present - expected[cell.id]["present"]) < 1e-6
        assert abs(cell.score_future - expected[cell.id]["future"]) < 1e-6

    ranked_names = [
        cell["name"]
        for cell in sorted(
            cells_raw["cells"],
            key=lambda cell: expected[cell["id"]]["present"],
            reverse=True,
        )[:3]
    ]
    assert ranked_names == [
        "Reichenberger Straße Ost",
        "Lausitzer Platz",
        "Graefekiez Nord",
    ]
    ranking_sentence = (
        f"The strongest default locations are {ranked_names[0]}, {ranked_names[1]}, and "
        f"{ranked_names[2]}, ranked from the PLR cell mix [e1]."
    )
    assert ranking_sentence in results_raw[0]["summary"]

    assert results_raw[1]["run_id"] == "mock-t1"
    assert events[-1].type == "done"
    assert events[-1].stage == "done"
    assert 12 <= len(point_events) <= 16
    assert {event.stage for event in point_events} >= {
        "classify",
        "plan",
        "research",
        "skeptic",
        "fact-check",
        "write",
        "done",
    }
    assert all(event.run_id == "mock-t1" for event in point_events)
    assert point_events[-1].type == "done"
    assert point_events[-1].stage == "done"
    assert all(
        document["synthetic"] is True for document in [cells_raw, candidates_raw, *results_raw]
    )
    assert all(entry["synthetic"] is True for entry in evidence_raw)
    assert all(event["synthetic"] is True for event in _jsonl(FIXTURES / "trace.jsonl"))
    assert all(event["synthetic"] is True for event in point_events_raw)
