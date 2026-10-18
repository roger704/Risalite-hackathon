from __future__ import annotations

import json
from datetime import datetime, timezone
from itertools import count
from typing import Any

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.contracts.events import ResearchEvent
from app.contracts.evidence import Finding
from app.domain.research import (
    EvidenceKind,
    EvidencePage,
    ExtractionStatus,
    ResearchExtract,
    ResearchSearch,
    WebEvidence,
    canonicalize_url,
    make_evidence_id,
)
from app.evidence.citations import citation_tokens
from app.evidence.ledger import EvidenceLedger
from app.research import (
    ResearchConfig,
    prompts,
    research_area,
    research_cell,
    research_hypothesis,
    skeptic,
    write_report,
)
from app.research.events import Events
from app.research.toolkit import BUDGET_MESSAGE, Toolkit, match_quote, quote_numbers

NOW = datetime(2026, 10, 18, tzinfo=timezone.utc)
AREA = {"area_id": "ortsteil:0202", "name": "Kreuzberg"}
CELLS = [
    {"id": "02010101", "name": "Moritzplatz", "parent_name": "Kreuzberg"},
    {"id": "02010102", "name": "Oranienplatz", "parent_name": "Kreuzberg"},
]
CELL = {**CELLS[0], "raw_values": {"transit": 1200, "cafe_saturation": 3.1}}
PASSAGE = (
    "Am Moritzplatz sollen bis 2028 rund 1.200 neue Wohnungen entstehen. "
    "Die Gewerbemieten in Kreuzberg steigen weiter."
)


class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self._seq = count()

    def __call__(self, type: str, payload: dict) -> None:
        ResearchEvent(
            seq=next(self._seq), run_id="r_test", ts="2026-10-18T00:00:00Z", type=type, **payload
        )
        self.events.append((type, payload))

    def types(self) -> list[str]:
        return [t for t, _ in self.events]


def web_evidence(
    url: str,
    *,
    query: str | None = "q",
    snippet: str | None = None,
    passage: str | None = None,
    title: str = "Wohnungsbau am Moritzplatz",
) -> WebEvidence:
    kind = EvidenceKind.EXTRACTED_PAGE if passage else EvidenceKind.SEARCH_RESULT
    canonical = canonicalize_url(url)
    return WebEvidence(
        evidence_id=make_evidence_id(kind, canonical, query, None, None),
        kind=kind,
        original_url=url,
        canonical_url=canonical,
        title=title,
        query=query,
        retrieved_at=NOW,
        published_at=None,
        published_at_raw=None,
        snippet=snippet,
        passage=passage,
        passage_locator=None,
        extraction_status=ExtractionStatus.OK if passage else ExtractionStatus.NOT_ATTEMPTED,
        failure_reason=None,
        content_hash=None,
        candidate_id=None,
        analysis_id=None,
        provider="tavily",
        provider_request_id=None,
        score=None,
    )


class StubConnector:
    def __init__(self) -> None:
        self.searches = 0
        self.extracts = 0

    async def search(self, request: ResearchSearch) -> EvidencePage:
        self.searches += 1
        url = f"https://www.berlin.de/projekt-{self.searches}"
        return EvidencePage((web_evidence(url, query=request.query, snippet=PASSAGE),), "complete")

    async def extract(self, request: ResearchExtract) -> EvidencePage:
        self.extracts += 1
        return EvidencePage(
            (web_evidence(request.urls[0], query=request.query, passage=PASSAGE + " Ende."),),
            "complete",
        )


class ScriptedModel(BaseChatModel):
    """Chat model that replays scripted replies; ``script`` may be a callable of the messages."""

    script: Any
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "ScriptedModel":
        return self

    def _reply(self, messages: list[BaseMessage]) -> AIMessage:
        self.calls += 1
        reply = self.script(messages, self.calls) if callable(self.script) else self.script.pop(0)
        return reply if isinstance(reply, AIMessage) else AIMessage(content=reply)

    def _generate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._reply(messages))])


def tool_call(name: str, args: dict, n: int) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call_{n}"}])


