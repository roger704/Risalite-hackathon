"""Budgeted, ledger-backed tools: web_search, extract_page, record_finding (§5.3)."""

from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from app.contracts.evidence import Finding
from app.domain.research import (
    BudgetExceededError,
    ExtractionStatus,
    ResearchConnector,
    ResearchExtract,
    ResearchSearch,
)
from app.evidence.ledger import EvidenceLedger, entry_text, entry_url
from app.evidence.matching import classify_ratio, quote_ratio
from app.research import prompts
from app.research.config import ResearchConfig
from app.research.events import Events, clean_error
from app.research.locality import mentioned

BUDGET_MESSAGE = "BUDGET_EXHAUSTED: stop searching and return your findings"
TOOL_NAMES = ("web_search", "extract_page", "record_finding")
ACCEPTED_MATCHES = ("verified", "partial")
FUTURE_UNITS = ("dwellings", "kita_places", "children_0_6")
Role = Literal["future", "t1", "risk"]

ClaimType = Literal["fact", "estimate", "opinion", "future", "risk"]


class SearchArgs(BaseModel):
    query: str = Field(description="Search query; German keywords work best for Berlin sources.")
    why: str = Field(description="One sentence: what this search should establish.")
    include_domains: list[str] | None = Field(
        default=None, description="Optional domain filter, e.g. ['berlin.de']."
    )
    topic: Literal["general", "news"] = "general"


class ExtractArgs(BaseModel):
    evidence_id: str = Field(description="An evidence_id returned by web_search.")
    why: str = Field(description="One sentence: what you expect to find on the page.")


class FindingArgs(BaseModel):
    claim: str = Field(description="One claim in English, in your own words.")
    quote: str = Field(
        description="Verbatim passage copied from the source text, in its original language."
    )
    evidence_ids: list[str] = Field(description="Ledger ids returned by web_search/extract_page.")
    claim_type: ClaimType
    localization: str | None = Field(
        default=None, description="A given cell id, 'area', or 'address' (only if stated)."
    )
    why: str = ""


class FutureFindingArgs(FindingArgs):
    kind: str | None = Field(
        default=None,
        description="Future projects only: housing, office, retail, transit, cafe_opening, "
        "kita_opening, kita_closure, school, other.",
    )
    unit: Literal["dwellings", "kita_places", "children_0_6"] | None = Field(
        default=None, description="Unit of amount, only if the quote states a number."
    )
    amount: float | None = Field(default=None, description="Number stated in the quote.")
    completion_year: int | None = Field(default=None, description="Completion year per source.")


class T1FindingArgs(FindingArgs):
    dimension: (
        Literal["cost", "space_fit", "operator_fit", "demand", "competition", "regulation"] | None
    ) = None
    stance: Literal["for", "against", "neutral"] | None = None


ARGS: dict[Role, type[FindingArgs]] = {
    "future": FutureFindingArgs,
    "t1": T1FindingArgs,
    "risk": FindingArgs,
}


@dataclass
class Budget:
    max_searches: int
    max_extracts: int
    max_tool_errors: int
    grace_model_calls: int
    searches: int = 0
    extracts: int = 0
    consecutive_errors: int = 0
    stop_reason: str | None = None
    exhausted: set[str] = field(default_factory=set)

    def stop(self, reason: str) -> None:
        if self.stop_reason is None:
            self.stop_reason = reason


@dataclass(frozen=True)
class QuoteMatch:
    matched: tuple[str, ...]
    unknown: tuple[str, ...]
    best: str
    ratios: dict[str, float]


