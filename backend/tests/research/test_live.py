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
from app.evidence.factcheck import FACT_CHECKED_MARK, CheckResult, FactChecker
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
CITATION_RULES = (
    "Only cite a source for a claim about the target area if the quoted passage or the source "
    "title explicitly places it in that area (district, Ortsteil, street or postcode inside it). "
    "Listings, venues or statistics located in other districts must not support claims about "
    "the target area; the skeptic must exclude any finding whose cited source is located elsewhere."
)


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
    def __init__(
        self,
        budget,
        decisions,
        *,
        exclude=False,
        synthesis_findings=None,
        summary="Kreuzberg has local demand, though more evidence would improve confidence.",
    ):
        self.budget = budget
        self.config = type("Config", (), {"model": "fake-model"})()
        self.decisions = iter(decisions)
        self.exclude = exclude
        self.synthesis_findings = synthesis_findings
        self.summary = summary
        self.synthesis_context = None
        self.review_context = None
        self.review_evidence = []

    async def decide(self, context, output_model, evidence=()):
        self.budget.consume_inference_call()
        self.budget.record_tokens(10)
        return next(self.decisions)

    async def synthesize(self, context, output_model, evidence=()):
        self.budget.consume_inference_call()
        self.budget.record_tokens(20)
        self.synthesis_context = context
        selected = evidence[0]
        return Synthesis(
            summary=self.summary,
            findings=self.synthesis_findings
            if self.synthesis_findings is not None
            else [
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
        self.review_context = context
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
    assert gateway.synthesis_context["citation_rules"] == CITATION_RULES
    assert gateway.review_context["citation_rules"] == CITATION_RULES
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


@pytest.mark.asyncio
async def test_live_answer_rebuilds_after_fact_check_drops_a_finding(tmp_path):
    budget = ResearchBudget(6, 6, 14, 200_000)
    gateway = FakeGateway(
        budget,
        [
            NextStep(action="search", query="local demand", why="We need local evidence."),
            NextStep(action="finish", why="The evidence covers the requested area."),
        ],
        synthesis_findings=[
            Finding(
                claim="Supported demand finding.",
                topic="demand",
                quote=QUOTE,
                evidence_ids=["ev_supported"],
                localization="Kreuzberg",
            ),
            Finding(
                claim="Dropped unsupported finding.",
                topic="cost",
                quote=QUOTE,
                evidence_ids=["ev_unsupported"],
                localization="Kreuzberg",
            ),
        ],
        summary="Draft repeats the dropped unsupported finding.",
    )

    class TwoEvidenceTavily(FakeTavily):
        async def search(self, request):
            self.budget.consume_search()
            self.search_calls.append(request.query)
            return EvidencePage(
                (
                    web_evidence("ev_supported", extracted=True),
                    web_evidence("ev_unsupported", extracted=True),
                ),
                "complete",
            )

    class MixedChecker(FactChecker):
        async def check_finding(self, finding, ledger):
            if finding["finding_id"] == "f_2":
                return CheckResult("f_2", "finding", "unsupported", 0.2, "No matching support.")
            return CheckResult(
                "f_1",
                "finding",
                "verified",
                1.0,
                "The finding matches its cited passage.",
                matched_ids=("ev_supported",),
            )

    await run_live(
        tmp_path,
        run_id="run-answer-rebuild",
        question="Where should a coffee shop open?",
        area_name="Kreuzberg",
        role="operator",
        gateway=gateway,
        tavily=TwoEvidenceTavily(budget),
        checker=MixedChecker(offline=True),
    )

    report = (tmp_path / "report.md").read_text()
    answer = report.split("## Findings", 1)[0]
    assert (
        "_Summary rebuilt from the fact-checked findings below; "
        "the model's draft referenced claims that were dropped._"
    ) in answer
    assert "Draft repeats the dropped unsupported finding." not in answer
    assert "Supported demand finding [1]." in answer
    assert "## Could not verify" in report
    assert "Dropped unsupported finding." in report
    assert report.rstrip().endswith(FACT_CHECKED_MARK)


@pytest.mark.asyncio
async def test_synthesis_rejections_are_persisted_and_not_fact_checked(tmp_path):
    budget = ResearchBudget(6, 6, 14, 200_000)
    gateway = FakeGateway(
        budget,
        [
            NextStep(action="search", query="local demand", why="We need local evidence."),
            NextStep(action="finish", why="The evidence covers the requested area."),
        ],
        synthesis_findings=[
            Finding(
                claim="The rejected synthesis claim.",
                topic="demand",
                quote="This quote is not in the evidence.",
                evidence_ids=["ev_search"],
                localization="Kreuzberg",
            )
        ],
    )

    class TrackingChecker(FactChecker):
        def __init__(self):
            super().__init__(offline=True)
            self.checked_findings = []

        async def check_finding(self, finding, ledger):
            self.checked_findings.append(finding["finding_id"])
            return CheckResult(
                finding["finding_id"], "finding", "verified", 1.0, "Unexpected verification."
            )

    checker = TrackingChecker()
    await run_live(
        tmp_path,
        run_id="run-synthesis-rejected",
        question="Where should a coffee shop open?",
        area_name="Kreuzberg",
        role="operator",
        gateway=gateway,
        tavily=FakeTavily(budget),
        checker=checker,
    )

    finding = json.loads((tmp_path / "findings.jsonl").read_text().splitlines()[0])
    assert finding["status"] == "rejected"
    assert finding["reject_reason"]
    assert finding["evidence_ids"] == []
    assert finding["cited_evidence_ids"] == ["ev_search"]
    assert finding["claim"] == "The rejected synthesis claim."
    assert finding["quote"] == "This quote is not in the evidence."
    assert checker.checked_findings == []
    assert gateway.review_context["findings"] == []
    report = (tmp_path / "report.md").read_text()
    assert "The rejected synthesis claim." not in report
    assert "## Findings" in report
    assert "[1]" not in report


def _write_canned_live_run(run_dir):
    run_dir.mkdir(parents=True, exist_ok=True)
    entries = [
        {
            "evidence_id": "ev_live",
            "method": "tavily-search",
            "url": "https://live.example.org/source",
            "title": "Primary source",
            "snippet": "Oranienstraße sees high pedestrian traffic.",
            "status": "verified",
        },
        {
            "evidence_id": "ev_secondary",
            "method": "tavily-search",
            "url": "https://live.example.org/secondary",
            "title": "Secondary source",
            "snippet": "The street attracts many visitors daily.",
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
        {
            "evidence_id": "ev_snippet",
            "method": "tavily-search",
            "url": "https://live.example.org/snippet",
            "title": "Snippet source",
            "snippet": "Snippet fallback " + "x" * 400,
            "status": "unsupported",
        },
    ]
    (run_dir / "evidence.jsonl").write_text("".join(json.dumps(entry) + "\n" for entry in entries))
    findings = [
        {
            "finding_id": "f_1",
            "claim": "A live finding.",
            "quote": "Oranienstraße sees high pedestrian traffic.",
            "evidence_ids": ["ev_live", "ev_secondary"],
            "claim_type": "demand",
            "localization": "Kreuzberg",
            "agent": "web_researcher",
            "status": "accepted",
            "verification": "verified",
        },
        {
            "finding_id": "f_2",
            "claim": "A rejected live finding.",
            "quote": "Rejected source quote",
            "evidence_ids": [],
            "cited_evidence_ids": ["ev_unsupported"],
            "claim_type": "risk",
            "localization": "Kreuzberg",
            "agent": "web_researcher",
            "status": "rejected",
            "reject_reason": "The claim is unsupported.",
        },
    ]
    (run_dir / "findings.jsonl").write_text(
        "".join(json.dumps(finding) + "\n" for finding in findings)
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
        layer = run["plan"]["layers"][0]
        approval_layers = [{"id": layer["id"], "enabled": False, "weight": 0.27}]
        approved = client.post(
            f"/runs/{run['run_id']}/approve",
            headers=headers,
            json={"layers": approval_layers},
        )
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
        payload = result.json()
        evidence_ids = [item["evidence_id"] for item in payload["evidence"]]
        assert evidence_ids[:3] == ["ev_live", "ev_secondary", "ev_unsupported"]
        assert evidence_ids[3] == "ev_snippet"
        assert payload["evidence"][0]["quote"] == "Oranienstraße sees high pedestrian traffic."
        assert payload["evidence"][1]["quote"] == "The street attracts many visitors daily."
        assert payload["evidence"][1]["quote_source"] == "snippet"
        assert payload["evidence"][2]["quote"] == "Unsupported quote"
        assert payload["evidence"][2]["quote_source"] == "snippet"
        assert payload["evidence"][2]["quote"] != payload["findings"][1]["quote"]
        assert payload["evidence"][3]["quote"] == ("Snippet fallback " + "x" * 400)[:300]
        assert payload["evidence"][3]["quote_source"] == "snippet"
        assert payload["evidence"][4]["method"] == "wfs"
        assert payload["findings"][1]["status"] == "rejected"
        ledger = [
            json.loads(line)
            for line in (tmp_path / run["run_id"] / "evidence.jsonl").read_text().splitlines()
        ]
        assert all("quote" not in entry and "quote_source" not in entry for entry in ledger)
        assert result.json()["fixture_notice"].startswith("The heatmap layers are demo fixtures")
        assert result.json()["trace"][0]["type"] == "approved"
        live_cells = result.json()["cells"]
        assert live_cells is not None
        approved_layer = next(item for item in live_cells["layers"] if item["id"] == layer["id"])
        assert approved_layer["enabled"] is False
        assert approved_layer["weight_default"] == 0.27
        assert (
            client.get(f"/runs/{run['run_id']}/export/report.md", headers=headers).status_code
            == 200
        )

    RUNS.clear()


def test_live_pending_result_and_export_keep_fixture_status(tmp_path, monkeypatch):
    monkeypatch.setenv("RISALITE_LIVE", "1")
    monkeypatch.setenv("NEBIUS_API_KEY", "test-nebius-key")
    monkeypatch.setenv("TAVILY_API_KEY", "test-tavily-key")
    RUNS.clear()

    async def unused_run_live(*args, **kwargs):
        raise AssertionError("pending runs must not start the live runner")

    monkeypatch.setattr("app.research.live.run_live", unused_run_live)
    with TestClient(create_app(runs_dir=tmp_path)) as client:
        response = client.post(
            "/research",
            json={"question": "Where should a coffee shop open in Kreuzberg?", "role": "operator"},
        )
        run = response.json()
        assert response.status_code == 200
        assert run["mode"] == "live"
        assert run["status"] in {"needs_clarification", "awaiting_approval"}
        headers = {"Authorization": f"Bearer {run['run_token']}"}
        assert "live_run_dir" not in RUNS[run["run_id"]]

        pending = client.get(f"/runs/{run['run_id']}/result", headers=headers)
        assert pending.status_code == 202
        assert pending.json() == {"status": run["status"]}

        exported = client.get(f"/runs/{run['run_id']}/export/report.md", headers=headers)
        assert exported.status_code == 409
        assert exported.json()["detail"] == "run not finished"

    RUNS.clear()


def test_live_point_result_omits_fixture_candidates_and_exports_findings(tmp_path, monkeypatch):
    monkeypatch.setenv("RISALITE_LIVE", "1")
    monkeypatch.setenv("NEBIUS_API_KEY", "test-nebius-key")
    monkeypatch.setenv("TAVILY_API_KEY", "test-tavily-key")
    RUNS.clear()

    async def canned_run(run_dir, **kwargs):
        _write_canned_live_run(run_dir)
        return {"run_id": kwargs["run_id"], "status": "done"}

    monkeypatch.setattr("app.research.live.run_live", canned_run)
    with TestClient(create_app(runs_dir=tmp_path)) as client:
        response = client.post(
            "/research",
            json={
                "question": (
                    "This storefront at Wiener Straße 12 just became available — what is the best use?"
                ),
                "role": "operator",
            },
        )
        run = response.json()
        assert response.status_code == 200
        assert run["mode"] == "live"
        assert run["shape"] == "point"
        assert run["plan"]["area"] is None
        headers = {"Authorization": f"Bearer {run['run_token']}"}
        approved = client.post(f"/runs/{run['run_id']}/approve", headers=headers, json={})
        assert approved.status_code == 200

        streamed = client.get(f"/runs/{run['run_id']}/events", headers=headers)
        assert streamed.status_code == 200
        result = client.get(f"/runs/{run['run_id']}/result", headers=headers)

        assert result.status_code == 200
        assert result.json()["candidates"] is None
        assert result.json()["cells"] is None
        assert result.json()["findings"][0]["claim"] == "A live finding."
        assert result.json()["findings"][1]["status"] == "rejected"
        exported = client.get(f"/runs/{run['run_id']}/export/report.md", headers=headers)
        assert exported.status_code == 200
        assert "A live finding. [1]" in exported.text
        assert "Dropped or unverified claims" in exported.text
        dropped_line = next(
            line for line in exported.text.splitlines() if "A rejected live finding." in line
        )
        assert "A rejected live finding." in dropped_line
        assert "[" not in dropped_line

    RUNS.clear()
