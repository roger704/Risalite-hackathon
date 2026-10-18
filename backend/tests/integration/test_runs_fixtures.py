import copy
import json
import re

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.api.runs_fixtures import FIXTURE_NOTICE, IDLE_TTL, RUNS
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


def finish_run(
    client: TestClient, question="Where is it smart to open a coffee place in Kreuzberg?"
):
    run = start_run(client, question)
    headers = auth(run)
    approved = client.post(f"/runs/{run['run_id']}/approve", headers=headers, json={})
    assert approved.status_code == 200
    result = client.get(f"/runs/{run['run_id']}/result", headers=headers)
    assert result.status_code == 200
    return run, headers


def test_run_endpoints_hide_missing_invalid_and_unknown_tokens(client):
    run = start_run(client)
    run_id = run["run_id"]
    missing = client.get(f"/runs/{run_id}/result")
    wrong = client.get(f"/runs/{run_id}/result", headers={"Authorization": "Bearer wrong"})
    unknown = client.get("/runs/not-a-run/result", headers=auth(run))
    assert missing.status_code == wrong.status_code == unknown.status_code == 404
    assert missing.json() == wrong.json() == unknown.json()


def test_research_rejects_question_over_2000_characters(client):
    response = client.post("/research", json={"question": "x" * 2001})
    assert response.status_code == 422


@pytest.mark.parametrize("field", ["role", "profile", "template_hint"])
def test_research_optional_fields_are_limited_to_2000_characters(client, field):
    response = client.post(
        "/research",
        json={"question": "Where in Kreuzberg?", field: "x" * 2001},
    )
    assert response.status_code == 422


def test_run_store_evicts_done_runs_before_active_runs_and_rejects_when_full(client, monkeypatch):
    monkeypatch.setattr("app.api.runs_fixtures.MAX_RUNS", 2)
    active = start_run(client)
    done = start_run(client)
    done_headers = auth(done)
    assert (
        client.post(f"/runs/{done['run_id']}/approve", headers=done_headers, json={}).status_code
        == 200
    )
    assert client.get(f"/runs/{done['run_id']}/result", headers=done_headers).status_code == 200

    newer = client.post(
        "/research",
        json={"question": "Where is it smart to open a coffee place in Kreuzberg?"},
    )
    assert newer.status_code == 200
    newer_run = newer.json()
    assert client.get(f"/runs/{active['run_id']}/result", headers=auth(active)).status_code == 202
    assert client.get(f"/runs/{done['run_id']}/result", headers=done_headers).status_code == 404

    full = client.post(
        "/research",
        json={"question": "Where in Kreuzberg should a new cafe open?"},
    )
    assert full.status_code == 503
    assert full.json() == {"detail": "fixture run store is full"}
    assert set(RUNS) == {active["run_id"], newer_run["run_id"]}


def test_run_store_evicts_failed_runs(client, monkeypatch):
    monkeypatch.setattr("app.api.runs_fixtures.MAX_RUNS", 1)
    failed = start_run(client)
    RUNS[failed["run_id"]]["status"] = "failed"

    response = client.post(
        "/research",
        json={"question": "Where should a coffee shop open in Neukölln?"},
    )

    assert response.status_code == 200
    assert failed["run_id"] not in RUNS
    assert (
        client.get(
            f"/runs/{failed['run_id']}/result",
            headers=auth(failed),
        ).status_code
        == 404
    )


def test_expired_approved_runs_are_evicted_but_fresh_ones_are_preserved(client, monkeypatch):
    monkeypatch.setattr("app.api.runs_fixtures.MAX_RUNS", 1)
    stale = start_run(client)
    stale_headers = auth(stale)
    assert (
        client.post(f"/runs/{stale['run_id']}/approve", headers=stale_headers, json={}).status_code
        == 200
    )
    RUNS[stale["run_id"]]["approved_at"] -= IDLE_TTL + 1

    new = client.post(
        "/research",
        json={"question": "Where in Kreuzberg should another cafe open?"},
    )
    assert new.status_code == 200
    assert (
        client.get(
            f"/runs/{stale['run_id']}/areas",
            headers=stale_headers,
        ).status_code
        == 404
    )

    fresh = new.json()
    fresh_headers = auth(fresh)
    assert (
        client.post(f"/runs/{fresh['run_id']}/approve", headers=fresh_headers, json={}).status_code
        == 200
    )
    approved_at = RUNS[fresh["run_id"]]["approved_at"]
    full = client.post(
        "/research",
        json={"question": "Where should a cafe open in Neukölln?"},
    )

    assert full.status_code == 503
    assert full.json() == {"detail": "fixture run store is full"}
    assert RUNS[fresh["run_id"]]["status"] == "approved"
    assert RUNS[fresh["run_id"]]["approved_at"] == approved_at


