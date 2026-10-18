import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.domain.errors import ValidationError
from app.domain.research import ExtractionStatus
from app.evidence.citations import citable, render_citations, strip_unknown_citations
from app.evidence.factcheck import CheckResult, FactChecker, fact_check_run
from app.evidence.judge import judge_run
from app.evidence.ledger import EvidenceLedger, dataset_entry
from app.evidence.matching import classify_ratio, quote_ratio
from app.evidence.trace import TraceWriter

ROOT = Path(__file__).resolve().parents[3]
PAGE_A = "<html><body><p>Die Erdgeschossmieten im Wrangelkiez stiegen um 18 % gegenüber dem Vorjahr.</p></body></html>"
PAGE_B = "<p>Ein neuer Bürocampus am Moritzplatz soll 2027 eröffnen.</p>"


async def public_resolver(host, port):
    return ["93.184.216.34"]


def web(eid, url, **extra):
    return {"evidence_id": eid, "url": url, "method": "tavily-search", "tier": "press", **extra}


def handler(request: httpx.Request) -> httpx.Response:
    pages = {"/a": PAGE_A, "/b": PAGE_B}
    if request.url.path == "/moved":
        return httpx.Response(302, headers={"location": "http://127.0.0.1/secret"})
    if request.url.path == "/rate-limited":
        return httpx.Response(429)
    if request.url.path in pages:
        return httpx.Response(200, text=pages[request.url.path])
    return httpx.Response(404)


def checker(**kwargs):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return FactChecker(http=http, resolver=public_resolver, **kwargs)


def write_findings(run_dir, findings):
    (run_dir / "findings.jsonl").write_text("".join(json.dumps(f) + "\n" for f in findings))


def test_quote_matching_thresholds():
    text = "Die Erdgeschossmieten im Wrangelkiez stiegen um 18 % gegenüber dem Vorjahr."
    assert classify_ratio(quote_ratio("„Erdgeschossmieten im Wrangelkiez stiegen“", text)) == (
        "verified"
    )
    assert classify_ratio(quote_ratio("Footfall on Oranienstraße rose 30%", text)) == "unsupported"


def test_ledger_appends_revisions_and_reads_latest(tmp_path):
    ledger = EvidenceLedger(tmp_path)
    entry = ledger.append(web("ev_a", "https://example.org/a", quote="x"))
    assert ledger.append({**entry, "quote": "changed"})["quote"] == "x"
    ledger.set_status("ev_a", "verified", {"ratio": 1.0})
    assert len(ledger.history()) == 2
    assert ledger.get("ev_a")["status"] == "verified"
    assert ledger.get("ev_a")["revision"] == 1
    with pytest.raises(ValidationError):
        ledger.set_status("ev_missing", "verified")


def test_dataset_entry_id_is_deterministic():
    kwargs = dict(
        dataset_id="lor", snapshot_id="s1", layer_id="demand", source_url="https://x", value=[1]
    )
    assert dataset_entry(**kwargs)["evidence_id"] == dataset_entry(**kwargs)["evidence_id"]
    assert dataset_entry(**kwargs)["evidence_id"].startswith("ds_")


def test_strip_unknown_citations_uses_export_numbering():
    entries = [
        web("ev_a", "https://a", status="verified"),
        web("ev_bad", "https://b", status="unsupported"),
        web("ev_c", "https://c", status="partial"),
    ]
    assert citable(entries)["2"]["evidence_id"] == "ev_c"
    result = strip_unknown_citations("A [1]. B [ev_bad]. C [2, 9]. [link](https://x) [^1]", entries)
    assert result.text == "A [1]. B. C [2]. [link](https://x) [^1]"
    assert result.stripped == ("ev_bad", "9")


def test_render_citations_drops_marked_sentences_and_keeps_supported_neighbor():
    entries = [
        web("ev_drop", "https://example.org/drop", status="verified"),
        web("ev_keep", "https://example.org/keep", status="verified"),
    ]

    result = render_citations(
        "Before-period marker [ev_drop].\n"
        "After-period marker. [ev_drop]\n"
        "Dropped sentence [ev_drop]. Kept sentence [ev_keep].",
        entries,
        exclude={"ev_drop"},
    )

    assert result.text == "Kept sentence [2]."
    assert result.stripped == ("ev_drop", "ev_drop", "ev_drop")