def match_quote(ledger: EvidenceLedger, quote: str, evidence_ids: Iterable[str]) -> QuoteMatch:
    """Ratio of ``quote`` against each cited entry's stored text (``app.evidence.matching``)."""
    entries = {e["evidence_id"]: e for e in ledger.entries()}
    ratios: dict[str, float] = {}
    unknown: list[str] = []
    for evidence_id in dict.fromkeys(evidence_ids):
        entry = entries.get(evidence_id)
        if entry is None:
            unknown.append(evidence_id)
            continue
        text = entry_text(entry) or entry.get("quote") or ""
        ratios[evidence_id] = round(quote_ratio(quote, text), 3) if text and quote else 0.0
    matched = tuple(e for e, r in ratios.items() if classify_ratio(r) in ACCEPTED_MATCHES)
    best = classify_ratio(max(ratios.values(), default=0.0))
    return QuoteMatch(matched, tuple(unknown), best, ratios)


_NUMBER = re.compile(r"\d{1,3}(?:[.\u202f\u00a0 ]\d{3})+(?:,\d+)?|\d+(?:[.,]\d+)?")


def quote_numbers(quote: str) -> set[float]:
    numbers: set[float] = set()
    for raw in _NUMBER.findall(quote):
        grouped = re.sub(r"[.\u202f\u00a0 ](?=\d{3}\b)", "", raw)
        for candidate in {grouped.replace(",", "."), raw.replace(",", "")}:
            try:
                numbers.add(float(candidate))
            except ValueError:
                continue
    return numbers


def untrusted(evidence_id: str, text: str) -> str:
    safe = text.replace("<", "&lt;").replace(">", "&gt;")
    return f'<untrusted_source evidence_id="{evidence_id}">\n{safe}\n</untrusted_source>'


def new_finding_id() -> str:
    return f"f_{uuid.uuid4().hex[:20]}"


