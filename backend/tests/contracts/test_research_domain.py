from datetime import datetime, timezone

import pytest

from app.domain.errors import ValidationError
from app.domain.research import (
    BudgetExceededError,
    EvidenceKind,
    ExtractionStatus,
    ResearchBudget,
    WebEvidence,
    canonicalize_url,
    make_evidence_id,
)


def evidence(**changes):
    values = dict(
        evidence_id="ev_test",
        kind=EvidenceKind.SEARCH_RESULT,
        original_url="https://example.com/page",
        canonical_url="https://example.com/page",
        title="Example",
        query="query",
        retrieved_at=datetime(2026, 10, 18, tzinfo=timezone.utc),
        published_at=None,
        published_at_raw=None,
        snippet="snippet",
        passage=None,
        passage_locator=None,
        extraction_status=ExtractionStatus.NOT_ATTEMPTED,
        failure_reason=None,
        content_hash=None,
        candidate_id=None,
        analysis_id=None,
        provider="tavily",
        provider_request_id=None,
        score=None,
    )
    return WebEvidence(**(values | changes))


def test_evidence_validates_urls_status_and_serializes_enum_and_utc():
    assert evidence().to_dict()["retrieved_at"] == "2026-10-18T00:00:00Z"
    with pytest.raises(ValidationError, match="http"):
        evidence(original_url="ftp://example.com/page")
    assert (
        evidence(
            original_url="ftp://example.com/page",
            canonical_url="ftp://example.com/page",
            extraction_status="failed",
            failure_reason="url_rejected: unsupported_scheme",
        ).extraction_status
        is ExtractionStatus.FAILED
    )
    with pytest.raises(ValidationError, match="failure_reason"):
        evidence(extraction_status="failed")
    with pytest.raises(ValidationError, match="requires passage or snippet"):
        evidence(extraction_status="ok", snippet=None)


def test_canonicalize_url_and_id_are_deterministic():
    assert (
        canonicalize_url("HTTPS://Example.COM:443/path?utm_source=news&x=1&gclid=abc#section")
        == "https://example.com/path?x=1"
    )
    assert canonicalize_url("https://user:password@example.com/path") == "https://example.com/path"
    args = ("search_result", "https://example.com", "q", "c1", "a1")
    assert make_evidence_id(*args) == make_evidence_id(*args)


def test_budget_checks_before_incrementing_and_allows_final_token_overshoot():
    budget = ResearchBudget(1, 2, 1, 2)
    budget.consume_search()
    with pytest.raises(BudgetExceededError) as exc:
        budget.consume_search()
    assert (exc.value.resource, exc.value.limit, exc.value.used) == ("searches", 1, 1)
    budget.record_tokens(3)
    assert budget.remaining_tokens() == 0
    assert budget.snapshot()["tokens_used"] == 3
