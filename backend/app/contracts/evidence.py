"""`runs/<id>/evidence.jsonl` — one ledger entry per line."""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class EvidenceEntry(BaseModel):
    model_config = ConfigDict(extra="allow")

    evidence_id: str
    url: str
    title: str | None = None
    quote: str
    method: Literal["wfs", "wfs-cached", "overpass", "osm", "web", "pdf", "api", "gtfs", "csv"]
    tier: str  # official | institutional | press | community | ...
    confidence: Literal["high", "medium", "low"] = "medium"
    status: Literal["verified", "partial", "unsupported", "unverified"] = "unverified"
    claim: str | None = None
    claim_type: str | None = None
    reason: str | None = None
    retrieved_at: str | None = None