class Toolkit:
    def __init__(
        self,
        *,
        ledger: EvidenceLedger,
        events: Events,
        cfg: ResearchConfig,
        connector: ResearchConnector | None,
        role: Role,
        localizations: Iterable[str],
        default_localization: str,
        place_terms: Mapping[str, Sequence[str]] | None = None,
        claim_types: Iterable[str] | None = None,
        finding_extra: dict[str, Any] | None = None,
    ):
        self.ledger = ledger
        self.events = events
        self.cfg = cfg
        self.connector = connector
        self.role = role
        self.localizations = set(localizations)
        self.default_localization = default_localization
        self.place_terms = {key: tuple(terms) for key, terms in (place_terms or {}).items()}
        self.claim_types = set(claim_types or ClaimType.__args__)  # type: ignore[attr-defined]
        self.finding_extra = dict(finding_extra or {})
        self.findings: list[Finding] = []
        self.budget = Budget(
            max_searches=cfg.max_searches,
            max_extracts=cfg.max_extracts,
            max_tool_errors=cfg.max_tool_errors,
            grace_model_calls=cfg.grace_model_calls,
        )

    @property
    def accepted(self) -> list[Finding]:
        return [f for f in self.findings if f.status == "accepted"]

    # -- budget -------------------------------------------------------------------------
    async def _exhausted(self, resource: str, why: str, limit: int) -> str:
        if resource not in self.budget.exhausted:
            self.budget.exhausted.add(resource)
            await self.events(
                "budget_exhausted",
                why=why,
                detail={"resource": resource, "limit": limit},
            )
        self.budget.stop(f"{resource} budget exhausted")
        return await self._error(BUDGET_MESSAGE)

    async def _error(self, message: str) -> str:
        self.budget.consecutive_errors += 1
        if self.budget.consecutive_errors >= self.budget.max_tool_errors:
            self.budget.stop(f"{self.budget.consecutive_errors} consecutive tool errors")
            if "tool_errors" not in self.budget.exhausted:
                self.budget.exhausted.add("tool_errors")
                await self.events(
                    "budget_exhausted",
                    why="too many consecutive tool errors",
                    detail={"resource": "tool_errors", "limit": self.budget.max_tool_errors},
                )
        return message

    async def _tool_error(self, tool: str, why: str, error: BaseException, **detail: Any) -> str:
        message = clean_error(error)
        await self.events("tool_error", why=why, detail={"tool": tool, "error": message, **detail})
        return await self._error(f"{tool} failed: {message}")

    def _ok(self) -> None:
        self.budget.consecutive_errors = 0

    # -- tools --------------------------------------------------------------------------
    async def web_search(
        self,
        query: str,
        why: str,
        include_domains: list[str] | None = None,
        topic: str = "general",
    ) -> str:
        if self.budget.searches >= self.budget.max_searches:
            return await self._exhausted("searches", why, self.budget.max_searches)
        if self.budget.stop_reason:
            return await self._error(BUDGET_MESSAGE)
        if self.connector is None:
            return await self._error("web_search is not available in this run")
        self.budget.searches += 1
        started = time.perf_counter()
        try:
            page = await self.connector.search(
                ResearchSearch(
                    query=query,
                    search_depth="advanced",
                    max_results=self.cfg.max_results,
                    include_domains=tuple(include_domains or ()),
                    topic=topic,
                )
            )
        except BudgetExceededError:
            return await self._exhausted("searches", why, self.budget.max_searches)
        except Exception as error:
            return await self._tool_error("web_search", why, error, query=query)
        results = []
        for evidence in page.evidence:
            if evidence.extraction_status is ExtractionStatus.FAILED:
                continue
            entry = self.ledger.add_web_evidence(evidence)
            results.append(
                {
                    "evidence_id": entry["evidence_id"],
                    "title": entry.get("title"),
                    "url": entry_url(entry),
                    "published_date": entry.get("published_at_raw") or entry.get("published_at"),
                    "snippet": untrusted(entry["evidence_id"], (entry.get("snippet") or "")[:600]),
                }
            )
        await self.events(
            "search",
            why=why,
            detail={
                "query": query,
                "results": len(results),
                "evidence_ids": [r["evidence_id"] for r in results],
                "searches_used": self.budget.searches,
                "searches_max": self.budget.max_searches,
            },
            seconds=time.perf_counter() - started,
        )
        self._ok()
        if not results:
            return "No results. Try different German keywords."
        return json.dumps(results, ensure_ascii=False)

    def _extracted(self, url: str) -> dict[str, Any] | None:
        return next(
            (
                e
                for e in self.ledger.entries()
                if e.get("method") == "tavily-extract"
                and url in {entry_url(e), e.get("canonical_url")}
                and entry_text(e)
            ),
            None,
        )

    def _page(self, entry: dict[str, Any], source_id: str) -> str:
        text = entry_text(entry)[: self.cfg.extract_chars]
        return json.dumps(
            {
                "evidence_id": entry["evidence_id"],
                "source_evidence_id": source_id,
                "title": entry.get("title"),
                "url": entry_url(entry),
                "published_date": entry.get("published_at_raw") or entry.get("published_at"),
                "content": untrusted(entry["evidence_id"], text),
            },
            ensure_ascii=False,
        )

    async def extract_page(self, evidence_id: str, why: str) -> str:
        source = self.ledger.get(evidence_id)
        if source is None:
            return await self._error("Unknown evidence_id. Use an id returned by web_search.")
        url = entry_url(source)
        cached = self._extracted(url) or self._extracted(source.get("canonical_url") or url)
        if cached is not None:
            self._ok()
            return self._page(cached, evidence_id)
        if self.budget.extracts >= self.budget.max_extracts:
            return await self._exhausted("extracts", why, self.budget.max_extracts)
        if self.budget.stop_reason:
            return await self._error(BUDGET_MESSAGE)
        if self.connector is None or not url:
            return await self._error("extract_page is not available for this evidence id")
        self.budget.extracts += 1
        started = time.perf_counter()
        try:
            page = await self.connector.extract(
                ResearchExtract(
                    urls=(url,),
                    query=source.get("query"),
                    candidate_id=source.get("candidate_id"),
                    analysis_id=source.get("analysis_id"),
                )
            )
        except BudgetExceededError:
            return await self._exhausted("extracts", why, self.budget.max_extracts)
        except Exception as error:
            return await self._tool_error("extract_page", why, error, evidence_id=evidence_id)
        ok = [e for e in page.evidence if e.extraction_status is not ExtractionStatus.FAILED]
        entries = [self.ledger.add_web_evidence(e) for e in ok if e.passage or e.snippet]
        target = entries[0] if entries else None
        await self.events(
            "extract",
            why=why,
            detail={
                "evidence_id": target["evidence_id"] if target else None,
                "source_evidence_id": evidence_id,
                "url": url,
                "ok": target is not None,
                "failures": [f.get("reason") for f in page.failures][:3],
                "extracts_used": self.budget.extracts,
                "extracts_max": self.budget.max_extracts,
            },
            seconds=time.perf_counter() - started,
        )
        if target is None:
            return await self._error(
                "Extraction returned no content; quote the search snippet or try another source."
            )
        self._ok()
        return self._page(target, evidence_id)

    def _localize(self, localization: str | None) -> str:
        if not localization:
            return self.default_localization
        value = localization.strip()
        if value in self.localizations:
            return value
        for prefix in ("plr:", "cell:"):
            if value.lower().startswith(prefix) and value[len(prefix) :] in self.localizations:
                return value[len(prefix) :]
        if value.lower().startswith("address") and "address" in self.localizations:
            return "address"
        if (
            value.lower() in {"district", "bezirk", "ortsteil", "area"}
            and "area" in self.localizations
        ):
            return "area"
        return self.default_localization

    def _source_text(self, evidence_ids: Iterable[str]) -> str:
        parts = []
        for evidence_id in evidence_ids:
            entry = self.ledger.get(evidence_id)
            if entry is None:
                continue
            parts.extend(
                value
                for value in (
                    entry_text(entry),
                    entry.get("title"),
                    entry_url(entry),
                )
                if isinstance(value, str) and value
            )
        return "\n".join(parts)

    async def _reject(self, payload: dict[str, Any], reason: str, why: str) -> str:
        finding = Finding(
            finding_id=new_finding_id(),
            claim=payload["claim"],
            quote=payload["quote"],
            evidence_ids=list(payload["evidence_ids"]),
            claim_type=payload["claim_type"],
            localization=payload.get("localization"),
            agent=self.events.agent,
            status="rejected",
            reject_reason=reason,
            **self.finding_extra,
        )
        self.findings.append(finding)
        await self.events(
            "finding_rejected",
            why=why or reason,
            detail={
                "finding_id": finding.finding_id,
                "reason": reason,
                "claim": finding.claim,
                "evidence_ids": finding.evidence_ids,
            },
        )
        return await self._error(f"Finding rejected: {reason}")

    async def record_finding(
        self,
        claim: str,
        quote: str,
        evidence_ids: list[str],
        claim_type: str,
        localization: str | None = None,
        why: str = "",
        **extra: Any,
    ) -> str:
        payload = {
            "claim": claim,
            "quote": quote,
            "evidence_ids": evidence_ids,
            "claim_type": claim_type,
            "localization": localization,
        }
        if claim_type not in self.claim_types:
            allowed = ", ".join(sorted(self.claim_types))
            return await self._reject(payload, f"claim_type must be one of: {allowed}", why)
        if not evidence_ids:
            return await self._reject(payload, "At least one evidence id is required.", why)
        if not quote.strip():
            return await self._reject(payload, "A verbatim quote is required.", why)
        match = match_quote(self.ledger, quote, evidence_ids)
        if match.unknown:
            return await self._reject(
                payload, f"Unknown evidence ids: {', '.join(match.unknown)}", why
            )
        if not match.matched:
            best = max(match.ratios.values(), default=0.0)
            reason = (
                f"Quote not found in the stored text of the cited evidence (best ratio {best:.2f}). "
                "Copy the passage verbatim from the tool output."
            )
            return await self._reject(payload, reason, why)
        notes: list[str] = []
        fields = {k: v for k, v in extra.items() if v is not None}
        if fields.get("amount") is not None:
            if fields["amount"] not in quote_numbers(quote):
                notes.append("amount dropped: the number is not stated in the quote")
                fields.pop("amount")
            elif fields.get("unit") not in FUTURE_UNITS:
                notes.append("amount kept without unit")
        if claim_type == "future" and type(fields.get("completion_year")) is int:
            completion_year = fields["completion_year"]
            if completion_year < date.fromisoformat(prompts.today()).year:
                claim_type = "fact"
                notes.append(f"completion year {completion_year} is before today; recorded as fact")

        loc = self._localize(localization)
        localization_note = ""
        downgrade: tuple[str, str, str] | None = None
        source_text = self._source_text(match.matched)
        terms = self.place_terms.get(loc, ())
        if loc != "area" and terms:
            term = mentioned(source_text, terms)
            if term is not None:
                fields["localization_check"] = f"mentioned:{term}"
            else:
                requested = loc
                label = terms[0]
                new_loc = "area"
                if loc == "address":
                    cell_id = next(
                        (
                            key
                            for key in self.place_terms
                            if key not in {"address", "area"}
                            and key in self.place_terms
                            and key in self.localizations
                            and mentioned(source_text, self.place_terms[key]) is not None
                        ),
                        None,
                    )
                    if cell_id is not None:
                        new_loc = cell_id
                loc = new_loc if new_loc in self.localizations else self.default_localization
                fields["localization_requested"] = requested
                fields["localization_check"] = "downgraded"
                downgrade = (requested, loc, label)
                localization_note = (
                    f" Note: recorded at {loc} level because the cited source does not name "
                    f"{label} or one of its streets. Cite a source that names the place to "
                    "localize it."
                )

        finding = Finding(
            finding_id=new_finding_id(),
            claim=claim,
            quote=quote,
            evidence_ids=list(match.matched),
            claim_type=claim_type,
            localization=loc,
            agent=self.events.agent,
            status="accepted",
            match=match.best,
            match_ratios={e: match.ratios[e] for e in match.matched},
            why=why or None,
            **self.finding_extra,
            **fields,
        )
        self.findings.append(finding)
        await self.events(
            "finding_accepted",
            why=why or claim,
            detail={
                "finding_id": finding.finding_id,
                "claim": claim,
                "claim_type": claim_type,
                "localization": finding.localization,
                "evidence_ids": finding.evidence_ids,
                "match": match.best,
            },
        )
        if downgrade is not None:
            requested, new_loc, label = downgrade
            await self.events(
                "localization_downgraded",
                why=f"Source does not name {label}; recorded as {new_loc}",
                detail={
                    "finding_id": finding.finding_id,
                    "requested": requested,
                    "localization": new_loc,
                    "evidence_ids": finding.evidence_ids,
                },
            )
        self._ok()
        suffix = f" ({'; '.join(notes)})" if notes else ""
        return json.dumps({"finding_id": finding.finding_id}) + suffix + localization_note

    def tools(self) -> list[BaseTool]:
        args = ARGS[self.role]
        return [
            StructuredTool.from_function(
                coroutine=self.web_search,
                name="web_search",
                description="Search the web (Tavily). Every result is stored in the evidence "
                "ledger; returns [{evidence_id, title, url, published_date, snippet}].",
                args_schema=SearchArgs,
            ),
            StructuredTool.from_function(
                coroutine=self.extract_page,
                name="extract_page",
                description="Fetch the full text of a page by an evidence_id from web_search. "
                "Returns a new evidence_id for the extracted page and its text.",
                args_schema=ExtractArgs,
            ),
            StructuredTool.from_function(
                coroutine=self.record_finding,
                name="record_finding",
                description="Record one claim backed by a verbatim quote from the stored text "
                "of the cited evidence ids. Returns {finding_id} or the reason for rejection.",
                args_schema=args,
            ),
        ]
