import json
from datetime import datetime, timezone

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from app.application.research.agent import run_research
from app.application.research.events import EventSink
from app.application.research.ledger import EvidenceLedger, FindingStore
from app.application.research.tools import build_research_tools
from app.domain.research import (
    EvidenceKind,
    EvidencePage,
    ExtractionStatus,
    WebEvidence,
)


class FakeConnector:
    def __init__(self):
        self.search_result = _evidence("ev_search", EvidenceKind.SEARCH_RESULT, snippet="Preview")
        self.extracted_result = _evidence(
            "ev_page",
            EvidenceKind.EXTRACTED_PAGE,
            passage="The source states that cafés benefit from busy high streets.",
        )

    async def search(self, request):
        return EvidencePage((self.search_result,), "complete")

    async def extract(self, request):
        return EvidencePage((self.extracted_result,), "complete")


class ToolCallingFake(GenericFakeChatModel):
    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self


def _evidence(evidence_id, kind, *, snippet=None, passage=None):
    return WebEvidence(
        evidence_id=evidence_id,
        kind=kind,
        original_url="https://example.com/kreuzberg",
        canonical_url="https://example.com/kreuzberg",
        title="Kreuzberg cafés",
        query="Kreuzberg café locations",
        retrieved_at=datetime.now(timezone.utc),
        published_at=None,
        published_at_raw=None,
        snippet=snippet,
        passage=passage,
        passage_locator=None,
        extraction_status=(ExtractionStatus.OK if passage else ExtractionStatus.NOT_ATTEMPTED),
        failure_reason=None,
        content_hash=None,
        candidate_id=None,
        analysis_id=None,
        provider="fake",
        provider_request_id=None,
        score=None,
    )


def _tool(tools, name):
    return next(item for item in tools if item.name == name)


async def test_web_search_persists_ledger_and_returns_evidence_id(tmp_path):
    connector = FakeConnector()
    ledger = EvidenceLedger(tmp_path)
    events = EventSink(tmp_path)
    tools = build_research_tools(connector, ledger, FindingStore(tmp_path), events, "researcher")

    response = await _tool(tools, "web_search").ainvoke(
        {"query": "Kreuzberg cafés", "why": "Find local sources"}
    )

    assert json.loads(response)[0]["evidence_id"] == "ev_search"
    assert ledger.has("ev_search")
    evidence_rows = [
        json.loads(line) for line in (tmp_path / "evidence.jsonl").read_text().splitlines()
    ]
    assert evidence_rows[0]["evidence_id"] == "ev_search"
    assert events.events[0]["type"] == "search"


async def test_extract_page_unknown_id_returns_guidance(tmp_path):
    tools = build_research_tools(
        FakeConnector(), EvidenceLedger(tmp_path), FindingStore(tmp_path), EventSink(tmp_path), "r"
    )

    response = await _tool(tools, "extract_page").ainvoke(
        {"evidence_id": "missing", "why": "Extract source"}
    )

    assert "Use an id returned by web_search" in response


async def test_record_finding_rejects_unknown_id_and_fabricated_quote(tmp_path):
    connector = FakeConnector()
    ledger = EvidenceLedger(tmp_path)
    ledger.add_page(EvidencePage((connector.extracted_result,), "complete"))
    findings = FindingStore(tmp_path)
    tools = build_research_tools(connector, ledger, findings, EventSink(tmp_path), "r")
    record = _tool(tools, "record_finding")

    unknown = await record.ainvoke(
        {
            "claim": "Unsupported",
            "quote": "Any quote",
            "evidence_ids": ["missing"],
            "claim_type": "fact",
        }
    )
    fabricated = await record.ainvoke(
        {
            "claim": "Also unsupported",
            "quote": "Cafés are always profitable.",
            "evidence_ids": ["ev_page"],
            "claim_type": "opinion",
        }
    )

    assert "Unknown evidence ids" in unknown
    assert "quote was not found" in fabricated
    assert not findings.accepted
    assert len(findings.rejected) == 2


async def test_record_finding_accepts_verbatim_and_whitespace_normalized_quotes(tmp_path):
    connector = FakeConnector()
    ledger = EvidenceLedger(tmp_path)
    ledger.add_page(EvidencePage((connector.extracted_result,), "complete"))
    findings = FindingStore(tmp_path)
    tools = build_research_tools(connector, ledger, findings, EventSink(tmp_path), "r")
    record = _tool(tools, "record_finding")
    base = {
        "claim": "Cafés benefit from busy high streets.",
        "evidence_ids": ["ev_page"],
        "claim_type": "fact",
    }

    exact = await record.ainvoke(
        base | {"quote": "The source states that cafés benefit from busy high streets."}
    )
    normalized = await record.ainvoke(
        base | {"quote": "The source states that cafés   benefit from\nbusy high streets."}
    )

    assert exact.startswith("finding_")
    assert normalized.startswith("finding_")
    assert len(findings.accepted) == 2
    assert (tmp_path / "findings.jsonl").read_text().count("\n") == 2


async def test_scripted_agent_run_persists_outputs_and_validates_citation(tmp_path):
    source_quote = "The source states that cafés benefit from busy high streets."
    model = ToolCallingFake(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "web_search",
                            "args": {"query": "Kreuzberg cafés", "why": "Find local sources"},
                            "id": "search-1",
                            "type": "tool_call",
                        }
                    ],
                    usage_metadata={"input_tokens": 12, "output_tokens": 3, "total_tokens": 15},
                ),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "extract_page",
                            "args": {"evidence_id": "ev_search", "why": "Read the source"},
                            "id": "extract-1",
                            "type": "tool_call",
                        }
                    ],
                    usage_metadata={"input_tokens": 14, "output_tokens": 4, "total_tokens": 18},
                ),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "record_finding",
                            "args": {
                                "claim": "Busy high streets benefit cafés.",
                                "quote": source_quote,
                                "evidence_ids": ["ev_page"],
                                "claim_type": "fact",
                                "why": "Record a sourced claim",
                            },
                            "id": "finding-1",
                            "type": "tool_call",
                        }
                    ],
                    usage_metadata={"input_tokens": 16, "output_tokens": 5, "total_tokens": 21},
                ),
                AIMessage(
                    content="Busy high streets benefit cafés [ev_page].",
                    usage_metadata={"input_tokens": 18, "output_tokens": 6, "total_tokens": 24},
                ),
            ]
        )
    )

    result = await run_research(
        "Where should a café open in Kreuzberg?",
        ["Find local café-location guidance."],
        connector=FakeConnector(),
        run_dir=tmp_path,
        orchestrator_model=model,
        researcher_model=model,
    )

    assert "[ev_page]" in result.report
    assert result.metrics["citation_validity_percent"] == 100.0
    assert result.metrics["findings_accepted"] == 1
    assert result.metrics["tokens_in"] == 60
    for filename in (
        "evidence.jsonl",
        "findings.jsonl",
        "trace.jsonl",
        "report.md",
        "metrics.json",
    ):
        assert (tmp_path / filename).is_file()
    trace = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()]
    assert sum(event["type"] == "model_call" for event in trace) == 4
