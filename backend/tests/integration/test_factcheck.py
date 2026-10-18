import json
from datetime import datetime, timezone

import httpx
import pytest

from app.connectors.tavily import TavilyClient, TavilyConfig
from app.domain.research import EvidencePage, ResearchBudget, WebEvidence, canonicalize_url
from app.evidence.factcheck import FactChecker, fact_check_run, prune_result
from app.evidence.ledger import EvidenceLedger, snapshot_fields, value_hash
from app.evidence.trace import TraceWriter

NOW = datetime(2026, 10, 18, 13, 0, tzinfo=timezone.utc)
NEWS = "https://www.berlin.de/ba-friedrichshain-kreuzberg/aktuelles/wohnungsbau"
RENT = "https://www.example-immo-report.de/berlin/gewerbemieten-2026"
BLOG = "https://www.example-food-blog.de/kreuzberg-hotspots"
WFS = (
    "https://gdi.berlin.de/services/wfs/lor_2021?SERVICE=WFS&REQUEST=GetFeature"
    "&TYPENAMES=lor_2021:a_lor_plr_2021&OUTPUTFORMAT=application/json"
)
PAGES = {
    NEWS: "Rund 300 Wohnungen sollen bis 2028 an der Cuvrystraße bezugsfertig sein.",
    RENT: "Rund 300 Wohnungen sollen bis 2028 an der Cuvrystraße bezugsfertig sein. "
    "Die Erdgeschossmieten stiegen moderat.",
    BLOG: "Our favourite brunch spots in Kreuzberg, ranked.",
}
FEATURES = {
    "type": "FeatureCollection",
    "features": [
        {"type": "Feature", "properties": {"plr_id": "02030301", "plr_name": "Wrangelkiez"}}
    ],
}


class FakeTavily:
    def __init__(self):
        self.requests = []

    async def search(self, request):  # pragma: no cover - unused
        raise NotImplementedError

    async def extract(self, request):
        self.requests.append(request)
        evidence = tuple(
            WebEvidence(
                evidence_id=f"ev_{i}",
                kind="extracted_page",
                original_url=url,
                canonical_url=canonicalize_url(url),
                title=None,
                query=None,
                retrieved_at=NOW,
                published_at=None,
                published_at_raw=None,
                snippet=None,
                passage=PAGES[url],
                passage_locator=None,
                extraction_status="ok",
                failure_reason=None,
                content_hash=None,
                candidate_id=None,
                analysis_id=None,
                provider="tavily",
                provider_request_id=None,
                score=None,
            )
            for i, url in enumerate(request.urls)
            if url in PAGES
        )
        failures = tuple({"url": u, "reason": "404"} for u in request.urls if u not in PAGES)
        return EvidencePage(evidence, "partial" if failures else "complete", failures)


def wfs_transport(request):
    if request.url.host == "gdi.berlin.de":
        return httpx.Response(200, json=FEATURES)
    return httpx.Response(404)


