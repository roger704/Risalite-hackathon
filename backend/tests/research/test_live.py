import asyncio
import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.api.runs_fixtures import RUNS
from app.connectors.tavily import EvidencePage
from app.domain.research import (
    EvidenceKind,
    ExtractionStatus,
    ResearchBudget,
    WebEvidence,
)
from app.evidence.factcheck import FactChecker
from app.evidence.trace import TraceWriter
from app.research.live import (
    Challenge,
    Finding,
    NextStep,
    Review,
    Synthesis,
    run_live,
)

QUOTE = "Kreuzberg has a dense and young population around the market square."


def web_evidence(evidence_id="ev_search", *, extracted=False):
    return WebEvidence(
        evidence_id=evidence_id,
        kind=EvidenceKind.EXTRACTED_PAGE if extracted else EvidenceKind.SEARCH_RESULT,
        original_url="https://berlin.example.org/kreuzberg",
        canonical_url="https://berlin.example.org/kreuzberg",
        title="Kreuzberg business district",
        query="Kreuzberg coffee demand",
        retrieved_at=datetime.now(timezone.utc),
        published_at=None,
        published_at_raw=None,
        snippet=QUOTE,
        passage=QUOTE if extracted else None,
        passage_locator=None,
        extraction_status=ExtractionStatus.OK if extracted else ExtractionStatus.NOT_ATTEMPTED,
        failure_reason=None,
        content_hash=None,
        candidate_id=None,
        analysis_id=None,
        provider="tavily",
        provider_request_id=None,
        score=0.9,
    )


class FakeGateway:
    def __init__(self, budget, decisions, *, exclude=False):
        self.budget = budget
        self.config = type("Config", (), {"model": "fake-model"})()
        self.decisions = iter(decisions)
        self.exclude = exclude
        self.review_evidence = []

    async def decide(self, context, output_model, evidence=()):
        self.budget.consume_inference_call()
        self.budget.record_tokens(10)
        return next(self.decisions)

    async def synthesize(self, context, output_model, evidence=()):
        self.budget.consume_inference_call()
        self.budget.record_tokens(20)
        selected = evidence[0]
        return Synthesis(
            summary="Kreuzberg has local demand, though more evidence would improve confidence.",
            findings=[
                Finding(
                    claim="The market square serves a dense, young population.",
                    topic="demand",
                    quote=QUOTE,
                    evidence_ids=[selected.evidence_id],
                    localization="Kreuzberg market square",
                )
            ],
        )

    async def review(self, context, output_model, evidence=()):
        self.budget.consume_inference_call()
        self.budget.record_tokens(5)
        self.review_evidence = list(evidence)
        challenges = (
            [
                Challenge(
                    finding_id="f_1",
                    severity="high",
                    reason="The source does not establish customer conversion.",
                    adjustment="exclude",
                )
            ]
            if self.exclude
            else []
        )
        return Review(challenges=challenges)


class FakeTavily:
    def __init__(self, budget):
        self.budget = budget
        self.search_calls = []
        self.extract_calls = []

    async def search(self, request):
        self.budget.consume_search()
        self.search_calls.append(request.query)
        return EvidencePage((web_evidence(),), "complete")

    async def extract(self, request):
        self.budget.consume_extract_urls(len(request.urls))
        self.extract_calls.extend(request.urls)
        return EvidencePage((web_evidence("ev_extracted", extracted=True),), "complete")


@pytest.mark.asyncio
async def test_live_loop_handles_duplicates_extracts_and_skeptic_exclusion(tmp_path):
    budget = ResearchBudget(6, 6, 14, 200_000)
    gateway = FakeGateway(
        budget,
        [
            NextStep(
                action="search",
                query="Kreuzberg coffee market",
                why="There is no evidence about local demand yet.",
            ),
            NextStep(
                action="search",
                query="Kreuzberg coffee market",
                why="The demand evidence is incomplete, but this query was already tried.",
            ),
            NextStep(
                action="extract",
                evidence_ids=["ev_search"],
                why="The search snippet needs the full source text to confirm its claim.",
            ),
            NextStep(action="finish", why="The strongest available source has been extracted."),
        ],
        exclude=True,
    )
    tavily = FakeTavily(budget)

    result = await run_live(
        tmp_path,
        run_id="run-live-test",
        question="Where should a coffee shop open?",
        area_name="Kreuzberg",
        role="operator",
        gateway=gateway,
        tavily=tavily,
        checker=FactChecker(offline=True),
    )

    trace = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()]
    search = next(event for event in trace if event["type"] == "search")
    assert search["why"] == "There is no evidence about local demand yet."
    assert sum(event["type"] == "search" for event in trace) == 1
    assert any("Duplicate search query" in event["why"] for event in trace)
    assert any(event["type"] == "extract" for event in trace)
    assert any(
        event["type"] == "finding_rejected" and event["agent"] == "skeptic" for event in trace
    )
    assert tavily.search_calls == ["Kreuzberg coffee market"]
    assert tavily.extract_calls == ["https://berlin.example.org/kreuzberg"]
    assert gateway.review_evidence
    assert result["status"] == "done"
    assert (tmp_path / "evidence.jsonl").exists()
    assert json.loads((tmp_path / "findings.jsonl").read_text().splitlines()[0])["status"] == (
        "rejected"
    )
    assert "The strongest available source" not in (tmp_path / "report.md").read_text()
    assert json.loads((tmp_path / "metrics.json").read_text())["Web researcher"]["calls"] >= 4
    assert trace[-1]["type"] == "status"
    assert trace[-1]["detail"]["summary"] == "done"