def test_expired_pending_run_is_evicted(client, monkeypatch):
    monkeypatch.setattr("app.api.runs_fixtures.MAX_RUNS", 1)
    expired = start_run(client)
    RUNS[expired["run_id"]]["created_at"] -= IDLE_TTL + 1

    response = client.post(
        "/research",
        json={"question": "Where in Neukölln should a new cafe open?"},
    )

    assert response.status_code == 200
    assert (
        client.get(
            f"/runs/{expired['run_id']}/result",
            headers=auth(expired),
        ).status_code
        == 404
    )


def test_needs_clarification_can_be_resolved_and_areas_are_searchable(client):
    run = start_run(client, "Where is it smart to open a coffee place?")
    assert run["status"] == "needs_clarification"
    assert run["clarify_question"]
    created_at = RUNS[run["run_id"]]["created_at"]

    too_long = client.post(
        f"/runs/{run['run_id']}/clarify",
        headers=auth(run),
        json={"answer": "x" * 501},
    )
    assert too_long.status_code == 422
    assert RUNS[run["run_id"]]["created_at"] == created_at

    response = client.post(
        f"/runs/{run['run_id']}/clarify",
        headers=auth(run),
        json={"answer": "Kreuzberg"},
    )
    assert response.status_code == 200
    assert RUNS[run["run_id"]]["created_at"] > created_at
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
    created_at = RUNS[run["run_id"]]["created_at"]

    changed = client.post(
        url,
        headers=headers,
        json={"area_id": "bezirk:02", "subquestions": ["s1"]},
    )
    assert changed.status_code == 200
    body = changed.json()
    assert body["status"] == "awaiting_approval"
    assert body["plan"]["area"]["area_id"] == "bezirk:02"
    assert body["plan"]["note"] == FIXTURE_NOTICE
    assert RUNS[run["run_id"]]["created_at"] > created_at
    created_at = RUNS[run["run_id"]]["created_at"]

    approved = client.post(
        url,
        headers=headers,
        json={"area_id": "bezirk:02", "subquestions": ["s1"]},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    assert RUNS[run["run_id"]]["created_at"] > created_at
    result = client.get(f"/runs/{run['run_id']}/result", headers=headers)
    assert result.status_code == 200
    assert result.json()["fixture_notice"] == FIXTURE_NOTICE
    assert client.post(url, headers=headers, json={}).status_code == 409


@pytest.mark.parametrize("hypothesis_ids", [[], ["not-a-hypothesis"], None])
def test_point_approval_rejects_invalid_hypotheses_without_state_change(client, hypothesis_ids):
    run = start_run(
        client,
        "This storefront at Wiener Straße 12 just became available — what is the best use?",
    )
    created_at = RUNS[run["run_id"]]["created_at"]
    response = client.post(
        f"/runs/{run['run_id']}/approve",
        headers=auth(run),
        json={"hypothesis_ids": hypothesis_ids},
    )

    assert response.status_code == 422
    assert RUNS[run["run_id"]]["status"] == "awaiting_approval"
    assert "approval" not in RUNS[run["run_id"]]
    assert RUNS[run["run_id"]]["created_at"] == created_at


def test_changed_unsupported_area_discloses_fixture_notice(client):
    run = start_run(client)
    url = f"/runs/{run['run_id']}/approve"
    headers = auth(run)

    changed = client.post(url, headers=headers, json={"area_id": "bezirk:08"})
    assert changed.status_code == 200
    assert changed.json()["status"] == "awaiting_approval"
    notice = changed.json()["plan"]["note"]

    approved = client.post(url, headers=headers, json={"area_id": "bezirk:08"})
    assert approved.status_code == 200
    result = client.get(f"/runs/{run['run_id']}/result", headers=headers)

    assert result.status_code == 200
    assert result.json()["fixture_notice"] == notice


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
    assert result.json()["fixture_notice"] is None
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
    assert "SpotWise" in response.text
    assert "app.js" in response.text


def test_point_result_does_not_include_area_findings(client):
    run = start_run(
        client,
        "This storefront at Wiener Straße 12 just became available — what is the best use?",
    )
    headers = auth(run)
    approved = client.post(f"/runs/{run['run_id']}/approve", headers=headers, json={})
    assert approved.status_code == 200

    result = client.get(f"/runs/{run['run_id']}/result", headers=headers)

    assert result.status_code == 200
    assert result.json()["findings"] == []


def test_point_result_report_matches_approved_hypotheses(client):
    run = start_run(
        client,
        "This storefront at Wiener Straße 12 just became available — what is the best use?",
    )
    second_hypothesis_id = run["plan"]["hypotheses"][1]["id"]
    headers = auth(run)
    approved = client.post(
        f"/runs/{run['run_id']}/approve",
        headers=headers,
        json={"hypothesis_ids": [second_hypothesis_id]},
    )
    assert approved.status_code == 200

    result = client.get(f"/runs/{run['run_id']}/result", headers=headers)

    assert result.status_code == 200
    body = result.json()
    assert [hypothesis["id"] for hypothesis in body["candidates"]["hypotheses"]] == [
        second_hypothesis_id
    ]
    assert "Natural-wine bar" in body["report_md"]
    assert "Craft bakery-café" not in body["report_md"]
    assert "[21]" in body["report_md"]
    assert body["fixture_notice"] == (
        "Summary regenerated from the fixture for your approved hypotheses."
    )


def test_gap_run_discloses_fixture_notice(client):
    run = start_run(client, "Where are Kitas missing in Neukölln?")
    assert run["shape"] == "gap"
    assert run["status"] == "awaiting_approval"
    assert run["plan"]["note"] == (
        "Demo fixtures only cover the Kreuzberg coffee question — results for this question/area "
        "will show that example, not a computed answer."
    )
    headers = auth(run)
    approved = client.post(f"/runs/{run['run_id']}/approve", headers=headers, json={})
    assert approved.status_code == 200

    result = client.get(f"/runs/{run['run_id']}/result", headers=headers)

    assert result.status_code == 200
    assert result.json()["fixture_notice"] == run["plan"]["note"]


def test_fixture_exports_require_auth_and_unknown_formats_are_not_found(client):
    run = start_run(client)
    path = f"/runs/{run['run_id']}/export/report.md"

    missing = client.get(path)
    wrong = client.get(path, headers={"Authorization": "Bearer wrong"})
    unknown_run = client.get("/runs/not-a-run/export/report.md", headers=auth(run))
    unknown_format = client.get(
        f"/runs/{run['run_id']}/export/nope",
        headers=auth(run),
    )

    assert missing.status_code == wrong.status_code == unknown_run.status_code == 404
    assert unknown_format.status_code == 404
    assert unknown_format.json()["error"]["code"] == "UNKNOWN_FORMAT"


@pytest.mark.parametrize(
    ("question", "shape"),
    [
        ("Where are Kitas missing in Neukölln?", "gap"),
        ("Where should a coffee place open in Neukölln?", "location"),
    ],
)
def test_fixture_notice_is_in_markdown_and_html_exports(client, question, shape):
    run, headers = finish_run(client, question)
    assert run["shape"] == shape

    result = client.get(f"/runs/{run['run_id']}/result", headers=headers).json()
    assert result["fixture_notice"] == FIXTURE_NOTICE

    for fmt in ("report.md", "report.html"):
        response = client.get(f"/runs/{run['run_id']}/export/{fmt}", headers=headers)

        assert response.status_code == 200
        assert FIXTURE_NOTICE in response.text


def test_fixture_markdown_export_lists_approved_subquestions(client):
    run, headers = finish_run(client)
    stored_plan = copy.deepcopy(RUNS[run["run_id"]]["plan"])

    response = client.get(
        f"/runs/{run['run_id']}/export/report.md",
        headers=headers,
    )

    assert response.status_code == 200
    assert all(subquestion["text"] in response.text for subquestion in stored_plan["sub_questions"])
    assert RUNS[run["run_id"]]["plan"] == stored_plan


def test_fixture_markdown_export_attributes_trace_roles_and_tokens(client):
    run, headers = finish_run(client)
    stored_trace = copy.deepcopy(RUNS[run["run_id"]]["result"]["trace"])

    response = client.get(
        f"/runs/{run['run_id']}/export/report.md",
        headers=headers,
    )

    assert response.status_code == 200
    rows = [line for line in response.text.splitlines() if line.startswith("| Classifier |")]
    assert rows
    assert int(rows[0].split("|")[4].strip()) > 0
    assert not any(
        line.split("|")[1].strip().lower() == "unknown"
        for line in response.text.splitlines()
        if line.startswith("|")
    )
    assert RUNS[run["run_id"]]["result"]["trace"] == stored_trace


def test_fixture_export_rejects_runs_before_replay_finishes(client, monkeypatch):
    run = start_run(client)
    headers = auth(run)
    approved = client.post(f"/runs/{run['run_id']}/approve", headers=headers, json={})
    assert approved.status_code == 200
    monkeypatch.setenv("RISALITE_REPLAY_DELAY", "1")

    response = client.get(f"/runs/{run['run_id']}/export/report.md", headers=headers)

    assert response.status_code == 409
    assert response.json() == {"detail": "run not finished"}


@pytest.mark.parametrize(
    "fmt,media_type,disposition",
    [
        ("report.md", "text/markdown; charset=utf-8", "attachment"),
        ("report.html", "text/html; charset=utf-8", "inline"),
        ("sources.csv", "text/csv; charset=utf-8", "attachment"),
        ("sources.bib", "application/x-bibtex; charset=utf-8", "attachment"),
    ],
)
def test_fixture_exports_return_expected_formats_and_headers(client, fmt, media_type, disposition):
    run, headers = finish_run(client)

    response = client.get(f"/runs/{run['run_id']}/export/{fmt}", headers=headers)

    assert response.status_code == 200
    assert response.headers["content-type"] == media_type
    assert response.headers["content-disposition"] == (
        f'{disposition}; filename="risalite-{run["run_id"]}-{fmt}"'
    )
    if fmt == "sources.csv":
        assert response.content.startswith(b"\xef\xbb\xbf")


def test_fixture_markdown_export_has_question_and_numbered_citations(client):
    run, headers = finish_run(client)

    response = client.get(
        f"/runs/{run['run_id']}/export/report.md",
        headers=headers,
    )

    assert response.status_code == 200
    assert run["question"] in response.text
    assert re.search(r"\[\d+\]", response.text)
    assert not re.search(r"\[e\d+\]", response.text)


def test_fixture_export_applies_layer_mix_and_rejects_unknown_layers(client):
    run, headers = finish_run(client)
    path = f"/runs/{run['run_id']}/export/report.md"

    changed = client.get(
        path,
        headers=headers,
        params={"layers": "competition_osm", "mode": "future"},
    )
    unknown = client.get(
        path,
        headers=headers,
        params={"layers": "not-a-layer"},
    )

    assert changed.status_code == 200
    assert "Mix: custom" in changed.text
    assert unknown.status_code == 422
    assert unknown.json()["error"]["code"] == "UNKNOWN_LAYER"


def test_point_fixture_export_ignores_area_mix(client):
    run, headers = finish_run(
        client,
        "This storefront at Wiener Straße 12 just became available — what is the best use?",
    )

    response = client.get(
        f"/runs/{run['run_id']}/export/report.md",
        headers=headers,
        params={"layers": "not-a-layer", "mode": "future"},
    )

    assert run["shape"] == "point"
    assert response.status_code == 200
    assert run["question"] in response.text
    assert "[12]" in response.text
    assert "[20]" in response.text
    assert "[22]" not in response.text