def build_run(run_dir):
    ledger = EvidenceLedger(run_dir)
    add = ledger.append
    add(
        url=WFS, quote='"plr_name": "Wrangelkiez"', method="wfs", tier="official", confidence="high"
    )
    add(
        url=NEWS,
        quote="…300 Wohnungen sollen bis 2028 an der Cuvrystraße bezugsfertig sein…",
        method="tavily-extract",
        tier="official",
        confidence="high",
        claim_type="forecast",
    )
    add(
        url=RENT,
        quote="Rund 400 Wohnungen werden bis 2031 in der Wrangelstraße gebaut",
        method="tavily-extract",
        tier="press",
        confidence="medium",
    )
    add(
        url=BLOG,
        quote="Footfall on Oranienstraße rose 30 % in 2026",
        method="tavily-search",
        tier="weak",
        confidence="low",
    )
    add(
        **snapshot_fields(
            url="https://download.statistik-berlin-brandenburg.de/pop.xlsx",
            dataset_id="berlin_population",
            snapshot_id="s1",
            query={"area_code": "02030301"},
            value=4200,
            quote="4200 residents",
            retrieved_at=NOW,
        )
    )
    add(
        **snapshot_fields(
            url="https://download.statistik-berlin-brandenburg.de/pop.xlsx",
            dataset_id="berlin_population",
            snapshot_id="s1",
            query={"area_code": "02030302"},
            value=999,
            quote="999 residents",
            retrieved_at=NOW,
        )
    )
    (run_dir / "cells.json").write_text(
        json.dumps(
            {
                "area": {"name": "Friedrichshain-Kreuzberg", "level": "plr", "cell_count": 2},
                "mode_default": "present",
                "layers": [
                    {
                        "id": "demand_density",
                        "direction": "+",
                        "weight_default": 0.3,
                        "source": WFS,
                        "evidence_id": "e1",
                    },
                    {
                        "id": "residents",
                        "direction": "+",
                        "weight_default": 0.2,
                        "source": WFS,
                        "evidence_id": "e6",
                    },
                ],
                "cells": [
                    {
                        "id": "02030301",
                        "name": "Wrangelkiez",
                        "values": {"demand_density": 0.8},
                        "modifiers": [
                            {
                                "type": "future",
                                "label": "Planned housing",
                                "quote": "…",
                                "evidence_id": "e2",
                                "bonus": 0.1,
                            },
                            {
                                "type": "future",
                                "label": "Footfall up",
                                "quote": "…",
                                "evidence_id": "e4",
                                "bonus": 0.1,
                            },
                            {
                                "type": "future",
                                "label": "Hallucinated",
                                "quote": "…",
                                "evidence_id": "e99",
                                "bonus": 0.1,
                            },
                        ],
                        "risks": [
                            {"label": "Rents rising", "severity": "high", "evidence_id": "e3"}
                        ],
                        "score_present": 0.6,
                        "score_future": 0.7,
                    },
                ],
            }
        )
    )
    (run_dir / "candidates.json").write_text(
        json.dumps(
            {
                "hypotheses": [
                    {
                        "name": "Craft bakery-café",
                        "evidence_ids": ["e2", "e4"],
                        "for": [
                            {"claim": "Housing growth", "evidence_id": "e2"},
                            {"claim": "Footfall up 30 %", "evidence_id": "e4"},
                        ],
                        "against": [{"claim": "Residents", "evidence_ids": ["e5", "e4"]}],
                        "unknowns": [
                            {"question": "Ventilation?", "suggested_validation": "Ask landlord"}
                        ],
                    },
                ]
            }
        )
    )
    (run_dir / "report.md").write_text(
        "Wrangelkiez ranks first [1][5]. Housing is planned [2]. Footfall rose [4]. "
        "Rents rise [3]. Hallucinated [42].\n"
    )
    return ledger


def snapshot_reader(ref):
    return {"02030301": 4200, "02030302": 1000}[ref.query["area_code"]]


async def test_fact_check_run_classifies_prunes_and_traces(tmp_path):
    run_dir = tmp_path / "run1"
    ledger = build_run(run_dir)
    http = httpx.AsyncClient(transport=httpx.MockTransport(wfs_transport))
    tavily = FakeTavily()
    checker = FactChecker(research=tavily, http=http, snapshot_reader=snapshot_reader)
    report = await fact_check_run(run_dir, checker)

    statuses = {e.evidence_id: e.status for e in ledger.entries()}
    assert statuses == {
        "e1": "verified",
        "e2": "verified",
        "e3": "partial",
        "e4": "unsupported",
        "e5": "verified",
        "e6": "unsupported",
    }
    assert report.counts() == {"verified": 3, "partial": 1, "unsupported": 2}
    assert tavily.requests[0].urls == (NEWS, RENT, BLOG)

    cells = json.loads((run_dir / "cells.json").read_text())
    cell = cells["cells"][0]
    assert [m["evidence_id"] for m in cell["modifiers"]] == ["e2"]
    assert cell["risks"][0]["evidence_id"] == "e3"
    assert cells["layers"][1]["fact_check"] == "unsupported"
    assert "fact_check" not in cells["layers"][0]
    dropped = {(d["evidence_id"], d.get("action")) for d in cells["could_not_verify"]}
    assert {("e4", "dropped"), ("e99", "dropped"), ("e6", "flagged")} <= dropped
    assert ("e4", None) in dropped and ("e6", None) in dropped  # ledger-level appendix

    candidates = json.loads((run_dir / "candidates.json").read_text())
    hypothesis = candidates["hypotheses"][0]
    assert hypothesis["evidence_ids"] == ["e2"]
    assert [f["claim"] for f in hypothesis["for"]] == ["Housing growth"]
    assert hypothesis["against"][0]["evidence_ids"] == ["e5"]
    assert hypothesis["unknowns"][0]["question"] == "Ventilation?"

    text = (run_dir / "report.md").read_text()
    assert "[4]" not in text and "[42]" not in text and "[1][5]" in text
    assert "## Could not verify" in text and "e4" in text
    assert sorted(report.stripped_citations) == [4, 42]

    events = TraceWriter(run_dir).read()
    types = [e.type for e in events]
    assert types[0] == "fact_check.start" and types[-1] == "fact_check.done"
    assert types.count("fact_check.result") == 6
    rejects = [e for e in events if e.type == "fact_check.reject"]
    assert {e.evidence_id for e in rejects} == {"e4", "e6"}
    assert "citations.stripped" in types
    summary = json.loads((run_dir / "factcheck.json").read_text())
    assert summary["thresholds"] == {"verified": 0.85, "partial": 0.6}

    assert cells["rescore_required"] is True
    again = await fact_check_run(run_dir, checker)  # idempotent: nothing left to check
    assert again.results == [] and "[1][5]" in (run_dir / "report.md").read_text()
    await http.aclose()


