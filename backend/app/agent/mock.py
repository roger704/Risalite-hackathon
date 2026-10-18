from datetime import datetime, timezone
from typing import Sequence

from pydantic import BaseModel

from app.agent.classify import _fallback
from app.agent.schemas import ModifierOutput, RiskOutput, WriterOutput
from app.contracts.api import Classification
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


class MockResearch:
    def __init__(self):
        self.searches = 0
        self.extracts = 0
        self._urls: dict[str, tuple[str, str, datetime]] = {}

    @staticmethod
    def _item(url: str, query: str, title: str, quote: str, published_at: datetime) -> WebEvidence:
        canonical = canonicalize_url(url)
        return WebEvidence(
            evidence_id=make_evidence_id(EvidenceKind.SEARCH_RESULT, canonical, query, None, None),
            kind=EvidenceKind.SEARCH_RESULT,
            original_url=url,
            canonical_url=canonical,
            title=title,
            query=query,
            retrieved_at=datetime.now(timezone.utc),
            published_at=published_at,
            published_at_raw=published_at.date().isoformat(),
            snippet=quote,
            passage=None,
            passage_locator=None,
            extraction_status=ExtractionStatus.OK,
            failure_reason=None,
            content_hash=None,
            candidate_id=None,
            analysis_id=None,
            provider="risalite-mock",
            provider_request_id=None,
            score=1.0,
        )

    async def search(self, request: ResearchSearch) -> EvidencePage:
        self.searches += 1
        future_url = "https://example.org/risalite-mock/future-housing"
        risk_url = "https://example.org/risalite-mock/wrangel-rents"
        future_quote = "300 Wohnungen bis 2028 im Umfeld des Wrangelkiez."
        risk_quote = "Erdgeschossmieten im Wrangelkiez +18 % gegenüber Vorjahr"
        self._urls[future_url] = (
            "Planned housing project",
            future_quote,
            datetime(2026, 9, 1, tzinfo=timezone.utc),
        )
        self._urls[risk_url] = (
            "Commercial rent trend",
            risk_quote,
            datetime(2026, 9, 1, tzinfo=timezone.utc),
        )
        return EvidencePage(
            evidence=(
                self._item(
                    future_url,
                    request.query,
                    "Planned housing project",
                    future_quote,
                    datetime(2026, 9, 1, tzinfo=timezone.utc),
                ),
                self._item(
                    risk_url,
                    request.query,
                    "Commercial rent trend",
                    risk_quote,
                    datetime(2026, 9, 1, tzinfo=timezone.utc),
                ),
            ),
            status="ok",
        )

    async def extract(self, request: ResearchExtract) -> EvidencePage:
        self.extracts += 1
        entries = []
        for url in request.urls:
            title, quote, published_at = self._urls[url]
            entries.append(
                WebEvidence(
                    evidence_id=make_evidence_id(
                        EvidenceKind.EXTRACTED_PAGE,
                        canonicalize_url(url),
                        request.query,
                        None,
                        None,
                    ),
                    kind=EvidenceKind.EXTRACTED_PAGE,
                    original_url=url,
                    canonical_url=canonicalize_url(url),
                    title=title,
                    query=request.query,
                    retrieved_at=datetime.now(timezone.utc),
                    published_at=published_at,
                    published_at_raw=published_at.date().isoformat(),
                    snippet=quote,
                    passage=quote,
                    passage_locator=None,
                    extraction_status=ExtractionStatus.OK,
                    failure_reason=None,
                    content_hash=None,
                    candidate_id=None,
                    analysis_id=None,
                    provider="risalite-mock",
                    provider_request_id=None,
                    score=1.0,
                )
            )
        return EvidencePage(evidence=tuple(entries), status="ok")


class MockModelClient:
    def model_name(self, role: str) -> str:
        return "mock-" + role

    async def structured(
        self,
        role: str,
        operation: str,
        context: dict,
        output_model: type[BaseModel],
        evidence: Sequence[WebEvidence] = (),
    ) -> tuple[BaseModel, int]:
        if output_model is Classification:
            return _fallback(context["question"]), 800
        if output_model.__name__ == "PlanProposal":
            return output_model(weight_adjustments=[]), 800
        entries = context.get("evidence", [])
        if output_model is ModifierOutput:
            future = next(
                (entry for entry in entries if "bis 2028" in (entry.get("quote") or "")), None
            )
            modifiers = (
                [{"evidence_id": future["evidence_id"], "label": "Planned housing development"}]
                if future
                else []
            )
            return output_model(modifiers=modifiers), 800
        if output_model is RiskOutput:
            risk = next(
                (entry for entry in entries if "Erdgeschossmieten" in (entry.get("quote") or "")),
                None,
            )
            risks = (
                [
                    {
                        "evidence_id": risk["evidence_id"],
                        "label": "Commercial rent pressure",
                        "severity": "high",
                    }
                ]
                if risk
                else []
            )
            return output_model(risks=risks), 800
        if output_model is WriterOutput:
            evidence_ids = [entry["evidence_id"] for entry in entries[:2]]
            return output_model(
                sentences=[
                    {
                        "text": "The synthetic fixture ranks the default layer mix and includes source-quoted future and risk signals.",
                        "claim_type": "proxy",
                        "evidence_ids": evidence_ids,
                    }
                ]
            ), 800
        return output_model(), 800