async def test_fact_check_run_verifies_partial_and_drops(tmp_path):
    ledger = EvidenceLedger(tmp_path)
    ledger.extend(
        [
            web("ev_a", "https://example.org/a"),
            web("ev_b", "https://example.org/b"),
            web("ev_gone", "https://example.org/missing"),
        ]
    )
    ds = ledger.add_dataset_value(
        dataset_id="lor",
        snapshot_id="s1",
        layer_id="demand",
        source_url="https://gdi.berlin.de",
        value={"population": 12},
    )
    quote = "Erdgeschossmieten im Wrangelkiez stiegen um 18 %"
    write_findings(
        tmp_path,
        [
            {
                "finding_id": "f_ok",
                "claim": "Rents +18%",
                "quote": quote,
                "evidence_ids": ["ev_a"],
                "status": "accepted",
            },
            {
                "finding_id": "f_mix",
                "claim": "Rents",
                "quote": quote,
                "evidence_ids": ["ev_a", "ev_b"],
                "status": "accepted",
            },
            {
                "finding_id": "f_bad",
                "claim": "Footfall +30%",
                "quote": "Footfall rose 30 % on Oranienstraße",
                "evidence_ids": ["ev_b"],
                "status": "accepted",
            },
            {
                "finding_id": "f_old",
                "claim": "x",
                "quote": "x",
                "evidence_ids": ["ev_a"],
                "status": "rejected",
            },
        ],
    )
    (tmp_path / "report.md").write_text("Rents rose [1] [ev_a]. Campus [2]. Ghost [7].\n")

    async with checker(snapshot_reader=lambda entry: {"population": 12}) as fc:
        report = await fact_check_run(tmp_path, fc)

    statuses = {r.id: r.status for r in report.findings}
    assert statuses == {"f_ok": "verified", "f_mix": "partial", "f_bad": "unsupported"}
    findings = {f["finding_id"]: f for f in map(json.loads, (tmp_path / "findings.jsonl").open())}
    assert findings["f_mix"]["evidence_ids"] == ["ev_a"]
    assert findings["f_mix"]["cited_evidence_ids"] == ["ev_a", "ev_b"]
    assert "verification" not in findings["f_old"]
    assert ledger.get("ev_gone")["status"] == "unsupported"
    assert ledger.get(ds["evidence_id"])["status"] == "verified"

    text = (tmp_path / "report.md").read_text()
    assert text.startswith("Rents rose [1] [1].\n")
    assert "Ghost" not in text and "Campus" not in text
    assert "## Could not verify" in text and "Footfall +30%" in text
    saved = json.loads((tmp_path / "factcheck.json").read_text())
    assert saved["counts"]["findings"] == {"verified": 1, "partial": 1, "unsupported": 1}
    events = [json.loads(x) for x in (tmp_path / "trace.jsonl").open()]
    assert {e["type"] for e in events} == {"fact_check"}
    assert [e["seq"] for e in events] == list(range(len(events)))

    async with checker(snapshot_reader=lambda entry: {"population": 12}) as fc:
        again = await fact_check_run(tmp_path, fc)
    assert again.to_dict()["counts"] == saved["counts"]
    assert (tmp_path / "report.md").read_text() == text

    async with checker(snapshot_reader=lambda entry: {"population": 12}) as fc:
        await fact_check_run(tmp_path, fc, recheck=True)
    assert (tmp_path / "report.md").read_text().count("## Could not verify") == 1


async def test_prefetch_batches_more_than_thirty_web_entries():
    entries = [web(f"ev_{index}", f"https://example.org/{index}") for index in range(45)]

    class FakeResearch:
        def __init__(self):
            self.calls = []
            self.fetched = []

        async def extract(self, request):
            self.calls.append(request.urls)
            if len(request.urls) > 20:
                raise RuntimeError("too many URLs")
            self.fetched.extend(request.urls)
            evidence = tuple(
                SimpleNamespace(
                    extraction_status=ExtractionStatus.OK,
                    passage=f"Fetched {url}",
                    snippet="",
                    original_url=url,
                    canonical_url=url,
                )
                for url in request.urls
            )
            return SimpleNamespace(evidence=evidence, failures=())

    research = FakeResearch()
    async with FactChecker(research=research) as fact_checker:
        await fact_checker.prefetch(entries)
        fetched = [await fact_checker.fetch(entry) for entry in entries]

    assert [len(batch) for batch in research.calls] == [20, 20, 5]
    assert research.fetched == [entry["url"] for entry in entries]
    assert [item.text for item in fetched] == [f"Fetched {entry['url']}" for entry in entries]