async def test_wfs_failures_and_host_allowlist(tmp_path):
    ledger = EvidenceLedger(tmp_path)
    for method, url in (("wfs", WFS), ("wfs-cached", WFS), ("wfs", "https://evil.example/wfs")):
        ledger.append(
            url=url,
            quote=f"layer {method} {url}",
            method=method,
            tier="official",
            confidence="high",
        )
    down = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    results = await FactChecker(http=down).check(ledger.entries())
    assert [r.status for r in results] == ["unsupported", "partial", "unsupported"]
    assert "not allow-listed" in results[2].reason
    up = httpx.AsyncClient(transport=httpx.MockTransport(wfs_transport))
    results = await FactChecker(http=up).check(ledger.entries()[:1])
    assert results[0].status == "partial" and "1 features" in results[0].reason
    await down.aclose()
    await up.aclose()


async def test_snapshot_without_reader_and_value_hash(tmp_path):
    ledger = EvidenceLedger(tmp_path)
    ledger.append(
        **snapshot_fields(
            url="https://example.org/x",
            dataset_id="d",
            snapshot_id="s",
            query={},
            value=[1, 2],
            quote="values 1 and 2",
            retrieved_at=NOW,
        )
    )
    [result] = await FactChecker().check(ledger.entries())
    assert result.status == "unsupported" and result.reason == "no snapshot reader"
    [result] = await FactChecker(snapshot_reader=lambda ref: [1, 2]).check(ledger.entries())
    assert result.status == "verified" and ledger.entries()[
        0
    ].source_ref.content_hash == value_hash([1, 2])


async def public_resolver(host, port):
    return ["93.184.216.34"]


async def test_fact_checker_with_real_tavily_client_and_mock_transport(tmp_path):
    def handler(request):
        assert request.url.path == "/extract"
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "request_id": "req-x",
                "results": [
                    {"url": u, "raw_content": PAGES[u]} for u in payload["urls"] if u in PAGES
                ],
                "failed_results": [
                    {"url": u, "error": "404"} for u in payload["urls"] if u not in PAGES
                ],
            },
        )

    client = TavilyClient(
        TavilyConfig(api_key="test-key"),
        ResearchBudget(0, 10, 0, 0),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        resolver=public_resolver,
    )
    ledger = EvidenceLedger(tmp_path)
    ledger.append(
        url=NEWS,
        quote="300 Wohnungen sollen bis 2028",
        method="tavily-extract",
        tier="official",
        confidence="high",
    )
    ledger.append(
        url="https://www.berlin.de/missing",
        quote="anything",
        method="tavily-search",
        tier="official",
        confidence="high",
    )
    results = await FactChecker(research=client).check(ledger.entries())
    assert [r.status for r in results] == ["verified", "unsupported"]
    assert "404" in results[1].reason
    await client._client.aclose()


async def test_plain_http_fallback_without_tavily(tmp_path):
    html = f"<html><script>x()</script><body><p>{PAGES[NEWS]}</p></body></html>"
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text=html))
    )
    ledger = EvidenceLedger(tmp_path)
    ledger.append(
        url=NEWS,
        quote="300 Wohnungen sollen bis 2028",
        method="tavily-extract",
        tier="official",
        confidence="high",
    )
    [result] = await FactChecker(http=http, resolver=public_resolver).check(ledger.entries())
    assert (result.status, result.fetch_method) == ("verified", "http-get")
    await http.aclose()


@pytest.mark.live
async def test_live_fact_check_real_page(tmp_path):
    import os

    key = os.environ.get("TAVILY_API_KEY")
    if not key:
        pytest.skip("TAVILY_API_KEY not set")
    url = "https://www.berlin.de/ba-friedrichshain-kreuzberg/"
    async with TavilyClient(
        TavilyConfig(api_key=key, max_passage_chars=200_000), ResearchBudget(0, 2, 0, 0)
    ) as client:
        ledger = EvidenceLedger(tmp_path)
        ledger.append(
            url=url,
            quote="Friedrichshain-Kreuzberg",
            method="tavily-extract",
            tier="official",
            confidence="high",
        )
        [result] = await FactChecker(research=client).check(ledger.entries())
    print(result)
    assert result.status == "verified"


