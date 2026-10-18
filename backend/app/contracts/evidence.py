"""`runs/<id>/evidence.jsonl` — one ledger entry per line."""

from pydantic import BaseModel, ConfigDict


class EvidenceEntry(BaseModel):
    model_config = ConfigDict(extra="allow")

    evidence_id: str
    method: str
    url: str | None = None
    source_url: str | None = None
    title: str | None = None
    quote: str | None = None
    tier: str | None = None
    confidence: str | None = None
    status: str | None = None
    verification_status: str | None = None
    claim: str | None = None
    claim_type: str | None = None
    reason: str | None = None
    retrieved_at: str | None = None


class Finding(BaseModel):
    model_config = ConfigDict(extra="allow")

    finding_id: str
    claim: str
    quote: str
    evidence_ids: list[str]
    claim_type: str
    localization: str | None = None
    agent: str
    status: str
    reject_reason: str | None = None
    verification: str | None = None
