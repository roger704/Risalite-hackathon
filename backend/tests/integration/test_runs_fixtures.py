import json
import re

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.api.runs_fixtures import RUNS
from app.contracts.api import ResearchResponse, RunResult


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("RISALITE_REPLAY_DELAY", "0")
    monkeypatch.delenv("RISALITE_RUNS", raising=False)
    RUNS.clear()
    with TestClient(create_app()) as test_client:
        yield test_client
    RUNS.clear()


def start_run(
    client: TestClient, question="Where is it smart to open a coffee place in Kreuzberg?"
):
    response = client.post("/research", json={"question": question, "role": "operator"})
    assert response.status_code == 200
    return ResearchResponse.model_validate(response.json()).model_dump()


def auth(run: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {run['run_token']}"}


def test_run_endpoints_hide_missing_invalid_and_unknown_tokens(client):
    run = start_run(client)
    run_id = run["run_id"]
    missing = client.get(f"/runs/{run_id}/result")
    wrong = client.get(f"/runs/{run_id}/result", headers={"Authorization": "Bearer wrong"})
    unknown = client.get("/runs/not-a-run/result", headers=auth(run))
    assert missing.status_code == wrong.status_code == unknown.status_code == 404
    assert missing.json() == wrong.json() == unknown.json()


def test_needs_clarification_can_be_resolved_and_areas_are_searchable(client):
    run = start_run(client, "Where is it smart to open a coffee place?")
    assert run["status"] == "needs_clarification"
    assert run["clarify_question"]

    response = client.post(
        f"/runs/{run['run_id']}/clarify",
        headers=auth(run),
        json={"answer": "Kreuzberg"},
    )
    assert response.status_code == 200
    clarified = response.json()
    assert clarified["status"] == "awaiting_approval"
    assert clarified["run_token"] == run["run_token"]
    assert clarified["plan"]["area"]["area_id"] == "ortsteil:0202"
    assert len(clarified["plan"]["sub_questions"]) <= 3

    areas = client.get(
        f"/runs/{run['run_id']}/areas",
        headers=auth(run),
        params={"q": "kreuz"},
    )
    assert areas.status_code == 200
    assert any(area["area_id"] == "ortsteil:0202" for area in areas.json())


def test_approve_validates_layers_weights_subquestions_and_area(client):
    run = start_run(client)
    run_url = f"/runs/{run['run_id']}/approve"
    headers = auth(run)
    invalid_payloads = [
        {"layers": [{"id": "not-a-layer", "enabled": True, "weight": 0.5}]},
        {"layers": [{"id": "demand_density", "enabled": True, "weight": 1.5}]},
        {"subquestions": ["s1", "s2", "s3", "s4"]},
        {"area_id": "not-an-area"},
    ]
    for payload in invalid_payloads:
        response = client.post(run_url, headers=headers, json=payload)
        assert response.status_code == 422


def test_changed_area_requires_review_and_approval_is_one_shot(client):
    run = start_run(client)
    url = f"/runs/{run['run_id']}/approve"
    headers = auth(run)

    changed = client.post(
        url,
        headers=headers,
        json={"area_id": "bezirk:02", "subquestions": ["s1"]},
    )
    assert changed.status_code == 200
    body = changed.json()
    assert body["status"] == "awaiting_approval"
    assert body["plan"]["area"]["area_id"] == "bezirk:02"
    assert "fixture data covers Kreuzberg only" in body["plan"]["note"]

    approved = client.post(
        url,
        headers=headers,
        json={"area_id": "bezirk:02", "subquestions": ["s1"]},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    assert client.post(url, headers=headers, json={}).status_code == 409


def test_result_status_sse_replay_and_result_contract(client):
    run = start_run(client)
    run_id = run["run_id"]
    headers = auth(run)
    result_url = f"/runs/{run_id}/result"

    before_approval = client.get(result_url, headers=headers)
    assert before_approval.status_code == 202
    assert before_approval.json()["status"] == "awaiting_approval"

    response = client.post(
        f"/runs/{run_id}/approve",
        headers=headers,
        json={
            "area_id": "ortsteil:0202",
            "subquestions": ["s1"],
            "layers": [
                {"id": "demand_density", "enabled": False, "weight": 0.2},
                {"id": "young_share", "enabled": True, "weight": 0.6},
            ],
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "approved"

    events = client.get(f"/runs/{run_id}/events", headers=headers)
    assert events.status_code == 200
    blocks = [block for block in events.text.split("\n\n") if block.strip()]
    ids = [int(re.search(r"^id:\s*(\d+)$", block, re.MULTILINE).group(1)) for block in blocks]
    payloads = [
        json.loads(re.search(r"^data:\s*(.+)$", block, re.MULTILINE).group(1)) for block in blocks
    ]
    assert ids == sorted(ids)
    assert len(ids) == len(set(ids))
    assert payloads[0]["type"] == "approved"
    assert payloads[0]["detail"]["layers"][0]["enabled"] is False

    resumed = client.get(
        f"/runs/{run_id}/events",
        headers={**headers, "Last-Event-ID": str(ids[0])},
    )
    resumed_ids = [int(value) for value in re.findall(r"^id:\s*(\d+)$", resumed.text, re.MULTILINE)]
    assert resumed_ids
    assert min(resumed_ids) > ids[0]

    result = client.get(result_url, headers=headers)
    assert result.status_code == 200
    validated = RunResult.model_validate(result.json())
    assert validated.status == "done"
    assert validated.cells is not None
    layers = {layer.id: layer for layer in validated.cells.layers}
    assert layers["demand_density"].enabled is False
    assert layers["demand_density"].weight_default == 0.2
    assert layers["young_share"].weight_default == 0.6


def test_root_serves_dashboard_index(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Risalite" in response.text
    assert "app.js" in response.text