async def test_requery_rejects_ports_and_redirects(tmp_path):
    seen = []

    def handler(request):
        seen.append(request.url.host)
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest"})

    ledger = EvidenceLedger(tmp_path)
    for url in ("https://gdi.berlin.de:8443/wfs", WFS):
        ledger.append(url=url, quote="x", method="wfs", tier="official", confidence="high")
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    port, redirect = await FactChecker(http=http).check(ledger.entries())
    assert port.status == "unsupported" and "not allow-listed" in port.reason
    assert redirect.status == "unsupported" and "HTTP 302" in redirect.reason
    assert seen == ["gdi.berlin.de"]
    await http.aclose()


async def test_web_fallback_revalidates_redirect_hops(tmp_path):
    def handler(request):
        if request.url.host == "www.berlin.de":
            return httpx.Response(301, headers={"location": "http://internal.example/secret"})
        return httpx.Response(200, text=PAGES[NEWS])

    async def resolver(host, port):
        return ["10.0.0.5"] if host == "internal.example" else ["93.184.216.34"]

    ledger = EvidenceLedger(tmp_path)
    ledger.append(
        url=NEWS, quote="300 Wohnungen", method="tavily-extract", tier="official", confidence="high"
    )
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    [result] = await FactChecker(http=http, resolver=resolver).check(ledger.entries())
    assert result.status == "unsupported" and "UnsafeURLError" in result.reason
    await http.aclose()


async def test_feature_count_alone_is_only_partial(tmp_path):
    ledger = EvidenceLedger(tmp_path)
    ledger.append(
        url=WFS,
        quote="one planning area",
        method="wfs",
        tier="official",
        confidence="high",
        source_ref={"feature_count": 1},
    )
    http = httpx.AsyncClient(transport=httpx.MockTransport(wfs_transport))
    [result] = await FactChecker(http=http).check(ledger.entries())
    assert result.status == "partial" and "cited 1 features" in result.reason
    await http.aclose()


@pytest.mark.parametrize(
    ("quote", "status"),
    [
        ("4200 residents", "verified"),
        ("4.200 Einwohner", "verified"),
        ("4,200 residents", "verified"),
        ("9000 residents", "partial"),
    ],
)
async def test_snapshot_quote_must_state_the_value(tmp_path, quote, status):
    ledger = EvidenceLedger(tmp_path)
    ledger.append(
        **snapshot_fields(
            url="https://example.org/pop.xlsx",
            dataset_id="d",
            snapshot_id="s",
            query={},
            value=4200,
            quote=quote,
            retrieved_at=NOW,
        )
    )
    [result] = await FactChecker(snapshot_reader=lambda ref: 4200).check(ledger.entries())
    assert result.status == status


def test_prune_flags_nested_claims_and_replaces_rejected_singular_id():
    result = {
        "summary": {"claim": "Footfall rose", "evidence_ids": ["e4"]},
        "risks": [{"label": "Rents", "evidence_id": "e4", "evidence_ids": ["e4", "e1"]}],
    }
    pruned, records = prune_result(result, {"e1"})
    assert pruned["summary"]["fact_check"] == "unsupported"
    assert pruned["summary"]["evidence_ids"] == []
    assert pruned["risks"][0]["evidence_id"] == "e1"
    assert pruned["risks"][0]["evidence_ids"] == ["e1"]
    assert [(r["path"], r["action"]) for r in records] == [("summary", "flagged")]
    again, _ = prune_result(pruned | {"summary": {"claim": "ok", "evidence_ids": ["e1"]}}, {"e1"})
    assert "fact_check" not in again["summary"]


async def test_recheck_restores_claims_from_originals(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    ledger = build_run(run_dir)
    http = httpx.AsyncClient(transport=httpx.MockTransport(wfs_transport))
    checker = FactChecker(research=FakeTavily(), http=http, snapshot_reader=snapshot_reader)
    await fact_check_run(run_dir, checker)
    assert "[4]" not in (run_dir / "report.md").read_text()
    monkeypatch.setitem(PAGES, BLOG, "Footfall on Oranienstraße rose 30 % in 2026.")
    await fact_check_run(run_dir, checker, recheck=True)
    assert ledger.get("e4").status == "verified"
    cell = json.loads((run_dir / "cells.json").read_text())["cells"][0]
    assert [m["evidence_id"] for m in cell["modifiers"]] == ["e2", "e4"]
    assert "Footfall rose [4]" in (run_dir / "report.md").read_text()
    await http.aclose()


async def test_checker_closes_only_its_own_client():
    owned = FactChecker()
    client = await owned._client()
    await owned.aclose()
    assert client.is_closed and owned.http is None
    injected = httpx.AsyncClient()
    async with FactChecker(http=injected) as checker:
        await checker._client()
    assert not injected.is_closed
    await injected.aclose()