def cfg_with(model: BaseChatModel, connector: Any, **kwargs: Any) -> ResearchConfig:
    return ResearchConfig(
        fake=False, connector=connector, model_factory=lambda _name: model, **kwargs
    )


# -- fake mode -------------------------------------------------------------------------------


@pytest.fixture
def fake_cfg() -> ResearchConfig:
    return ResearchConfig(fake=True)


def test_research_config_defaults_model_roles_to_nemotron(monkeypatch):
    for name in (
        "RISALITE_RESEARCH_MODEL",
        "RISALITE_WRITER_MODEL",
        "RISALITE_SKEPTIC_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)

    cfg = ResearchConfig()
    expected = "nvidia/nemotron-3-super-120b-a12b"
    assert cfg.model == cfg.writer_model == cfg.skeptic_model == expected


async def test_fake_research_area(tmp_path, fake_cfg):
    ledger, emit = EvidenceLedger(tmp_path), Recorder()
    findings = await research_area("Café in Kreuzberg?", AREA, CELLS, ledger, emit, fake_cfg)
    assert {f.localization for f in findings} == {"02010101", "area"}
    future = next(f for f in findings if f.claim_type == "future")
    assert (future.unit, future.amount) == ("dwellings", 350.0)
    ids = {e["evidence_id"] for e in ledger.entries()}
    assert all(set(f.evidence_ids) <= ids for f in findings)
    for finding in findings:
        assert match_quote(ledger, finding.quote, finding.evidence_ids).best == "verified"
    assert "finding_accepted" in emit.types()
    again = await research_area(
        "Café in Kreuzberg?", AREA, CELLS, EvidenceLedger(tmp_path / "b"), Recorder(), fake_cfg
    )
    assert [f.finding_id for f in again] == [f.finding_id for f in findings]


async def test_fake_research_cell_and_hypothesis(tmp_path, fake_cfg):
    ledger, emit = EvidenceLedger(tmp_path), Recorder()
    cell = await research_cell("q", CELL, ledger, emit, fake_cfg)
    assert {f.claim_type for f in cell} >= {"future", "risk"}
    assert all(f.localization == "02010101" for f in cell)
    assert next(f for f in cell if f.claim_type == "future").amount == 120.0
    space = {"address": "Oranienstraße 1, 10999 Berlin", "cell_id": "02010101"}
    t1 = await research_hypothesis(
        "q", space, {"id": "cafe", "name": "Café"}, ledger, emit, fake_cfg
    )
    assert {getattr(f, "dimension") for f in t1} == {"cost", "operator_fit"}
    assert all(getattr(f, "hypothesis_id") == "cafe" for f in t1)


async def test_fake_skeptic_and_writer(tmp_path, fake_cfg):
    ledger, emit = EvidenceLedger(tmp_path), Recorder()
    findings = await research_cell("q", CELL, ledger, emit, fake_cfg)
    risks = await skeptic("q", CELL, findings, ledger, emit, fake_cfg)
    assert risks and risks[0]["severity"] in {"low", "medium", "high"}
    assert set(risks[0]) >= {"severity", "text", "quote", "evidence_id", "finding_id"}
    assert ledger.get(risks[0]["evidence_id"]) is not None
    assert Finding.model_validate(risks[0]["finding"]).claim_type == "risk"
    result = {
        "cells": [{**CELL, "score_present": 0.81, "risks": [risks[0]], "unknowns": ["rent level"]}]
    }
    report = await write_report("q", result, findings, ledger, emit, fake_cfg)
    assert "Moritzplatz" in report and "## Unknowns" in report
    known = {e["evidence_id"] for e in ledger.entries()}
    tokens = citation_tokens(report)
    assert tokens and set(tokens) <= known
    assert "write" in emit.types()


async def test_t1_hypotheses_are_ranked_in_fake_and_model_reports(tmp_path, fake_cfg):
    result = {
        "space": {"address": "Kreuzberg"},
        "candidates": {
            "hypotheses": [
                {"id": "h1", "name": "Café am Platz", "score_present": 0.91},
                {"id": "h2", "name": "Café am Park", "score_present": 0.82},
            ]
        },
    }
    ledger = EvidenceLedger(tmp_path)
    fake_report = await write_report("q", result, [], ledger, Recorder(), fake_cfg)

    assert "## Top locations" in fake_report
    assert "### 1. Café am Platz (h1)" in fake_report
    assert "### 2. Café am Park (h2)" in fake_report

    model = ScriptedModel(
        script=[
            json.dumps(
                {
                    "summary": [],
                    "cells": [
                        {"cell_id": "h1", "name": "Café am Platz"},
                        {"cell_id": "h2", "name": "Café am Park"},
                    ],
                    "unknowns": [],
                }
            )
        ]
    )
    model_report = await write_report("q", result, [], ledger, Recorder(), cfg_with(model, None))

    assert "## Top locations" in model_report
    assert "### 1. Café am Platz (h1)" in model_report
    assert "### 2. Café am Park (h2)" in model_report


async def test_writer_strips_unknown_ids_and_uncited_sentences(tmp_path):
    ledger = EvidenceLedger(tmp_path)
    ledger.add_web_evidence(web_evidence("https://www.berlin.de/a", snippet=PASSAGE))
    good = ledger.entries()[0]["evidence_id"]
    reply = json.dumps(
        {
            "summary": [
                {
                    "text": "Moritzplatz gets 1,200 dwellings [ev_bogus].",
                    "evidence_ids": [good, "ev_bogus"],
                },
                {"text": "Invented claim.", "evidence_ids": ["ev_nothere"]},
            ],
            "cells": [
                {
                    "cell_id": "02010101",
                    "reasons": [{"text": "Uncited reason.", "evidence_ids": []}],
                }
            ],
            "unknowns": [],
        }
    )
    model = ScriptedModel(script=[reply])
    finding = Finding(
        finding_id="f_1",
        claim="c",
        quote="q",
        evidence_ids=[good],
        claim_type="future",
        localization="02010101",
        agent="a",
        status="accepted",
    )
    cfg = cfg_with(model, None)
    report = await write_report("q", {"cells": [CELL]}, [finding], ledger, Recorder(), cfg)
    assert f"[{good}]" in report
    assert "bogus" not in report and "Invented" not in report and "Uncited" not in report


# -- record_finding --------------------------------------------------------------------------


@pytest.fixture
def toolkit(tmp_path) -> tuple[Toolkit, Recorder, str]:
    ledger, emit = EvidenceLedger(tmp_path), Recorder()
    entry = ledger.add_web_evidence(web_evidence("https://www.berlin.de/a", snippet=PASSAGE))
    kit = Toolkit(
        ledger=ledger,
        events=Events(emit, "researcher:wave1:1"),
        cfg=ResearchConfig(fake=False),
        connector=None,
        role="future",
        localizations=["02010101", "area"],
        default_localization="area",
    )
    return kit, emit, entry["evidence_id"]


async def test_record_finding_accepts_verbatim_quote(toolkit):
    kit, emit, eid = toolkit
    out = await kit.record_finding(
        claim="About 1,200 dwellings are planned at Moritzplatz by 2028.",
        quote="sollen bis 2028 rund 1.200 neue Wohnungen entstehen",
        evidence_ids=[eid],
        claim_type="future",
        localization="plr:02010101",
        kind="housing",
        unit="dwellings",
        amount=1200,
        completion_year=2028,
    )
    assert json.loads(out)["finding_id"].startswith("f_")
    finding = kit.accepted[0]
    assert finding.localization == "02010101"
    assert (finding.unit, finding.amount, finding.match) == ("dwellings", 1200, "verified")
    assert emit.types() == ["finding_accepted"]


async def test_record_finding_accepts_partial_and_drops_unstated_amount(toolkit):
    kit, _, eid = toolkit
    out = await kit.record_finding(
        claim="Rents keep rising.",
        quote="Die Gewerbemieten in Kreuzberg steigen deutlich weiter an",
        evidence_ids=[eid],
        claim_type="fact",
        amount=50,
    )
    assert "amount dropped" in out
    assert kit.accepted[0].match in {"verified", "partial"}
    assert getattr(kit.accepted[0], "amount", None) is None


def _localization_toolkit(tmp_path, passage, place_terms, localizations):
    ledger, emit = EvidenceLedger(tmp_path), Recorder()
    entry = ledger.add_web_evidence(
        web_evidence(
            "https://example.org/report",
            snippet=passage,
            title="Lokaler Bericht",
        )
    )
    kit = Toolkit(
        ledger=ledger,
        events=Events(emit, "researcher:t1:test"),
        cfg=ResearchConfig(fake=False),
        connector=None,
        role="t1",
        localizations=localizations,
        default_localization="area",
        place_terms=place_terms,
    )
    return kit, emit, entry["evidence_id"]


async def test_unsupported_cell_localization_downgrades_to_area(tmp_path):
    passage = "Eine Diskussion am Kotti über neue Cafés."
    kit, emit, evidence_id = _localization_toolkit(
        tmp_path,
        passage,
        {"02010101": ["Askanischer Platz", "Wiener Straße"]},
        ["02010101", "area"],
    )

    out = await kit.record_finding(
        claim=passage,
        quote=passage,
        evidence_ids=[evidence_id],
        claim_type="fact",
        localization="02010101",
    )

    finding = kit.accepted[0]
    assert finding.localization == "area"
    assert finding.localization_requested == "02010101"
    assert finding.localization_check == "downgraded"
    assert "recorded at area level" in out and "names the place to localize it" in out
    event = next(payload for kind, payload in emit.events if kind == "localization_downgraded")
    assert event["detail"] == {
        "finding_id": finding.finding_id,
        "requested": "02010101",
        "localization": "area",
        "evidence_ids": [evidence_id],
    }


async def test_named_cell_localization_is_preserved(tmp_path):
    passage = "Ein neues Café am Askanischer Platz eröffnet im Sommer."
    kit, _, evidence_id = _localization_toolkit(
        tmp_path,
        passage,
        {"02010101": ["Askanischer Platz"]},
        ["02010101", "area"],
    )

    await kit.record_finding(
        claim=passage,
        quote=passage,
        evidence_ids=[evidence_id],
        claim_type="fact",
        localization="02010101",
    )

    finding = kit.accepted[0]
    assert finding.localization == "02010101"
    assert finding.localization_check == "mentioned:Askanischer Platz"


@pytest.mark.parametrize(
    ("passage", "expected"),
    [
        ("Ein Café eröffnet am Askanischer Platz.", "02010101"),
        ("Ein Café eröffnet am Kottbusser Tor.", "area"),
    ],
)
async def test_address_localization_falls_back_to_cell_then_area(tmp_path, passage, expected):
    kit, emit, evidence_id = _localization_toolkit(
        tmp_path,
        passage,
        {
            "address": ["Wiener Straße"],
            "02010101": ["Askanischer Platz"],
        },
        ["address", "02010101", "area"],
    )

    await kit.record_finding(
        claim=passage,
        quote=passage,
        evidence_ids=[evidence_id],
        claim_type="fact",
        localization="address",
    )

    finding = kit.accepted[0]
    assert finding.localization == expected
    if expected == "area":
        assert finding.localization_requested == "address"
        assert "localization_downgraded" in emit.types()


@pytest.mark.parametrize(
    ("year", "expected_type"),
    [(2025, "fact"), (2026, "future"), (2027, "future")],
)
async def test_past_completion_year_is_recorded_as_fact(toolkit, monkeypatch, year, expected_type):
    monkeypatch.setattr(prompts, "today", lambda: "2026-10-18")
    kit, _, evidence_id = toolkit
    passage = "The building is complete."
    entry = kit.ledger.add_web_evidence(
        web_evidence("https://example.org/completed", snippet=passage, title="Report")
    )

    out = await kit.record_finding(
        claim=passage,
        quote=passage,
        evidence_ids=[entry["evidence_id"] or evidence_id],
        claim_type="future",
        completion_year=year,
    )

    finding = kit.accepted[0]
    assert finding.claim_type == expected_type
    assert finding.completion_year == year
    if year < 2026:
        assert "completion year 2025 is before today; recorded as fact" in out


@pytest.mark.parametrize(
    ("quote", "ids", "reason"),
    [
        (
            "Am Alexanderplatz eröffnet ein riesiges Einkaufszentrum mit Kino",
            None,
            "Quote not found",
        ),
        (
            "sollen bis 2028 rund 1.200 neue Wohnungen entstehen",
            ["ev_unknown"],
            "Unknown evidence ids",
        ),
        ("sollen bis 2028 rund 1.200 neue Wohnungen entstehen", [], "At least one evidence id"),
    ],
)
async def test_record_finding_rejects(toolkit, quote, ids, reason):
    kit, emit, eid = toolkit
    out = await kit.record_finding(
        claim="claim", quote=quote, evidence_ids=[eid] if ids is None else ids, claim_type="fact"
    )
    assert out.startswith("Finding rejected") and reason in out
    assert not kit.accepted and kit.findings[0].status == "rejected"
    assert emit.types() == ["finding_rejected"]


def test_quote_numbers_german_formats():
    assert {1200.0, 2028.0} <= quote_numbers("bis 2028 rund 1.200 Wohnungen")
    assert 2.5 in quote_numbers("2,5 Prozent")


# -- agent loop with budgets -----------------------------------------------------------------


async def test_agent_records_finding_through_tools(tmp_path):
    connector = StubConnector()
    ledger, emit = EvidenceLedger(tmp_path), Recorder()

    def script(messages: list[BaseMessage], n: int) -> AIMessage:
        if n == 1:
            return tool_call(
                "web_search", {"query": "Wohnungsbau Moritzplatz", "why": "projects"}, n
            )
        if n == 2:
            eid = ledger.entries()[0]["evidence_id"]
            return tool_call("extract_page", {"evidence_id": eid, "why": "details"}, n)
        if n == 3:
            eid = json.loads(messages[-1].content)["evidence_id"]
            args = {
                "claim": "1,200 dwellings by 2028 at Moritzplatz.",
                "quote": "sollen bis 2028 rund 1.200 neue Wohnungen entstehen",
                "evidence_ids": [eid],
                "claim_type": "future",
                "localization": "02010101",
                "unit": "dwellings",
                "amount": 1200,
            }
            return tool_call("record_finding", args, n)
        return AIMessage(content="Recorded one future project.")

    cfg = cfg_with(ScriptedModel(script=script), connector)
    findings = await research_area("q", AREA, CELLS, ledger, emit, cfg)
    assert [f.status for f in findings] == ["accepted"]
    assert findings[0].evidence_ids[0] != ledger.entries()[0]["evidence_id"]
    assert ledger.get(findings[0].evidence_ids[0])["method"] == "tavily-extract"
    assert (connector.searches, connector.extracts) == (1, 1)
    assert {"search", "extract", "model_call", "finding_accepted"} <= set(emit.types())


async def test_agents_pass_place_terms_and_brief_cell_streets(tmp_path, monkeypatch):
    from app.research import agents

    calls = []

    async def capture(**kwargs):
        calls.append(kwargs)
        return []

    monkeypatch.setattr(agents, "_research", capture)
    cfg = ResearchConfig(fake=False)
    ledger, emit = EvidenceLedger(tmp_path), Recorder()
    streets = [f"Street {i}" for i in range(20)]
    cell = {"id": "02010101", "name": "Graefekiez Nord", "streets": streets}

    await agents.research_area("q", AREA, [cell], ledger, emit, cfg)
    await agents.research_cell("q", cell, ledger, emit, cfg)
    await agents.research_hypothesis(
        "q",
        {
            "address": "Wiener Straße 12, 10999 Berlin",
            "cell_id": "02010101",
            "cell_name": "Graefekiez Nord",
            "streets": streets,
        },
        {"id": "h_1"},
        ledger,
        emit,
        cfg,
    )
    await agents.skeptic("q", cell, [], ledger, emit, cfg)

    assert calls[0]["place_terms"]["02010101"][:2] == [
        "Graefekiez Nord",
        "Graefekiez",
    ]
    assert "Street 14" in calls[0]["place_terms"]["02010101"]
    assert calls[1]["place_terms"]["02010101"][-1] == "Street 19"
    assert calls[2]["place_terms"]["address"] == ["Wiener Straße"]
    assert calls[2]["place_terms"]["02010101"][0] == "Graefekiez Nord"
    assert calls[3]["place_terms"]["02010101"][0] == "Graefekiez Nord"
    assert agents._cell_brief(cell)["streets"] == streets[:15]


async def test_budget_exhaustion_stops_agent(tmp_path):
    connector = StubConnector()
    model = ScriptedModel(
        script=lambda _m, n: tool_call("web_search", {"query": f"suche {n}", "why": "loop"}, n)
    )
    emit = Recorder()
    cfg = cfg_with(model, connector, max_searches=2, recursion_limit=200)
    findings = await research_cell("q", CELL, EvidenceLedger(tmp_path), emit, cfg)
    assert findings == []
    assert connector.searches == 2
    exhausted = [p for t, p in emit.events if t == "budget_exhausted"]
    assert exhausted and exhausted[0]["detail"]["resource"] == "searches"
    assert model.calls <= 2 + 1 + cfg.grace_model_calls + 1


async def test_budget_message_returned_after_cap(tmp_path):
    kit = Toolkit(
        ledger=EvidenceLedger(tmp_path),
        events=Events(Recorder(), "researcher:wave2:x"),
        cfg=ResearchConfig(fake=False, max_searches=1),
        connector=StubConnector(),
        role="future",
        localizations=["x"],
        default_localization="x",
    )
    assert (await kit.web_search("a", "why")).startswith("[")
    assert await kit.web_search("b", "why") == BUDGET_MESSAGE
    assert kit.budget.stop_reason == "searches budget exhausted"


async def test_recursion_limit_returns_recorded(tmp_path):
    connector = StubConnector()
    model = ScriptedModel(
        script=lambda _m, n: tool_call("extract_page", {"evidence_id": "ev_x", "why": "w"}, n)
    )
    emit = Recorder()
    cfg = cfg_with(model, connector, recursion_limit=6, max_tool_errors=100)
    assert await research_cell("q", CELL, EvidenceLedger(tmp_path), emit, cfg) == []
    resources = [p["detail"]["resource"] for t, p in emit.events if t == "budget_exhausted"]
    assert resources == ["recursion_limit"]


async def test_skeptic_review_assigns_severity(tmp_path):
    connector = StubConnector()
    ledger, emit = EvidenceLedger(tmp_path), Recorder()

    def script(messages: list[BaseMessage], n: int) -> AIMessage:
        if n == 1:
            return tool_call(
                "web_search", {"query": "Moritzplatz Gewerbemieten", "why": "rents"}, n
            )
        if n == 2:
            eid = ledger.entries()[0]["evidence_id"]
            args = {
                "claim": "Commercial rents in Kreuzberg keep rising.",
                "quote": "Die Gewerbemieten in Kreuzberg steigen weiter.",
                "evidence_ids": [eid],
                "claim_type": "risk",
            }
            return tool_call("record_finding", args, n)
        if n == 3:
            return AIMessage(content="One risk recorded.")
        fid = next(p for t, p in emit.events if t == "finding_accepted")["detail"]["finding_id"]
        return AIMessage(
            content=json.dumps(
                {"reviews": [{"finding_id": fid, "severity": "high", "rationale": "r"}]}
            )
        )

    cfg = cfg_with(ScriptedModel(script=script), connector)
    risks = await skeptic("q", CELL, [], ledger, emit, cfg)
    assert [r["severity"] for r in risks] == ["high"]
    assert risks[0]["evidence_id"] == ledger.entries()[0]["evidence_id"]
    assert {"review", "skeptic_risk"} <= set(emit.types())


async def test_timeout_returns_recorded(tmp_path):
    import asyncio

    class SlowConnector(StubConnector):
        async def search(self, request: ResearchSearch) -> EvidencePage:
            await asyncio.sleep(5)
            return await super().search(request)

    model = ScriptedModel(
        script=lambda _m, n: tool_call("web_search", {"query": "x", "why": "w"}, n)
    )
    emit = Recorder()
    cfg = cfg_with(model, SlowConnector(), timeout_s=0.2)
    assert await research_cell("q", CELL, EvidenceLedger(tmp_path), emit, cfg) == []
    resources = [p["detail"]["resource"] for t, p in emit.events if t == "budget_exhausted"]
    assert resources == ["timeout"]