async def test_fact_check_only_checks_and_prefetches_selected_ids(tmp_path):
    ledger = EvidenceLedger(tmp_path)
    ledger.extend(
        [
            web("ev_a", "https://example.org/a", quote="quote A"),
            web("ev_b", "https://example.org/b", quote="quote B"),
        ]
    )
    requested = []

    class Research:
        async def extract(self, request):
            requested.extend(request.urls)
            return SimpleNamespace(
                evidence=[],
                failures=[{"url": url, "reason": "HTTP 429"} for url in request.urls],
            )

    async with FactChecker(research=Research()) as fc:
        report = await fact_check_run(tmp_path, fc, only_ids={"ev_a"})

    assert requested == ["https://example.org/a"]
    assert [item.id for item in report.evidence] == ["ev_a"]
    assert ledger.get("ev_a")["status"] == "unsupported"
    assert ledger.get("ev_b").get("status", "unverified") == "unverified"


async def test_fact_check_entries_run_concurrently_but_write_in_ledger_order(tmp_path, monkeypatch):
    monkeypatch.setenv("RISALITE_FACTCHECK_CONCURRENCY", "2")
    evidence_ids = ["ev_slow", "ev_medium", "ev_fast"]
    EvidenceLedger(tmp_path).extend(
        [web(evidence_id, f"https://example.org/{evidence_id}") for evidence_id in evidence_ids]
    )

    class DelayedChecker(FactChecker):
        def __init__(self):
            super().__init__(offline=True)
            self.active = 0
            self.max_active = 0
            self.completed = []

        async def check_entry(self, entry):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                await asyncio.sleep(
                    {"ev_slow": 0.05, "ev_medium": 0.01, "ev_fast": 0.001}[entry["evidence_id"]]
                )
                self.completed.append(entry["evidence_id"])
                return CheckResult(entry["evidence_id"], "evidence", "verified", 1.0, "test")
            finally:
                self.active -= 1

    async with DelayedChecker() as fc:
        report = await fact_check_run(tmp_path, fc)

    assert fc.max_active == 2
    assert fc.completed != evidence_ids
    assert [item.id for item in report.evidence] == evidence_ids
    assert [item["evidence_id"] for item in EvidenceLedger(tmp_path).history()[-3:]] == (
        evidence_ids
    )
    events = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()]
    check_events = [event for event in events if event.get("detail", {}).get("evidence_ids")]
    assert [event["detail"]["summary"].split()[0] for event in check_events] == evidence_ids


async def test_recheck_failure_uses_a_matching_stored_extract():
    quote = "Erdgeschossmieten im Wrangelkiez stiegen um 18 %"
    stored = web(
        "ev_stored",
        "https://example.org/rate-limited",
        quote=quote,
        passage="Die Erdgeschossmieten im Wrangelkiez stiegen um 18 % gegenüber dem Vorjahr.",
    )
    missing = web("ev_missing", "https://example.org/rate-limited", quote=quote)

    async with checker() as fc:
        stored_result = await fc.check_entry(stored)
        missing_result = await fc.check_entry(missing)
        finding_result = await fc.check_finding(
            {"finding_id": "f_stored", "quote": quote, "evidence_ids": ["ev_stored"]},
            {"ev_stored": stored},
        )

    assert stored_result.status == "partial"
    assert stored_result.reason == ("re-fetch failed (HTTP 429); matched stored extract")
    assert finding_result.status == "partial"
    assert finding_result.matched_ids == ("ev_stored",)
    assert missing_result.status == "unsupported"


async def test_rejected_entry_renumbers_citations_and_cannot_verify(tmp_path):
    EvidenceLedger(tmp_path).extend(
        [
            web("ev_x", "https://example.org/a", quote="rent doubled overnight in Kreuzberg"),
            web("ev_a", "https://example.org/a"),
            web("ev_b", "https://example.org/b"),
        ]
    )
    quote = "Erdgeschossmieten im Wrangelkiez stiegen um 18 %"
    write_findings(
        tmp_path,
        [{"finding_id": "f1", "claim": "c", "quote": quote, "evidence_ids": ["ev_x"]}],
    )
    (tmp_path / "report.md").write_text("Wrong [1]. Rents [2]. Campus [3].\n")
    async with checker() as fc:
        report = await fact_check_run(tmp_path, fc)
    assert {r.id: r.status for r in report.findings} == {"f1": "unsupported"}
    assert "ledger entry unsupported" in report.findings[0].reason
    assert (tmp_path / "report.md").read_text().startswith("Rents [1]. Campus [2].\n")