@pytest.mark.asyncio
async def test_live_loop_records_search_budget_exhaustion(tmp_path):
    budget = ResearchBudget(1, 6, 14, 200_000)
    gateway = FakeGateway(
        budget,
        [
            NextStep(action="search", query="first query", why="We need a source."),
            NextStep(action="search", query="second query", why="One gap remains."),
        ],
    )
    tavily = FakeTavily(budget)

    await run_live(
        tmp_path,
        run_id="run-budget",
        question="Where should a coffee shop open?",
        area_name="Kreuzberg",
        role=None,
        gateway=gateway,
        tavily=tavily,
        checker=FactChecker(offline=True),
    )

    trace = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()]
    assert tavily.search_calls == ["first query"]
    assert any(
        event["type"] == "budget_exhausted" and "search budget is exhausted" in event["why"]
        for event in trace
    )


def _write_canned_live_run(run_dir):
    run_dir.mkdir(parents=True, exist_ok=True)
    entries = [
        {
            "evidence_id": "ev_live",
            "method": "tavily-search",
            "url": "https://live.example.org/source",
            "title": "Live source",
            "snippet": "Live source quote",
            "status": "verified",
        },
        {
            "evidence_id": "ev_unsupported",
            "method": "tavily-search",
            "url": "https://live.example.org/unsupported",
            "title": "Unsupported source",
            "snippet": "Unsupported quote",
            "status": "unsupported",
        },
    ]
    (run_dir / "evidence.jsonl").write_text("".join(json.dumps(entry) + "\n" for entry in entries))
    (run_dir / "findings.jsonl").write_text(
        json.dumps(
            {
                "finding_id": "f_1",
                "claim": "A live finding.",
                "quote": "Live source quote",
                "evidence_ids": ["ev_live"],
                "claim_type": "demand",
                "localization": "Kreuzberg",
                "agent": "web_researcher",
                "status": "accepted",
            }
        )
        + "\n"
    )
    (run_dir / "report.md").write_text("## Answer\nA live answer.\n")
    (run_dir / "metrics.json").write_text("{}\n")
    trace = TraceWriter(run_dir, "live-api-test")
    trace.emit("plan", "planner", "The live run started.")
    trace.emit("status", "orchestrator", "Research run completed.", detail={"summary": "done"})
    (run_dir / "result.json").write_text('{"status":"done"}\n')


def test_live_api_streams_run_and_orders_citable_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("RISALITE_LIVE", "1")
    monkeypatch.setenv("NEBIUS_API_KEY", "test-nebius-key")
    monkeypatch.setenv("TAVILY_API_KEY", "test-tavily-key")
    monkeypatch.setenv("RISALITE_REPLAY_DELAY", "0")
    RUNS.clear()

    async def canned_run(run_dir, **kwargs):
        await asyncio.sleep(0.7)
        _write_canned_live_run(run_dir)
        return {"run_id": kwargs["run_id"], "status": "done"}

    monkeypatch.setattr("app.research.live.run_live", canned_run)
    with TestClient(create_app(runs_dir=tmp_path)) as client:
        response = client.post(
            "/research",
            json={"question": "Where should a coffee shop open in Kreuzberg?", "role": "operator"},
        )
        run = response.json()
        assert response.status_code == 200
        assert run["mode"] == "live"
        headers = {"Authorization": f"Bearer {run['run_token']}"}
        approved = client.post(f"/runs/{run['run_id']}/approve", headers=headers, json={})
        assert approved.status_code == 200

        pending = client.get(f"/runs/{run['run_id']}/result", headers=headers)
        assert pending.status_code == 202
        streamed = client.get(f"/runs/{run['run_id']}/events", headers=headers)
        assert streamed.status_code == 200
        assert '"type": "approved"' in streamed.text
        assert '"type": "plan"' in streamed.text
        assert '"synthetic"' not in streamed.text
        resumed = client.get(
            f"/runs/{run['run_id']}/events",
            headers={**headers, "Last-Event-ID": "0"},
        )
        assert '"type": "approved"' not in resumed.text
        assert '"type": "plan"' in resumed.text

        result = client.get(f"/runs/{run['run_id']}/result", headers=headers)
        assert result.status_code == 200
        evidence_ids = [item["evidence_id"] for item in result.json()["evidence"]]
        assert evidence_ids[:2] == ["ev_live", "ev_unsupported"]
        assert result.json()["evidence"][2]["method"] == "wfs"
        assert result.json()["fixture_notice"].startswith("The heatmap layers are demo fixtures")
        assert result.json()["trace"][0]["type"] == "approved"
        assert (
            client.get(f"/runs/{run['run_id']}/export/report.md", headers=headers).status_code
            == 200
        )

    RUNS.clear()
