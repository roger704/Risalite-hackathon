import re
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.agent.classify import classify
from app.agent.events import EventBus, TraceWriter
from app.agent.live import NebiusModelClient
from app.agent.mock import MockModelClient, MockResearch
from app.agent.pipeline import Orchestrator, strip_unknown_citations
from app.agent.schemas import WriterOutput
from app.agent.stand_ins import FixtureCellScorer, PassThroughFactChecker
from app.api.main import create_app
from app.contracts.api import ApproveRequest, Classification, ResearchRequest, RunState
from app.contracts.events import ResearchEvent
from app.contracts.evidence import EvidenceEntry
from app.domain.research import ResearchBudget


def test_evidence_entry_matches_s4_ledger_fields():
    entry = EvidenceEntry(
        evidence_id="e1",
        method="tavily-search",
        url="https://example.org/source",
        quote="Verbatim source snippet.",
        tier="weak",
        retrieved_at=datetime.now(timezone.utc),
        source_ref={"web_evidence_id": "source-1"},
        revision=1,
        provider_tag="retained",
    )

    assert entry.model_dump()["provider_tag"] == "retained"
    assert entry.source_ref == {"web_evidence_id": "source-1"}
    assert entry.revision == 1
    with pytest.raises(ValidationError):
        EvidenceEntry.model_validate(entry.model_dump() | {"quote": ""})
    with pytest.raises(ValidationError):
        EvidenceEntry.model_validate(entry.model_dump() | {"method": "web"})


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
    assert client.get("/runs/..%2Fx/result").status_code == 404
    assert client.get("/runs/..%2Fx/events").status_code == 404
    assert client.post("/runs/invalid/approve", json={}).status_code == 404
    created = client.post(
        "/research",
        json={"question": "Where is it smart to open a coffee place in Kreuzberg?"},
    ).json()
    run_id = created["run_id"]
    assert client.post(f"/runs/{run_id}/approve", json={}).status_code == 202
    assert client.post(f"/runs/{run_id}/approve", json={}).status_code == 409


@pytest.mark.asyncio
async def test_model_classification_cannot_skip_missing_area_clarification(tmp_path):
    class MissingAreaModel:
        async def structured(self, role, operation, context, output_model, evidence=()):
            assert output_model is Classification
            return (
                Classification(
                    shape="location",
                    template="coffee",
                    area=None,
                    why="Model classified the request.",
                    needs_clarification=False,
                ),
                10,
            )

        def model_name(self, role):
            return "missing-area-test"

    orchestrator = Orchestrator(tmp_path, EventBus(), MissingAreaModel(), None)
    state = await orchestrator.start(
        ResearchRequest(question="Where is it smart to open a coffee place?")
    )

    assert state.status == "needs_clarification"
    assert state.classification.needs_clarification
    assert "required location field is missing" in state.classification.why


@pytest.mark.asyncio
async def test_rotated_subquestions_cover_all_default_questions(tmp_path):
    class RecordingResearch(MockResearch):
        def __init__(self):
            super().__init__()
            self.queries = []

        async def search(self, request):
            self.queries.append(request.query)
            return await super().search(request)

    research = RecordingResearch()
    orchestrator = Orchestrator(
        tmp_path, EventBus(), MockModelClient(), research, FixtureCellScorer
    )
    state = await orchestrator.start(
        ResearchRequest(question="Where is it smart to open a coffee place in Kreuzberg?")
    )
    await orchestrator.approve(state.run_id, ApproveRequest())
    await orchestrator.execute(state.run_id)

    assert all(
        any(question.text in query for query in research.queries)
        for question in state.plan.sub_questions
    )
    entries = orchestrator.result(state.run_id).evidence
    assert {entry.method for entry in entries} >= {
        "local-snapshot",
        "tavily-search",
        "tavily-extract",
    }
    assert all(entry.quote.strip() for entry in entries)


@pytest.mark.asyncio
async def test_research_runtime_errors_do_not_fail_run(tmp_path):
    class FailingResearch:
        async def search(self, request):
            raise RuntimeError("provider unavailable")

        async def extract(self, request):
            raise RuntimeError("provider unavailable")

    orchestrator = Orchestrator(
        tmp_path, EventBus(), MockModelClient(), FailingResearch(), FixtureCellScorer
    )
    state = await orchestrator.start(
        ResearchRequest(question="Where is it smart to open a coffee place in Kreuzberg?")
    )
    await orchestrator.approve(state.run_id, ApproveRequest())
    await orchestrator.execute(state.run_id)

    assert orchestrator.get_state(state.run_id).status == "done"
    assert (tmp_path / state.run_id / "cells.json").exists()
    assert any(
        event.type == "error" and "RuntimeError" in event.text
        for event in orchestrator.events(state.run_id)
    )


@pytest.mark.asyncio
async def test_rejected_future_evidence_is_removed_from_cells_and_report(tmp_path):
    class RejectFutureFactChecker:
        async def check(self, entries):
            return [
                entry.model_copy(update={"status": "unsupported"})
                if "bis 2028" in (entry.quote or "")
                else entry
                for entry in entries
            ]

    orchestrator = Orchestrator(
        tmp_path,
        EventBus(),
        MockModelClient(),
        MockResearch(),
        FixtureCellScorer,
        RejectFutureFactChecker(),
    )
    state = await orchestrator.start(
        ResearchRequest(question="Where is it smart to open a coffee place in Kreuzberg?")
    )
    await orchestrator.approve(state.run_id, ApproveRequest())
    await orchestrator.execute(state.run_id)

    result = orchestrator.result(state.run_id)
    rejected_ids = {
        entry.evidence_id
        for entry in result.evidence
        if entry.status == "unsupported" and "bis 2028" in (entry.quote or "")
    }
    assert rejected_ids
    assert all(
        modifier.evidence_id not in rejected_ids
        for cell in result.cells.cells
        for modifier in cell.modifiers
    )
    assert all(f"[{evidence_id}]" not in result.report_md for evidence_id in rejected_ids)


