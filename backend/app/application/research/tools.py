"""Evidence-ledger-backed tools exposed to the research agents."""

import difflib
import json
import os
import re
from typing import Literal

from langchain_core.tools import BaseTool, tool

from app.application.research.events import EventSink
from app.application.research.ledger import EvidenceLedger, FindingStore
from app.domain.research import ResearchConnector, ResearchExtract, ResearchSearch


def _clean_error(error: Exception) -> str:
    message = str(error)[:500]
    for name in ("TAVILY_API_KEY", "NEBIUS_API_KEY"):
        secret = os.getenv(name)
        if secret:
            message = message.replace(secret, "[redacted]")
    return f"{type(error).__name__}: {message}"


def _normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def _quote_matches(quote: str, text: str) -> bool:
    needle = _normalized(quote)
    haystack = _normalized(text)
    if not needle or needle in haystack:
        return bool(needle)
    length = len(needle)
    return any(
        difflib.SequenceMatcher(None, needle, haystack[start : start + length]).ratio() >= 0.85
        for start in range(max(0, len(haystack) - length + 1))
    )


def build_research_tools(
    connector: ResearchConnector,
    ledger: EvidenceLedger,
    findings: FindingStore,
    events: EventSink,
    agent_name: str,
) -> list[BaseTool]:
    @tool
    async def web_search(
        query: str,
        why: str,
        include_domains: list[str] | None = None,
        topic: Literal["general", "news"] = "general",
    ) -> str:
        """Search the web and add the result sources to the evidence ledger."""
        try:
            page = await connector.search(
                ResearchSearch(
                    query=query,
                    search_depth="advanced",
                    max_results=5,
                    include_domains=tuple(include_domains or ()),
                    topic=topic,
                )
            )
            ledger.add_page(page)
            await events.emit(
                "search",
                agent=agent_name,
                why=why,
                detail={"query": query, "result_count": len(page.evidence)},
            )
            results = [ledger.get(item.evidence_id) or item for item in page.evidence]
            return json.dumps(
                [
                    {
                        "evidence_id": item.evidence_id,
                        "title": item.title,
                        "url": item.original_url,
                        "published_date": item.published_at_raw
                        or (
                            item.published_at.isoformat() if item.published_at is not None else None
                        ),
                        "snippet": (item.snippet or "")[:500],
                    }
                    for item in results
                ],
                ensure_ascii=False,
            )
        except Exception as error:
            message = _clean_error(error)
            await events.emit(
                "tool_error",
                agent=agent_name,
                why=why,
                detail={"tool": "web_search", "error": message},
            )
            return f"Search failed: {message}"

    @tool
    async def extract_page(evidence_id: str, why: str) -> str:
        """Extract a page using an evidence id returned by web_search."""
        source = ledger.get(evidence_id)
        if source is None:
            return "Unknown evidence_id. Use an id returned by web_search."
        try:
            page = await connector.extract(
                ResearchExtract(
                    urls=(source.original_url,),
                    query=source.query,
                    candidate_id=source.candidate_id,
                    analysis_id=source.analysis_id,
                    extract_depth="advanced",
                )
            )
            extracted = ledger.add_page(page)
            target = next(
                (item for item in extracted if item.canonical_url == source.canonical_url),
                extracted[0] if extracted else None,
            )
            await events.emit(
                "extract",
                agent=agent_name,
                why=why,
                detail={
                    "evidence_id": target.evidence_id if target else None,
                    "source_evidence_id": evidence_id,
                    "result_count": len(extracted),
                },
            )
            if target is None:
                return "Extraction returned no evidence for that page."
            return json.dumps(
                {
                    "evidence_id": target.evidence_id,
                    "url": target.original_url,
                    "content": (target.passage or target.snippet or "")[:4000],
                },
                ensure_ascii=False,
            )
        except Exception as error:
            message = _clean_error(error)
            await events.emit(
                "tool_error",
                agent=agent_name,
                why=why,
                detail={"tool": "extract_page", "evidence_id": evidence_id, "error": message},
            )
            return f"Extraction failed: {message}"

    @tool
    async def record_finding(
        claim: str,
        quote: str,
        evidence_ids: list[str],
        claim_type: Literal["fact", "estimate", "opinion", "future"],
        localization: str | None = None,
        why: str = "",
    ) -> str:
        """Record a claim only when a cited source contains its verbatim quote."""
        payload = {
            "claim": claim,
            "quote": quote,
            "evidence_ids": evidence_ids,
            "claim_type": claim_type,
            "localization": localization,
        }
        unknown = [evidence_id for evidence_id in evidence_ids if not ledger.has(evidence_id)]
        if unknown:
            reason = f"Unknown evidence ids: {', '.join(unknown)}"
        elif not evidence_ids:
            reason = "At least one evidence id is required."
        elif not any(
            _quote_matches(quote, item.passage or item.snippet or "")
            for evidence_id in evidence_ids
            if (item := ledger.get(evidence_id)) is not None
        ):
            reason = "The quote was not found in the stored text of any cited evidence."
        else:
            try:
                finding = findings.accept(claim, quote, evidence_ids, claim_type, localization)
                await events.emit(
                    "finding",
                    agent=agent_name,
                    why=why,
                    detail={
                        "finding_id": finding["finding_id"],
                        "evidence_ids": evidence_ids,
                    },
                )
                return finding["finding_id"]
            except Exception as error:
                message = _clean_error(error)
                await events.emit(
                    "tool_error",
                    agent=agent_name,
                    why=why,
                    detail={"tool": "record_finding", "error": message},
                )
                return f"Finding could not be recorded: {message}"
        findings.reject(payload | {"reason": reason})
        await events.emit(
            "finding_rejected",
            agent=agent_name,
            why=why,
            detail={"reason": reason, "evidence_ids": evidence_ids},
        )
        return f"Finding rejected: {reason}"

    return [web_search, extract_page, record_finding]