async def test_offline_dataset_finding_is_partial(tmp_path):
    ds = EvidenceLedger(tmp_path).add_dataset_value(
        dataset_id="lor", snapshot_id="s1", layer_id="d", source_url="https://x", value=1
    )
    finding = {"finding_id": "f", "quote": "q", "evidence_ids": [ds["evidence_id"]]}
    async with FactChecker(offline=True) as fc:
        result = await fc.check_finding(finding, {ds["evidence_id"]: ds})
    assert result.status == "partial" and result.matched_ids == (ds["evidence_id"],)


async def test_get_public_pins_validated_address():
    seen = []
    answers = iter([["93.184.216.34"], ["127.0.0.1"]])

    async def rebinding(host, port):
        return next(answers)

    def record(request):
        seen.append((request.url.host, request.headers["host"], request.extensions))
        return httpx.Response(200, text="ok")

    http = httpx.AsyncClient(transport=httpx.MockTransport(record))
    async with FactChecker(http=http, resolver=rebinding) as fc:
        assert (await fc.get_public("https://example.org/a")).status_code == 200
    host, header, extensions = seen[0]
    assert (host, header, extensions["sni_hostname"]) == (
        "93.184.216.34",
        "example.org",
        "example.org",
    )


async def test_changed_snapshot_and_unlisted_requery_host_are_unsupported():
    async with checker(snapshot_reader=lambda entry: {"population": 13}) as fc:
        ds = dataset_entry(
            dataset_id="lor", snapshot_id="s1", layer_id="d", source_url="https://x", value=12
        )
        assert (await fc.check_entry(ds)).status == "unsupported"
        wfs = {
            "evidence_id": "e1",
            "method": "wfs",
            "url": "https://evil.example/wfs",
            "quote": "q",
        }
        result = await fc.check_entry(wfs)
        assert result.status == "unsupported" and "allow-listed" in result.reason


async def test_redirect_to_private_host_is_blocked():
    async with checker() as fc:
        result = await fc.check_entry(web("ev_r", "https://example.org/moved", quote="secret"))
    assert result.status == "unsupported"
    assert "re-fetch failed" in result.reason


async def test_judge_scores_report(tmp_path):
    EvidenceLedger(tmp_path).extend(
        [web("ev_a", "https://example.org/a", quote="Erdgeschossmieten im Wrangelkiez")]
    )
    (tmp_path / "report.md").write_text("Rents [1]. Unknown [4].")
    async with checker() as fc:
        report = await judge_run(tmp_path, fc)
    scores = {(c.check, c.id): c.score for c in report.checks}
    assert scores == {
        ("citation", "1"): 1.0,
        ("citation", "4"): 0.0,
        ("url", "ev_a"): 1.0,
        ("quote", "ev_a"): 1.0,
    }
    assert not report.passed


async def test_verify_citations_script_offline_on_fixtures(capsys):
    sys.path.insert(0, str(ROOT / "scripts"))
    import verify_citations

    assert await verify_citations.main([str(ROOT / "frontend/fixtures"), "--offline"]) == 0
    assert "PASS" in capsys.readouterr().out


def test_trace_writer_continues_sequence(tmp_path):
    TraceWriter(tmp_path, "r1").emit("fact_check", "factcheck", "a")
    event = TraceWriter(tmp_path, "r1").emit("fact_check", "factcheck", "b")
    assert event["seq"] == 1 and event["run_id"] == "r1"


async def test_pin_screens_url_before_dns_and_ignores_proxy_env():
    calls = []

    async def resolver(host, port):
        calls.append(host)
        return ["93.184.216.34"]

    async with FactChecker(resolver=resolver) as fc:
        for url in (
            "ftp://example.org/a",
            "https://user@example.org/",
            "https://example.org:8443/",
        ):
            with pytest.raises(Exception):
                await fc.get_public(url)
        assert calls == []
        assert (await fc._client()).trust_env is False