@pytest.mark.asyncio
async def test_terminal_events_are_published_before_terminal_state_is_saved(tmp_path):
    class StateCheckingBus(EventBus):
        def __init__(self):
            super().__init__()
            self.terminal_states = []

        async def publish(self, event):
            if event.stage in {"done", "error"}:
                state = RunState.model_validate_json(
                    (tmp_path / event.run_id / "state.json").read_text()
                )
                self.terminal_states.append((event.stage, state.status))
            await super().publish(event)

    class BrokenScorer:
        def build_cells(self, area, layers, ledger):
            raise RuntimeError("scoring failed")

    bus = StateCheckingBus()
    orchestrator = Orchestrator(tmp_path, bus, MockModelClient(), None, FixtureCellScorer)
    done_state = await orchestrator.start(
        ResearchRequest(question="Where is it smart to open a coffee place in Kreuzberg?")
    )
    await orchestrator.approve(done_state.run_id, ApproveRequest())
    await orchestrator.execute(done_state.run_id)

    failed_orchestrator = Orchestrator(
        tmp_path, bus, MockModelClient(), None, lambda: BrokenScorer()
    )
    failed_state = await failed_orchestrator.start(
        ResearchRequest(question="Where is it smart to open a coffee place in Kreuzberg?")
    )
    await failed_orchestrator.approve(failed_state.run_id, ApproveRequest())
    await failed_orchestrator.execute(failed_state.run_id)

    assert ("done", "running") in bus.terminal_states
    assert ("error", "running") in bus.terminal_states


@pytest.mark.asyncio
async def test_execute_rebinds_shared_connector_budgets_to_its_run(tmp_path):
    class SharedBudgetModel:
        records_budget = True

        def __init__(self):
            self.gateway = SimpleNamespace(budget=None)

        async def structured(self, role, operation, context, output_model, evidence=()):
            if output_model is Classification:
                return (
                    Classification(
                        shape="location",
                        template="coffee",
                        area="Friedrichshain-Kreuzberg",
                        why="Area provided.",
                    ),
                    0,
                )
            return output_model(), 0

        def model_name(self, role):
            return "shared-budget-test"

    class SharedBudgetResearch(MockResearch):
        budget = None

    model, research = SharedBudgetModel(), SharedBudgetResearch()
    orchestrator = Orchestrator(tmp_path, EventBus(), model, research, FixtureCellScorer)
    first = await orchestrator.start(
        ResearchRequest(question="Where is it smart to open a coffee place in Kreuzberg?")
    )
    first_budget = orchestrator.budgets[first.run_id]
    second = await orchestrator.start(
        ResearchRequest(question="Where is it smart to open a coffee place in Neukölln?")
    )
    assert model.gateway.budget is orchestrator.budgets[second.run_id]

    await orchestrator.approve(first.run_id, ApproveRequest())
    await orchestrator.execute(first.run_id)

    assert model.gateway.budget is first_budget
    assert research.budget is first_budget


@pytest.mark.asyncio
async def test_event_bus_bounds_queues_and_drops_only_slow_subscribers():
    bus = EventBus()
    slow, active = bus.subscribe("run"), bus.subscribe("run")
    event = ResearchEvent(
        seq=1,
        run_id="run",
        ts=datetime.now(timezone.utc),
        stage="research",
        role="researcher",
        type="search",
        text="query",
        why="test",
    )
    for _ in range(1000):
        slow.put_nowait(event)

    await bus.publish(event)

    assert slow.maxsize == 1000 and slow.qsize() == 1000
    assert active.qsize() == 1 and active.get_nowait() == event


@pytest.mark.asyncio
async def test_model_gateway_receives_ledger_ids_with_exact_ids(tmp_path):
    class FakeGateway:
        def __init__(self):
            self.budget = ResearchBudget(12, 10, 12, 60000)
            self.evidence_ids = []

        async def synthesize(self, context, output_model, evidence=()):
            self.evidence_ids = [item.evidence_id for item in evidence]
            return output_model(sentences=[])

    gateway = FakeGateway()
    orchestrator = Orchestrator(
        tmp_path, EventBus(), NebiusModelClient(gateway, "fake-model"), None
    )
    run_id = "a" * 12
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    writer = TraceWriter(run_dir, run_id, orchestrator.bus)
    entry = EvidenceEntry(
        evidence_id="e42",
        method="tavily-extract",
        url="https://example.org/source",
        title="Test source",
        quote="A source-backed passage.",
        tier="weak",
        retrieved_at=datetime.now(timezone.utc),
    )

    await orchestrator._model_call(
        run_id,
        writer,
        gateway.budget,
        "writer",
        "synthesize",
        {"evidence": [entry.model_dump(mode="json")]},
        WriterOutput,
        stage="write",
    )

    assert gateway.evidence_ids == ["e42"]
