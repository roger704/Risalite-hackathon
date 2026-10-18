from datetime import datetime, timezone

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.contracts.events import ResearchEvent
from app.contracts.evidence import LedgerEntry
from app.domain.errors import ValidationError
from app.domain.research import WebEvidence, canonicalize_url
from app.evidence.citations import citation_numbers, strip_unknown_citations
from app.evidence.ledger import EvidenceLedger, snapshot_fields_from_manifest, value_hash
from app.evidence.matching import classify_ratio, quote_ratio
from app.evidence.trace import TraceWriter

NOW = datetime(2026, 10, 18, 13, 0, tzinfo=timezone.utc)
PAGE = (
    "Der Bezirk plant neue Wohnungen. Rund 300 Wohnungen sollen bis 2028 an der Cuvrystraße "
    "bezugsfertig sein. Außerdem entsteht ein Büro-Campus mit 1.200 Arbeitsplätzen am Moritzplatz."
)


def entry(**changes):
    values = {
        "evidence_id": "e1",
        "url": "https://www.berlin.de/a",
        "quote": "q",
        "retrieved_at": NOW,
        "method": "tavily-extract",
        "tier": "official",
        "confidence": "high",
    }
    return LedgerEntry(**(values | changes))


def web_evidence(url="https://www.berlin.de/news", passage=PAGE, kind="extracted_page"):
    return WebEvidence(
        evidence_id="ev_1",
        kind=kind,
        original_url=url,
        canonical_url=canonicalize_url(url),
        title="News",
        query="Kreuzberg Wohnungsbau",
        retrieved_at=NOW,
        published_at=None,
        published_at_raw=None,
        snippet=passage if kind == "search_result" else None,
        passage=passage if kind == "extracted_page" else None,
        passage_locator=None,
        extraction_status="ok",
        failure_reason=None,
        content_hash="abc",
        candidate_id=None,
        analysis_id=None,
        provider="tavily",
        provider_request_id="req-1",
        score=None,
    )


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "JavaScript:alert(1)",
        " javascript:alert(1)",
        "data:text/html;base64,PHNjcmlwdD4=",
        "ftp://example.org/file",
        "file:///etc/passwd",
        "/relative/path",
        "https://",
        "https://user@example.org/",
        "https://example.org/a b",
        "https://example.org/\nx",
    ],
)
def test_ledger_entry_rejects_non_http_urls(url):
    with pytest.raises(PydanticValidationError):
        entry(url=url)


def test_ledger_entry_accepts_http_and_https_and_validates_enums():
    assert entry(url="http://example.org/x?a=1").url == "http://example.org/x?a=1"
    assert entry(url="https://gdi.berlin.de/services/wfs/kita?A=1").citation_number == 1
    for bad in ({"evidence_id": "ev_1"}, {"method": "web"}, {"status": "ok"}, {"quote": ""}):
        with pytest.raises(PydanticValidationError):
            entry(**bad)


def test_ledger_append_assigns_stable_ids_dedupes_and_revises(tmp_path):
    ledger = EvidenceLedger(tmp_path / "runs" / "r1")
    first = ledger.append(
        url="https://www.berlin.de/a",
        quote="A",
        method="tavily-search",
        tier="official",
        confidence="high",
    )
    second = ledger.append(
        url="https://www.berlin.de/b", quote="B", method="wfs", tier="official", confidence="high"
    )
    again = ledger.append(
        url="https://www.berlin.de/a",
        quote="A",
        method="tavily-search",
        tier="official",
        confidence="high",
    )
    assert (first.evidence_id, second.evidence_id, again.evidence_id) == ("e1", "e2", "e1")
    revised = ledger.set_status("e1", "verified")
    assert revised.revision == 1
    assert [e.status for e in ledger.entries()] == ["verified", "unverified"]
    assert len(ledger.history()) == 3
    assert EvidenceLedger(tmp_path / "runs" / "r1").next_id() == "e3"
    with pytest.raises(PydanticValidationError):
        ledger.append(
            url="javascript:alert(1)", quote="x", method="wfs", tier="weak", confidence="low"
        )
    with pytest.raises(ValidationError):
        ledger.set_status("e9", "verified")


def test_converters_from_web_evidence_and_snapshot(tmp_path):
    ledger = EvidenceLedger(tmp_path)
    web = ledger.add_web_evidence(
        web_evidence(), tier="official", confidence="high", quote="Rund 300 Wohnungen"
    )
    assert web.method == "tavily-extract" and web.url == "https://www.berlin.de/news"
    assert web.source_ref.web_evidence_id == "ev_1" and web.retrieved_at == NOW
    search = ledger.add_web_evidence(
        web_evidence(kind="search_result", passage="snippet text"), tier="press", confidence="low"
    )
    assert (search.method, search.quote) == ("tavily-search", "snippet text")
    manifest = {
        "dataset_id": "berlin_population",
        "snapshot_id": "snap-1",
        "resource_url": "https://download.statistik-berlin-brandenburg.de/x.xlsx",
        "retrieved_at": "2026-10-18T10:00:00Z",
    }
    snap = ledger.append(
        **snapshot_fields_from_manifest(
            manifest,
            query={"area_code": "02030301", "field": "value"},
            value=1234,
            quote="1234 residents",
        )
    )
    assert snap.method == "local-snapshot" and snap.source_ref.content_hash == value_hash(1234)
    assert [e.evidence_id for e in ledger.entries()] == ["e1", "e2", "e3"]


def test_quote_ratio_thresholds():
    assert (
        classify_ratio(quote_ratio("„…300 Wohnungen sollen bis 2028 an der Cuvrystraße…“", PAGE))
        == "verified"
    )
    partial = quote_ratio("Rund 400 Wohnungen werden bis 2031 in der Wrangelstraße gebaut", PAGE)
    assert 0.6 <= partial < 0.85 and classify_ratio(partial) == "partial"
    assert (
        classify_ratio(quote_ratio("Footfall on Oranienstraße rose 30 % in 2026", PAGE))
        == "unsupported"
    )


def test_strip_unknown_citations_keeps_ledger_ids_only():
    entries = [entry(evidence_id="e1"), entry(evidence_id="e2", status="unsupported")]
    text = "A [1]. B [2]. C [1, 9]. Link [3](https://x.org) and [^4]. Gone [12]!"
    result = strip_unknown_citations(text, entries)
    assert result.text == "A [1]. B. C [1]. Link [3](https://x.org) and [^4]. Gone!"
    assert result.stripped == (2, 9, 12) and result.kept == (1, 1)
    assert citation_numbers(result.text) == [1, 1]


def test_trace_writer_appends_sequenced_events(tmp_path):
    trace = TraceWriter(tmp_path / "r9")
    trace.emit("Fact-checker", "fact_check.start", "2 sources", "why", data={"n": 2})
    TraceWriter(tmp_path / "r9").emit("Fact-checker", "fact_check.done", "done", "why")
    events = TraceWriter(tmp_path / "r9").read()
    assert [(e.seq, e.run_id, e.type) for e in events] == [
        (0, "r9", "fact_check.start"),
        (1, "r9", "fact_check.done"),
    ]
    assert isinstance(events[0], ResearchEvent)
