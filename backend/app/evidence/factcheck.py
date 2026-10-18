"""Fact-checker: re-fetch every ledger source and verify its quote.

* ``tavily-extract`` / ``tavily-search`` -> Tavily ``extract`` (plain HTTP GET if no connector)
* ``wfs`` / ``wfs-cached`` / ``overpass`` -> re-run the exact GET request (allow-listed hosts)
* ``local-snapshot`` -> re-read the snapshot and compare the SHA-256 of the cited value

Quote match ratio (see :mod:`app.evidence.matching`): ``>= 0.85`` verified, ``>= 0.60``
partial, otherwise unsupported. Unsupported entries are dropped from ``cells.json`` /
``candidates.json`` and listed under ``could_not_verify``.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import httpx

from app.connectors.tavily import validate_public_url
from app.contracts.evidence import FactCheck, LedgerEntry, SourceRef
from app.domain.research import (
    ExtractionStatus,
    ResearchConnector,
    ResearchExtract,
    canonicalize_url,
)
from app.evidence.citations import strip_unknown_citations
from app.evidence.ledger import EvidenceLedger, value_hash
from app.evidence.matching import (
    PARTIAL_RATIO,
    VERIFIED_RATIO,
    classify_ratio,
    html_to_text,
    normalize,
)
from app.evidence.matching import quote_ratio as _quote_ratio
from app.evidence.trace import TraceWriter

WEB_METHODS = {"tavily-extract", "tavily-search"}
REQUERY_METHODS = {"wfs", "wfs-cached", "overpass"}
MAX_REDIRECTS = 5
FACT_CHECKED_MARK = "<!-- fact-checked: see factcheck.json -->"
DEFAULT_REQUERY_HOSTS = ("gdi.berlin.de", "fbinter.stadt-berlin.de", "overpass-api.de")
RESULT_FILES = ("cells.json", "candidates.json")
# Lists whose items are structural (dropping them would break the result shape); unsupported
# items there are flagged with ``"fact_check": "unsupported"`` instead of being removed.
STRUCTURAL_LISTS = {"layers", "cells", "hypotheses", "candidates", "dimensions"}
ROLE = "Fact-checker"

SnapshotReader = Callable[[SourceRef], Any]


@dataclass(frozen=True)
class CheckResult:
    evidence_id: str
    url: str
    method: str
    status: str
    ratio: float | None
    reason: str
    fetch_method: str


@dataclass
class FactCheckReport:
    checked_at: datetime
    results: list[CheckResult]
    could_not_verify: list[dict[str, Any]] = field(default_factory=list)
    stripped_citations: list[int] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        counts = {"verified": 0, "partial": 0, "unsupported": 0}
        for result in self.results:
            counts[result.status] += 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "checked_at": self.checked_at.isoformat().replace("+00:00", "Z"),
            "thresholds": {"verified": VERIFIED_RATIO, "partial": PARTIAL_RATIO},
            "counts": self.counts(),
            "results": [asdict(result) for result in self.results],
            "could_not_verify": self.could_not_verify,
            "stripped_citations": self.stripped_citations,
        }


def datasvc_snapshot_reader(data_dir: Path | None = None) -> SnapshotReader:
    """Re-read a value from a local ``DataService`` snapshot.

    ``source_ref.query`` may hold ``area_code`` / ``metric_code`` filters, a ``record_id`` and a
    ``field``. The value is the matching record (or its ``field``), or the list of records.
    """

    def read(ref: SourceRef) -> Any:
        from app.application.service import DataService

        if not ref.dataset_id or not ref.snapshot_id:
            raise ValueError("source_ref needs dataset_id and snapshot_id")
        query = ref.query if isinstance(ref.query, dict) else {}
        service = DataService(data_dir=data_dir)
        records: list[dict[str, Any]] = []
        cursor = None
        while True:
            page = service.records(
                ref.dataset_id,
                ref.snapshot_id,
                area_code=query.get("area_code"),
                metric_code=query.get("metric_code"),
                limit=1000,
                cursor=cursor,
            )
            records.extend(page["records"])
            cursor = page["next_cursor"]
            if not cursor:
                break
        if "record_id" in query:
            records = [r for r in records if r.get("record_id") == query["record_id"]]
        if "field" in query:
            values = [r.get(query["field"]) for r in records]
            return values[0] if len(values) == 1 else values
        return records[0] if len(records) == 1 and "record_id" in query else records

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
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.research = research
        self.http = http
        self._owns_http = http is None
        self.snapshot_reader = snapshot_reader
        self.requery_hosts = {host.lower() for host in requery_hosts}
        self.resolver = resolver
        self.clock = clock

    async def _client(self) -> httpx.AsyncClient:
        if self.http is None:
            self.http = httpx.AsyncClient(timeout=30.0, follow_redirects=False)
        return self.http

    async def aclose(self) -> None:
        """Close the HTTP client this checker created; injected clients stay open."""
        if self._owns_http and self.http is not None:
            await self.http.aclose()
            self.http = None

    async def __aenter__(self) -> FactChecker:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    def _result(self, entry: LedgerEntry, status: str, ratio, reason: str, how: str):
        return CheckResult(entry.evidence_id, entry.url, entry.method, status, ratio, reason, how)

    def _judge(self, entry: LedgerEntry, text: str, how: str) -> CheckResult:
        ratio = round(_quote_ratio(entry.quote, text), 3)
        status = classify_ratio(ratio)
        reason = {
            "verified": f"quote found (ratio {ratio:.2f} >= {VERIFIED_RATIO})",
            "partial": f"quote partly found (ratio {ratio:.2f}, {PARTIAL_RATIO}–{VERIFIED_RATIO})",
            "unsupported": f"quote not found on re-fetch (best ratio {ratio:.2f} < "
            f"{PARTIAL_RATIO})",
        }[status]
        return self._result(entry, status, ratio, reason, how)

    # -- web ---------------------------------------------------------------------------
    async def _check_web(self, entries: list[LedgerEntry]) -> list[CheckResult]:
        if not entries:
            return []
        if self.research is None:
            return [await self._check_web_http(entry) for entry in entries]
        urls = tuple(dict.fromkeys(entry.url for entry in entries))
        try:
            page = await self.research.extract(ResearchExtract(urls=urls))
        except Exception as exc:  # provider down, budget exhausted, ...
            reason = f"tavily extract failed: {type(exc).__name__}"
            return [self._result(e, "unsupported", None, reason, "tavily-extract") for e in entries]
        texts: dict[str, str] = {}
        failures: dict[str, str] = {f["url"]: f["reason"] for f in page.failures}
        for evidence in page.evidence:
            if evidence.extraction_status in {ExtractionStatus.OK, ExtractionStatus.PARTIAL}:
                text = evidence.passage or evidence.snippet or ""
                texts[evidence.original_url] = text
                texts.setdefault(evidence.canonical_url, text)
        results = []
        for entry in entries:
            text = texts.get(entry.url) or texts.get(canonicalize_url(entry.url))
            if text is None:
                reason = f"re-fetch failed: {failures.get(entry.url, 'no content returned')}"
                results.append(self._result(entry, "unsupported", None, reason, "tavily-extract"))
            else:
                results.append(self._judge(entry, text, "tavily-extract"))
        return results

    async def _check_web_http(self, entry: LedgerEntry) -> CheckResult:
        try:
            response = await self._get_public(entry.url)
        except Exception as exc:
            reason = f"re-fetch failed: {type(exc).__name__}"
            return self._result(entry, "unsupported", None, reason, "http-get")
        if response.status_code != 200:
            reason = f"re-fetch failed: HTTP {response.status_code}"
            return self._result(entry, "unsupported", None, reason, "http-get")
        return self._judge(entry, html_to_text(response.text), "http-get")

    async def _get_public(self, url: str) -> httpx.Response:
        """GET that re-validates every redirect hop so a redirect cannot reach a private host."""
        client = await self._client()
        for _ in range(MAX_REDIRECTS + 1):
            await validate_public_url(url, self.resolver)
            response = await client.get(url, follow_redirects=False)
            if not response.is_redirect:
                return response
            url = str(response.url.join(response.headers["location"]))
        raise httpx.TooManyRedirects("too many redirects", request=response.request)

    # -- WFS / Overpass ------------------------------------------------------------------
    async def _check_requery(self, entry: LedgerEntry) -> CheckResult:
        how = f"{entry.method}-requery"
        target = httpx.URL(entry.url)
        host = (target.host or "").lower()
        if host not in self.requery_hosts or target.port is not None:
            reason = f"host {target.netloc.decode()} is not allow-listed for re-query"
            return self._result(entry, "unsupported", None, reason, how)
        try:
            response = await (await self._client()).get(entry.url, follow_redirects=False)
            ok = response.status_code == 200
            failure = f"HTTP {response.status_code}"
        except httpx.HTTPError as exc:
            ok, failure, response = False, type(exc).__name__, None
        if not ok:
            if entry.method == "wfs-cached":
                reason = f"live re-query failed ({failure}); value is from a cached fixture"
                return self._result(entry, "partial", None, reason, how)
            return self._result(entry, "unsupported", None, f"re-query failed: {failure}", how)
        result = self._judge(entry, response.text, how)
        if result.status == "verified":
            return result
        features = _feature_count(response)
        expected = (
            (entry.source_ref.model_extra or {}).get("feature_count") if entry.source_ref else None
        )
        if features is not None and expected is not None and features == expected:
            reason = (
                f"re-query returned the cited {features} features but quote not verbatim "
                f"(ratio {result.ratio:.2f})"
            )
            return self._result(entry, "partial", result.ratio, reason, how)
        if features:
            reason = (
                f"re-query OK ({features} features) but quote not verbatim "
                f"(ratio {result.ratio:.2f})"
            )
            return self._result(entry, "partial", result.ratio, reason, how)
        return result

    # -- local snapshots -----------------------------------------------------------------
    def _check_snapshot(self, entry: LedgerEntry) -> CheckResult:
        ref = entry.source_ref
        if ref is None or not ref.content_hash:
            return self._result(entry, "unsupported", None, "no snapshot provenance", "snapshot")
        if self.snapshot_reader is None:
            return self._result(entry, "unsupported", None, "no snapshot reader", "snapshot")
        try:
            current = self.snapshot_reader(ref)
        except Exception as exc:
            reason = f"snapshot re-read failed: {type(exc).__name__}"
            return self._result(entry, "unsupported", None, reason, "snapshot")
        if value_hash(current) != ref.content_hash:
            reason = "snapshot value hash differs from the cited value"
            return self._result(entry, "unsupported", 0.0, reason, "snapshot")
        if not _quote_states_value(entry.quote, current):
            reason = "snapshot value unchanged, but the quote does not state it"
            return self._result(entry, "partial", None, reason, "snapshot")
        return self._result(entry, "verified", 1.0, "snapshot value hash matches", "snapshot")

    async def check(self, entries: Iterable[LedgerEntry]) -> list[CheckResult]:
        entries = list(entries)
        by_id: dict[str, CheckResult] = {}
        for result in await self._check_web([e for e in entries if e.method in WEB_METHODS]):
            by_id[result.evidence_id] = result
        for entry in entries:
            if entry.method in REQUERY_METHODS:
                by_id[entry.evidence_id] = await self._check_requery(entry)
            elif entry.method == "local-snapshot":
                by_id[entry.evidence_id] = self._check_snapshot(entry)
        return [by_id[entry.evidence_id] for entry in entries]


def _feature_count(response: httpx.Response) -> int | None:
    try:
        data = response.json()
    except ValueError:
        return None
    if isinstance(data, dict) and data.get("type") == "FeatureCollection":
        return len(data.get("features") or [])
    if isinstance(data, dict) and isinstance(data.get("elements"), list):  # Overpass
        return len(data["elements"])
    return None


def _scalars(value: Any) -> list[Any]:
    if isinstance(value, dict):
        return [s for v in value.values() for s in _scalars(v)]
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in _scalars(v)]
    return [] if value is None or isinstance(value, bool) else [value]


_NUMBER = re.compile(r"\d[\d.,\u202f\u00a0 ]*\d|\d")


def _number_readings(token: str) -> list[tuple[float, int]]:
    """(value, decimals) readings of '4.200', '4,200', '3,5', '4 200' (de and en styles)."""
    token = re.sub(r"[\u202f\u00a0 ]", "", token)
    readings = []
    for thousands, decimal in ((",", "."), (".", ",")):
        candidate = token.replace(thousands, "").replace(decimal, ".")
        try:
            number = float(candidate)
        except ValueError:
            continue
        decimals = len(candidate.split(".")[1]) if "." in candidate else 0
        readings.append((number, decimals))
    return readings


def _quote_states_value(quote: str, value: Any) -> bool:
    """True when at least one scalar of the cited value appears in the quote."""
    text = normalize(quote)
    numbers = [r for token in _NUMBER.findall(quote) for r in _number_readings(token)]
    for scalar in _scalars(value):
        if isinstance(scalar, (int, float)):
            if any(abs(n - scalar) <= 0.5 * 10**-d + 1e-9 for n, d in numbers):
                return True
        elif normalize(str(scalar)) and normalize(str(scalar)) in text:
            return True
    return False


def _item_ids(item: dict[str, Any]) -> list[str]:
    ids = [item["evidence_id"]] if isinstance(item.get("evidence_id"), str) else []
    ids += [i for i in item.get("evidence_ids") or [] if isinstance(i, str)]
    return ids


def _label(item: dict[str, Any]) -> str | None:
    for key in ("claim", "label", "text", "quote", "name", "id"):
        if isinstance(item.get(key), str):
            return item[key]
    return None


def prune_result(
    result: Any, citable: set[str], reasons: dict[str, str] | None = None
) -> tuple[Any, list[dict[str, Any]]]:
    """Drop claims whose evidence ids are all non-citable (works on cells and candidates).

    ``citable`` holds ids that exist in the ledger and are not ``unsupported``. Ids missing
    from the ledger are treated as unsupported too.
    """
    reasons = reasons or {}
    dropped: list[dict[str, Any]] = []

    def _record(item: dict[str, Any], path: str, bad: str, action: str) -> dict[str, Any]:
        return {
            "path": path,
            "evidence_id": bad,
            "claim": _label(item),
            "reason": reasons.get(bad, "evidence id not in ledger"),
            "action": action,
        }

    def walk(node: Any, path: str, key: str | None, checked: bool = False) -> Any:
        if isinstance(node, dict):
            node = dict(node)
            if not checked and node.get("fact_check") == "unsupported":
                node.pop("fact_check")
            ids = _item_ids(node)
            if ids and not checked and not any(i in citable for i in ids):
                node["fact_check"] = "unsupported"
                dropped.append(_record(node, path, ids[0], "flagged"))
            if isinstance(node.get("evidence_ids"), list):
                node["evidence_ids"] = [i for i in node["evidence_ids"] if i in citable]
            if isinstance(node.get("evidence_id"), str) and node["evidence_id"] not in citable:
                remaining = [i for i in node.get("evidence_ids") or [] if i in citable]
                if remaining:
                    node["evidence_id"] = remaining[0]
            return {k: walk(v, f"{path}.{k}" if path else k, k) for k, v in node.items()}
        if isinstance(node, list):
            kept = []
            for index, item in enumerate(node):
                item_path = f"{path}[{index}]"
                if isinstance(item, dict):
                    if item.get("fact_check") == "unsupported":
                        item = {k: v for k, v in item.items() if k != "fact_check"}
                    ids = _item_ids(item)
                    if ids and not any(i in citable for i in ids):
                        if key not in STRUCTURAL_LISTS:
                            dropped.append(_record(item, item_path, ids[0], "dropped"))
                            continue
                        item = {**item, "fact_check": "unsupported"}
                        dropped.append(_record(item, item_path, ids[0], "flagged"))
                    kept.append(walk(item, item_path, key, checked=True))
                    continue
                kept.append(walk(item, item_path, key))
            return kept
        return node

    return walk(result, "", None), dropped


def _original(path: Path, is_fresh: Callable[[str], bool]) -> str:
    """Pre-fact-check content of ``path``, kept as ``<stem>.orig<suffix>``.

    Pruning always starts from the original so a recheck can restore claims whose evidence
    has since been verified. Fresh writer output (no fact-check marker) replaces the copy.
    """
    original = path.with_name(f"{path.stem}.orig{path.suffix}")
    text = path.read_text(encoding="utf-8")
    if is_fresh(text) or not original.exists():
        original.write_text(text, encoding="utf-8")
        return text
    return original.read_text(encoding="utf-8")


def _appendix(report: FactCheckReport) -> str:
    lines = ["", "## Could not verify", ""]
    for item in report.could_not_verify:
        where = f" ({item['path']})" if item.get("path") else ""
        url = f" — {item['url']}" if item.get("url") else ""
        lines.append(f"- `{item['evidence_id']}`{where}{url}: {item['reason']}")
    return "\n".join(lines) + "\n"


async def fact_check_run(
    run_dir: Path | str,
    checker: FactChecker,
    *,
    trace: TraceWriter | None = None,
    recheck: bool = False,
) -> FactCheckReport:
    """Verify the run's ledger, prune result files, post-process ``report.md``.

    Writes ledger revisions, ``factcheck.json``, pruned ``cells.json``/``candidates.json``
    (with an added ``could_not_verify`` list), and fact_check events to ``trace.jsonl``.
    """
    run_dir = Path(run_dir)
    ledger = EvidenceLedger(run_dir)
    trace = trace or TraceWriter(run_dir)
    entries = [e for e in ledger.entries() if recheck or e.status == "unverified"]
    trace.emit(
        ROLE,
        "fact_check.start",
        f"{len(entries)} sources re-fetched",
        f"Tavily extract / WFS re-query / snapshot re-read, fuzzy >= {VERIFIED_RATIO}",
        data={"evidence_ids": [e.evidence_id for e in entries]},
    )
    started = checker.clock()
    results = await checker.check(entries)
    for entry, result in zip(entries, results):
        ledger.set_status(
            result.evidence_id,
            result.status,  # type: ignore[arg-type]
            FactCheck(
                checked_at=checker.clock(),
                ratio=result.ratio,
                reason=result.reason,
                fetch_method=result.fetch_method,
            ),
        )
        payload = {"status": result.status, "ratio": result.ratio, "method": result.method}
        trace.emit(
            ROLE,
            "fact_check.result",
            f"{result.evidence_id} {result.status}: {result.url}",
            result.reason,
            evidence_id=result.evidence_id,
            data=payload,
        )
        if result.status == "unsupported":
            trace.emit(
                ROLE,
                "fact_check.reject",
                f"“{entry.quote[:160]}”",
                f"{result.reason} → dropped",
                evidence_id=result.evidence_id,
                data=payload | {"url": result.url},
            )
    latest = ledger.entries()
    citable = {e.evidence_id for e in latest if e.status != "unsupported"}
    reasons = {e.evidence_id: e.fact_check.reason for e in latest if e.fact_check is not None}
    report = FactCheckReport(checked_at=started, results=results)
    for entry in latest:
        if entry.status == "unsupported":
            report.could_not_verify.append(
                {
                    "evidence_id": entry.evidence_id,
                    "url": entry.url,
                    "quote": entry.quote,
                    "reason": reasons.get(entry.evidence_id, "unsupported"),
                }
            )
    for name in RESULT_FILES:
        path = run_dir / name
        if not path.exists():
            continue
        data = json.loads(_original(path, is_fresh=lambda t: '"could_not_verify"' not in t))
        pruned, dropped = prune_result(data, citable, reasons)
        report.could_not_verify.extend(d | {"result": name} for d in dropped)
        pruned["could_not_verify"] = report.could_not_verify
        if name == "cells.json":
            # Scores were computed over the original inputs; S2's scorer must re-run.
            pruned["rescore_required"] = bool(dropped)
        path.write_text(json.dumps(pruned, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report_md = run_dir / "report.md"
    if report_md.exists():
        text = _original(report_md, is_fresh=lambda t: FACT_CHECKED_MARK not in t)
        stripped = strip_unknown_citations(text, latest)
        report.stripped_citations = list(stripped.stripped)
        if stripped.stripped:
            trace.emit(
                "Writer",
                "citations.stripped",
                f"stripped [n] not in ledger: {sorted(set(stripped.stripped))}",
                "Post-processor: every [n] must map to a citable ledger entry",
                data={"stripped": list(stripped.stripped)},
            )
        body = stripped.text.rstrip("\n") + "\n"
        appendix = _appendix(report) if report.could_not_verify else ""
        report_md.write_text(body + appendix + FACT_CHECKED_MARK + "\n", encoding="utf-8")
    (run_dir / "factcheck.json").write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    counts = report.counts()
    trace.emit(
        ROLE,
        "fact_check.done",
        f"{counts['verified']} verified · {counts['partial']} partial · "
        f"{counts['unsupported']} unsupported",
        "Unsupported claims dropped and listed under 'could not verify'",
        data=counts,
    )
    return report
