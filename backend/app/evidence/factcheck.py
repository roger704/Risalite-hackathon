"""Deterministic fact-checker (Deep_Research_Spec §5.6).

Re-fetches every cited source and re-matches quotes with the same matcher as
``record_finding`` (``app.evidence.matching``):

* web entries (``tavily-search`` / ``tavily-extract`` / ``web``) -> Tavily ``extract``, or a
  plain HTTP GET that re-validates every redirect hop when no connector is configured;
* ``wfs`` / ``wfs-cached`` / ``overpass`` -> re-run the exact GET (allow-listed hosts only);
* ``dataset`` -> re-read the pinned snapshot and compare the value hash.

Findings are ``verified`` when every cited id matches, ``partial`` when some do (only the
matching ids are kept) and ``unsupported`` otherwise. Unsupported findings and ledger entries
are listed under "Could not verify" in ``report.md`` and ``factcheck.json``.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
from dataclasses import asdict, dataclass, field
from inspect import isawaitable
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

import httpx

from app.connectors.tavily import (
    UnsafeURLError,
    _address,
    _default_resolver,
    validate_public_url,
)
from app.domain.research import ExtractionStatus, ResearchConnector, ResearchExtract
from app.evidence.citations import pin_citations, render_citations
from app.evidence.ledger import (
    EvidenceLedger,
    entry_status,
    entry_text,
    entry_url,
    utc_now,
    value_hash,
)
from app.evidence.matching import (
    PARTIAL_RATIO,
    VERIFIED_RATIO,
    classify_ratio,
    html_to_text,
    quote_ratio,
)
from app.evidence.trace import TraceWriter

WEB_METHODS = {"tavily-search", "tavily-extract", "web"}
REQUERY_METHODS = {"wfs", "wfs-cached", "overpass"}
DEFAULT_REQUERY_HOSTS = ("gdi.berlin.de", "fbinter.stadt-berlin.de", "overpass-api.de")
MAX_REDIRECTS = 5
FETCH_TIMEOUT_S = 10.0
PREFETCH_BATCH_TIMEOUT_S = float(os.environ.get("RISALITE_FACTCHECK_BATCH_TIMEOUT_S", "90"))
DEFAULT_FACTCHECK_CONCURRENCY = 8
FACTCHECK_FILE = "factcheck.json"
FINDINGS_FILE = "findings.jsonl"
FACT_CHECKED_MARK = "<!-- fact-checked: see factcheck.json -->"
AGENT = "factcheck"

SnapshotReader = Callable[[dict[str, Any]], Any]


def _factcheck_concurrency() -> int:
    try:
        return max(1, int(os.environ.get("RISALITE_FACTCHECK_CONCURRENCY", "8")))
    except ValueError:
        return DEFAULT_FACTCHECK_CONCURRENCY


@dataclass(frozen=True)
class Fetched:
    text: str | None
    how: str
    reason: str = ""


@dataclass(frozen=True)
class CheckResult:
    id: str
    kind: str  # evidence | finding
    status: str
    best_score: float | None
    reason: str
    url: str | None = None
    matched_ids: tuple[str, ...] = ()


@dataclass
class FactCheckReport:
    checked_at: str
    evidence: list[CheckResult] = field(default_factory=list)
    findings: list[CheckResult] = field(default_factory=list)
    stripped_citations: list[str] = field(default_factory=list)

    @staticmethod
    def _counts(results: Iterable[CheckResult]) -> dict[str, int]:
        counts = {"verified": 0, "partial": 0, "unsupported": 0}
        for result in results:
            counts[result.status] += 1
        return counts

    @property
    def could_not_verify(self) -> list[CheckResult]:
        return [r for r in (*self.findings, *self.evidence) if r.status == "unsupported"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "checked_at": self.checked_at,
            "thresholds": {"verified": VERIFIED_RATIO, "partial": PARTIAL_RATIO},
            "counts": {
                "evidence": self._counts(self.evidence),
                "findings": self._counts(self.findings),
            },
            "evidence": [asdict(r) for r in self.evidence],
            "findings": [asdict(r) for r in self.findings],
            "could_not_verify": [asdict(r) for r in self.could_not_verify],
            "stripped_citations": self.stripped_citations,
        }


def datasvc_snapshot_reader(data_dir: Path | None = None) -> SnapshotReader:
    """Re-read a dataset entry's value from the local ``DataService`` snapshot store.

    ``entry["query"]`` may hold ``area_code`` / ``metric_code`` filters and a ``field``.
    """

    def read(entry: dict[str, Any]) -> Any:
        from app.application.service import DataService

        query = entry.get("query") if isinstance(entry.get("query"), dict) else {}
        service = DataService(data_dir=data_dir)
        records: list[dict[str, Any]] = []
        cursor = None
        while True:
            page = service.records(
                entry["dataset_id"],
                entry["snapshot_id"],
                area_code=query.get("area_code"),
                metric_code=query.get("metric_code"),
                limit=1000,
                cursor=cursor,
            )
            records.extend(page["records"])
            cursor = page["next_cursor"]
            if not cursor:
                break
        if "field" in query:
            values = [r.get(query["field"]) for r in records]
            return values[0] if len(values) == 1 else values
        return records

    return read


class FactChecker:
    def __init__(
        self,
        *,
        research: ResearchConnector | None = None,
        http: httpx.AsyncClient | None = None,
        snapshot_reader: SnapshotReader | None = None,
        requery_hosts: Iterable[str] = DEFAULT_REQUERY_HOSTS,
        resolver: Any = None,
        offline: bool = False,
    ):
        self.research = research
        self.http = http
        self._owns_http = http is None
        self.snapshot_reader = snapshot_reader
        self.requery_hosts = {host.lower() for host in requery_hosts}
        self.resolver = resolver
        self.offline = offline
        self._cache: dict[str, Fetched] = {}

    async def aclose(self) -> None:
        if self._owns_http and self.http is not None:
            await self.http.aclose()
            self.http = None

    async def __aenter__(self) -> FactChecker:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def _client(self) -> httpx.AsyncClient:
        if self.http is None:
            self.http = httpx.AsyncClient(
                timeout=FETCH_TIMEOUT_S, follow_redirects=False, trust_env=False
            )
        return self.http

    async def _pin(self, url: str) -> tuple[str, dict[str, str], dict[str, Any]]:
        """Resolve once, validate that answer and connect to it (no DNS rebinding window)."""
        parts = urlsplit(url)
        host = (parts.hostname or "").rstrip(".").lower()
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            await validate_public_url(url)
            return url, {}, {}
        # Screen scheme, port, userinfo and hostname before any DNS query.
        await validate_public_url(url, lambda *_: ["93.184.216.34"])
        port = 443 if parts.scheme.lower() == "https" else 80
        try:
            resolved = (self.resolver or _default_resolver)(host, port)
            if isawaitable(resolved):
                resolved = await resolved
            resolved = list(resolved)
        except Exception as exc:
            raise UnsafeURLError("dns_resolution_failed") from exc
        await validate_public_url(url, lambda *_: resolved)
        address = _address(resolved[0])
        netloc = f"[{address}]" if address.version == 6 else str(address)
        extensions = {"sni_hostname": host} if parts.scheme.lower() == "https" else {}
        return parts._replace(netloc=netloc).geturl(), {"Host": parts.netloc}, extensions

    async def get_public(self, url: str) -> httpx.Response:
        """GET pinned to a validated public address; every redirect hop is re-validated."""
        client = await self._client()
        for _ in range(MAX_REDIRECTS + 1):
            target, headers, extensions = await self._pin(url)
            request = client.build_request("GET", target, headers=headers, extensions=extensions)
            response = await client.send(request, follow_redirects=False)
            if not response.is_redirect:
                return response
            url = str(httpx.URL(url).join(response.headers["location"]))
        raise httpx.TooManyRedirects("too many redirects", request=response.request)

    # -- fetching ------------------------------------------------------------------------
    @staticmethod
    def _refetch_failure_reason(reason: str) -> str:
        message = str(reason).strip()
        if message.startswith("re-fetch failed:"):
            return message
        status = re.search(
            r"\b(?:HTTP[_\s-]*|status(?:[_\s-]*code)?[:=\s]+)(\d{3})\b",
            message,
            re.IGNORECASE,
        )
        if status:
            message = f"HTTP {status.group(1)}"
        elif "timeout" in message.lower():
            message = "TimeoutError"
        return f"re-fetch failed: {message or 'no content returned'}"

    async def prefetch(
        self, entries: Iterable[dict[str, Any]], *, only_ids: set[str] | None = None
    ) -> None:
        """Batch Tavily extract for web entries not fetched yet in this run."""
        if self.offline or self.research is None:
            return
        selected = [
            entry for entry in entries if only_ids is None or entry.get("evidence_id") in only_ids
        ]
        urls = tuple(
            dict.fromkeys(
                entry_url(e)
                for e in selected
                if e.get("method") in WEB_METHODS and entry_url(e) not in self._cache
            )
        )
        if not urls:
            return
        for start in range(0, len(urls), 20):
            batch = urls[start : start + 20]
            try:
                async with asyncio.timeout(PREFETCH_BATCH_TIMEOUT_S):
                    page = await self.research.extract(ResearchExtract(urls=batch))
            except Exception as exc:  # provider down, budget exhausted, ...
                reason = self._refetch_failure_reason(type(exc).__name__)
                for url in batch:
                    self._cache[url] = Fetched(None, "tavily-extract", reason)
                continue
            failures = {
                f.get("url"): self._refetch_failure_reason(f.get("reason", "failed"))
                for f in page.failures
            }
            for evidence in page.evidence:
                if evidence.extraction_status in {ExtractionStatus.OK, ExtractionStatus.PARTIAL}:
                    text = evidence.passage or evidence.snippet or ""
                    for url in (evidence.original_url, evidence.canonical_url):
                        self._cache.setdefault(url, Fetched(text, "tavily-extract"))
            for url in batch:
                self._cache.setdefault(
                    url,
                    Fetched(
                        None,
                        "tavily-extract",
                        failures.get(url, self._refetch_failure_reason("no content returned")),
                    ),
                )

    async def fetch(self, entry: dict[str, Any]) -> Fetched:
        url = entry_url(entry)
        method = entry.get("method")
        if self.offline:
            text = entry_text(entry)
            return Fetched(text or None, "stored", "" if text else "no stored text (offline)")
        if url in self._cache:
            return self._cache[url]
        if method in WEB_METHODS:
            if self.research is not None:
                await self.prefetch([entry])
                return self._cache[url]
            fetched = await self._http_text(url, "http-get", html=True)
        elif method in REQUERY_METHODS:
            host = (urlsplit(url).hostname or "").lower()
            if host not in self.requery_hosts:
                fetched = Fetched(None, "requery", f"host {host!r} not allow-listed")
            else:
                fetched = await self._http_text(url, "requery", html=False)
        else:
            fetched = Fetched(None, "none", f"no re-fetch method for {method!r}")
        self._cache[url] = fetched
        return fetched

    async def _http_text(self, url: str, how: str, *, html: bool) -> Fetched:
        try:
            async with asyncio.timeout(FETCH_TIMEOUT_S):
                response = await self.get_public(url)
        except Exception as exc:
            return Fetched(None, how, self._refetch_failure_reason(type(exc).__name__))
        if response.status_code != 200:
            reason = self._refetch_failure_reason(f"HTTP {response.status_code}")
            return Fetched(None, how, reason)
        return Fetched(html_to_text(response.text) if html else response.text, how)

    # -- checks --------------------------------------------------------------------------
    def _dataset(self, entry: dict[str, Any]) -> CheckResult:
        eid = entry["evidence_id"]
        if self.offline:
            return CheckResult(eid, "evidence", "partial", None, "dataset not re-read (offline)")
        if not entry.get("value_hash") or self.snapshot_reader is None:
            return CheckResult(eid, "evidence", "unsupported", None, "no snapshot provenance")
        try:
            current = self.snapshot_reader(entry)
        except Exception as exc:
            reason = f"snapshot re-read failed: {type(exc).__name__}"
            return CheckResult(eid, "evidence", "unsupported", None, reason)
        if value_hash(current) != entry["value_hash"]:
            reason = "snapshot value differs from the cited value"
            return CheckResult(eid, "evidence", "unsupported", 0.0, reason)
        return CheckResult(eid, "evidence", "verified", 1.0, "recomputed from snapshot")

    @staticmethod
    def _judge(quote: str, fetched: Fetched) -> tuple[str, float | None, str]:
        if fetched.text is None:
            return "unsupported", None, fetched.reason or "no content"
        ratio = round(quote_ratio(quote, fetched.text), 3)
        status = classify_ratio(ratio)
        reason = {
            "verified": f"quote found ({fetched.how}, ratio {ratio:.2f} ≥ {VERIFIED_RATIO})",
            "partial": f"quote partly found ({fetched.how}, ratio {ratio:.2f})",
            "unsupported": f"quote not found on re-fetch ({fetched.how}, best ratio "
            f"{ratio:.2f} < {PARTIAL_RATIO})",
        }[status]
        return status, ratio, reason

    def _judge_entry(
        self, entry: dict[str, Any], quote: str, fetched: Fetched
    ) -> tuple[str, float | None, str]:
        if fetched.text is None and fetched.reason.startswith("re-fetch failed:"):
            stored = entry_text(entry) or entry.get("raw_content") or entry.get("raw_text") or ""
            if not stored:
                return "unsupported", None, fetched.reason
            if not isinstance(stored, str):
                stored = str(stored)
            ratio = round(quote_ratio(quote, stored), 3)
            if classify_ratio(ratio) != "unsupported":
                failure = fetched.reason.removeprefix("re-fetch failed:").strip()
                return (
                    "partial",
                    ratio,
                    f"re-fetch failed ({failure}); matched stored extract",
                )
            return (
                "unsupported",
                ratio,
                f"{fetched.reason}; quote not found in stored extract",
            )
        return self._judge(quote, fetched)

    async def check_entry(self, entry: dict[str, Any]) -> CheckResult:
        eid, url = entry["evidence_id"], entry_url(entry)
        if entry.get("method") == "dataset":
            return self._dataset(entry)
        quote = entry.get("quote") or entry_text(entry)[:300]
        fetched = await self.fetch(entry)
        if not quote:
            ok = fetched.text is not None
            status, reason = (
                ("verified", "source re-fetched")
                if ok
                else (
                    "unsupported",
                    fetched.reason,
                )
            )
            return CheckResult(eid, "evidence", status, None, reason, url)
        status, ratio, reason = self._judge_entry(entry, quote, fetched)
        if (
            status == "unsupported"
            and fetched.text is None
            and not fetched.reason.startswith("re-fetch failed:")
            and entry.get("method") in {"wfs-cached", "csv", "osm"}
        ):
            reason = f"{fetched.reason}; value is from a pinned download"
            return CheckResult(eid, "evidence", "partial", None, reason, url)
        return CheckResult(eid, "evidence", status, ratio, reason, url)

    async def check_finding(
        self, finding: dict[str, Any], ledger: dict[str, dict[str, Any]]
    ) -> CheckResult:
        fid = finding["finding_id"]
        ids = finding.get("cited_evidence_ids") or finding.get("evidence_ids") or []
        if not ids:
            return CheckResult(fid, "finding", "unsupported", None, "no evidence ids")
        matched: list[str] = []
        weak: list[str] = []
        best: float | None = None
        reasons: list[str] = []
        for eid in ids:
            entry = ledger.get(eid)
            if entry is None:
                reasons.append(f"{eid}: not in ledger")
                continue
            if entry_status(entry) == "unsupported":
                reasons.append(f"{eid}: ledger entry unsupported")
                continue
            if entry.get("method") == "dataset":
                result = self._dataset(entry)
                status, ratio, reason = result.status, result.best_score, result.reason
            else:
                quote = finding.get("quote", "")
                status, ratio, reason = self._judge_entry(entry, quote, await self.fetch(entry))
            if ratio is not None:
                best = ratio if best is None else max(best, ratio)
            if status == "verified":
                matched.append(eid)
            elif status == "partial":
                weak.append(eid)
            reasons.append(f"{eid}: {reason}")
        if len(matched) == len(ids):
            status = "verified"
        elif matched or weak:
            status, matched = "partial", [e for e in ids if e in {*matched, *weak}]
        else:
            status = "unsupported"
        return CheckResult(fid, "finding", status, best, "; ".join(reasons), None, tuple(matched))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _original(path: Path, entries: list[dict[str, Any]]) -> str:
    """Writer text with ``[n]`` pinned to ids, kept as ``<stem>.orig<suffix>`` for rechecks."""
    original = path.with_name(f"{path.stem}.orig{path.suffix}")
    text = path.read_text(encoding="utf-8")
    if FACT_CHECKED_MARK not in text or not original.exists():
        pinned = pin_citations(text, entries)
        original.write_text(pinned, encoding="utf-8")
        return pinned
    return original.read_text(encoding="utf-8")


def _appendix(report: FactCheckReport, findings: dict[str, dict[str, Any]]) -> str:
    lines = ["", "## Could not verify", ""]
    for result in report.could_not_verify:
        if result.kind == "finding":
            claim = findings.get(result.id, {}).get("claim", "")
            lines.append(f"- {claim} (`{result.id}`): {result.reason}")
        else:
            lines.append(f"- `{result.id}` {result.url or ''}: {result.reason}")
    return "\n".join(lines) + "\n"


def _stored_results(
    entries: list[dict[str, Any]], findings: list[dict[str, Any]], checked_at: str
) -> FactCheckReport:
    """Report built from every persisted status, so skipped items keep their results."""
    report = FactCheckReport(checked_at=checked_at)
    for entry in entries:
        status = entry_status(entry)
        if status == "unverified":
            continue
        check = entry.get("fact_check") or {}
        report.evidence.append(
            CheckResult(
                entry["evidence_id"],
                "evidence",
                status,
                check.get("ratio"),
                check.get("reason", "status from ledger"),
                entry_url(entry) or None,
            )
        )
    for finding in findings:
        if finding.get("status") == "rejected" or not finding.get("verification"):
            continue
        report.findings.append(
            CheckResult(
                finding["finding_id"],
                "finding",
                finding["verification"],
                finding.get("best_score"),
                finding.get("fact_check_reason", ""),
                matched_ids=tuple(finding.get("evidence_ids") or ()),
            )
        )
    return report


async def fact_check_run(
    run_dir: Path | str,
    checker: FactChecker,
    *,
    trace: TraceWriter | None = None,
    recheck: bool = False,
    only_ids: set[str] | None = None,
) -> FactCheckReport:
    """Verify ledger entries and findings of one run directory and write the results.

    Writes ledger revisions, ``findings.jsonl`` (``verification``, ``best_score``; partial
    findings keep only matching ids), ``factcheck.json``, ``report.md`` (markers renumbered,
    claims without surviving support dropped, "Could not verify" appendix) and
    ``fact_check`` events. Run it before the writer when possible (§5.7); an existing
    ``report.md`` is cleaned up afterwards.
    """
    run_dir = Path(run_dir)
    ledger = EvidenceLedger(run_dir)
    trace = trace or TraceWriter(run_dir)
    checked_at = utc_now()
    report_md = run_dir / "report.md"
    pinned = _original(report_md, ledger.entries()) if report_md.exists() else None
    findings_path = run_dir / FINDINGS_FILE
    findings = _read_jsonl(findings_path)
    entries = ledger.entries()
    await checker.prefetch(entries, only_ids=only_ids)

    entries_to_check = [
        entry
        for entry in entries
        if (only_ids is None or entry["evidence_id"] in only_ids)
        and (recheck or entry_status(entry) == "unverified")
    ]
    semaphore = asyncio.Semaphore(_factcheck_concurrency())

    async def check_entry(entry: dict[str, Any]) -> CheckResult:
        async with semaphore:
            return await checker.check_entry(entry)

    results = await asyncio.gather(*(check_entry(entry) for entry in entries_to_check))
    for entry, result in zip(entries_to_check, results):
        ledger.set_status(
            result.id,
            result.status,
            {"checked_at": checked_at, "ratio": result.best_score, "reason": result.reason},
        )
        trace.emit(
            "fact_check",
            AGENT,
            result.reason,
            detail={"summary": f"{result.id} {result.status}", "evidence_ids": [result.id]},
        )

    by_id = {e["evidence_id"]: e for e in ledger.entries()}
    for finding in findings:
        if finding.get("status") == "rejected":
            continue
        if finding.get("verification") and not recheck:
            continue
        cited = finding.get("cited_evidence_ids") or finding.get("evidence_ids") or []
        result = await checker.check_finding(finding, by_id)
        finding.update(
            verification=result.status,
            best_score=result.best_score,
            fact_check_reason=result.reason,
            cited_evidence_ids=list(cited),
            evidence_ids=list(result.matched_ids) if result.status != "unsupported" else [],
        )
        trace.emit(
            "fact_check",
            AGENT,
            result.reason,
            detail={
                "summary": f"{result.id} {result.status}",
                "finding_id": result.id,
                "evidence_ids": list(cited),
                "best_score": result.best_score,
            },
        )
    if findings:
        findings_path.write_text(
            "".join(json.dumps(f, ensure_ascii=False) + "\n" for f in findings), encoding="utf-8"
        )

    report = _stored_results(ledger.entries(), findings, checked_at)
    if pinned is not None:
        live = [f for f in findings if f.get("status") != "rejected"]
        supported = {
            e
            for f in live
            if f.get("verification") != "unsupported"
            for e in f.get("evidence_ids") or ()
        }
        only_unsupported = {
            e
            for f in live
            if f.get("verification") == "unsupported"
            for e in f.get("cited_evidence_ids") or ()
        } - supported
        rendered = render_citations(pinned, ledger.entries(), exclude=only_unsupported)
        report.stripped_citations = list(rendered.stripped)
        body = rendered.text.rstrip("\n") + "\n"
        named = {f["finding_id"]: f for f in findings}
        appendix = _appendix(report, named) if report.could_not_verify else ""
        report_md.write_text(body + appendix + FACT_CHECKED_MARK + "\n", encoding="utf-8")

    (run_dir / FACTCHECK_FILE).write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    trace.emit(
        "fact_check",
        AGENT,
        "Unsupported claims are excluded from the writer and listed under 'Could not verify'",
        detail={"summary": "fact-check done", "counts": report.to_dict()["counts"]},
    )
    return report
