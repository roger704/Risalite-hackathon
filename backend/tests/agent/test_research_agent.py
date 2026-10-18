import re

import pytest
from fastapi.testclient import TestClient

from app.agent.classify import classify
from app.agent.events import EventBus
from app.agent.mock import MockModelClient, MockResearch
from app.agent.pipeline import Orchestrator, strip_unknown_citations
from app.agent.stand_ins import FixtureCellScorer, PassThroughFactChecker
from app.api.main import create_app
from app.contracts.api import ApproveRequest, ResearchRequest
from app.domain.research import ResearchBudget


def _completed_client(tmp_path):
    client = TestClient(create_app(runs_dir=tmp_path, mock=True))
    created = client.post(
        "/research",
        json={"question": "Where is it smart to open a coffee place in Kreuzberg?"},
    )
    assert created.status_code == 201
    assert created.json()["classification"]["shape"] == "location"
    assert created.json()["classification"]["area"] == "Friedrichshain-Kreuzberg"
    assert len(created.json()["plan"]["paid_sources"]) == 2
    run_id = created.json()["run_id"]
    approved = client.post(
        f"/runs/{run_id}/approve",
        json={"layers": [{"id": "cost", "enabled": False}]},
    )
    assert approved.status_code == 202
    return client, run_id


def test_api_flow_and_citations(tmp_path):
    client, run_id = _completed_client(tmp_path)
    result = client.get(f"/runs/{run_id}/result").json()
    assert result["status"] == "done"
    assert result["cells"]["area"]["cell_count"] == 19
    assert result["report_md"]
    ids = {entry["evidence_id"] for entry in result["evidence"]}
    assert set(re.findall(r"\[(e\d+)\]", result["report_md"])) <= ids
    assert all(layer["evidence_id"] in ids for layer in result["cells"]["layers"])
    assert all(
        modifier["evidence_id"] in ids
        for cell in result["cells"]["cells"]
        for modifier in cell["modifiers"]
    )
    stages = [event["stage"] for event in result["trace"]]
    expected = ["classify", "plan", "approve", "research", "skeptic", "rescore", "write", "done"]
    for earlier, later in zip(expected, expected[1:]):
        assert stages.index(earlier) < stages.index(later)


def test_sse_replay_and_after(tmp_path):
    client, run_id = _completed_client(tmp_path)
    body = client.get(f"/runs/{run_id}/events").text
    ids = [int(value) for value in re.findall(r"^id: (\d+)$", body, re.M)]
    assert ids == list(range(1, len(ids) + 1))
    assert "event: classify" in body and "event: done" in body
    later = client.get(f"/runs/{run_id}/events?after=2").text
    later_ids = [int(value) for value in re.findall(r"^id: (\d+)$", later, re.M)]
    assert later_ids and min(later_ids) == 3
    header = client.get(f"/runs/{run_id}/events", headers={"Last-Event-ID": "3"}).text
    header_ids = [int(value) for value in re.findall(r"^id: (\d+)$", header, re.M)]
    assert min(header_ids) == 4


def test_skeptic_demotes_with_verbatim_evidence(tmp_path):
    client, run_id = _completed_client(tmp_path)
    result = client.get(f"/runs/{run_id}/result").json()
    rescore = next(event for event in result["trace"] if event["stage"] == "rescore")
    assert rescore["data"]["before_rank"] == 1
    assert rescore["data"]["after_rank"] > 1
    cell = next(
        cell for cell in result["cells"]["cells"] if cell["id"] == rescore["data"]["cell_id"]
    )
    risk = next(risk for risk in cell["risks"] if risk["severity"] == "high")
    evidence = next(
        item for item in result["evidence"] if item["evidence_id"] == risk["evidence_id"]
    )
    assert cell["demoted"] and risk["quote"] == evidence["quote"]


def test_citation_stripping():
    assert strip_unknown_citations("A [e1] B [e99] C [3]", {"e1"}) == "A [e1] B  C "


@pytest.mark.asyncio
async def test_missing_research_still_runs_model_writer(tmp_path):
    orchestrator = Orchestrator(
        tmp_path,
        EventBus(),
        MockModelClient(),
        None,
        FixtureCellScorer,
        PassThroughFactChecker(),
    )
    state = await orchestrator.start(
        ResearchRequest(question="Where is it smart to open a coffee place in Kreuzberg?")
    )
    await orchestrator.approve(state.run_id, ApproveRequest())
    await orchestrator.execute(state.run_id)

    events = orchestrator.events(state.run_id)
    assert any(event.type == "skip" and "web research skipped" in event.why for event in events)
    assert any(event.type == "llm" and event.role == "writer" for event in events)
    assert orchestrator.get_state(state.run_id).status == "done"


@pytest.mark.asyncio
async def test_budget_exhaustion_still_completes(tmp_path):
    orchestrator = Orchestrator(
        tmp_path,
        EventBus(),
        MockModelClient(),
        MockResearch(),
        FixtureCellScorer,
        PassThroughFactChecker(),
        lambda: ResearchBudget(2, 10, 12, 60000),
    )
    state = await orchestrator.start(
        ResearchRequest(question="Where is it smart to open a coffee place in Kreuzberg?")
    )
    await orchestrator.approve(state.run_id, ApproveRequest())
    await orchestrator.execute(state.run_id)
    assert orchestrator.get_state(state.run_id).status == "done"
    assert any(event.type == "budget_exhausted" for event in orchestrator.events(state.run_id))
    assert orchestrator.research.searches <= 2


@pytest.mark.asyncio
async def test_classification_clarifications():
    point = await classify("Analyze this storefront for a coffee place")
    assert point.shape == "point" and point.needs_clarification
    gap = await classify("Where are Kitas missing in Neukölln?")
    assert (gap.shape, gap.template, gap.area) == ("gap", "kita", "Neukölln")
    unknown_area = await classify("Where is a good coffee place?")
    assert unknown_area.needs_clarification and unknown_area.area is None


def test_unknown_run_and_double_approval(tmp_path):
    client = TestClient(create_app(runs_dir=tmp_path, mock=True))
    assert client.post("/runs/unknown/approve").status_code == 404
    assert client.get("/runs/unknown/result").status_code == 404
    created = client.post(
        "/research",
        json={"question": "Where is it smart to open a coffee place in Kreuzberg?"},
    ).json()
    run_id = created["run_id"]
    assert client.post(f"/runs/{run_id}/approve", json={}).status_code == 202
    assert client.post(f"/runs/{run_id}/approve", json={}).status_code == 409
