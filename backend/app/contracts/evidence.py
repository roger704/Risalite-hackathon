from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class EvidenceEntry(BaseModel):
    evidence_id: str
    method: Literal["wfs", "overpass", "web", "pdf", "fixture", "mock"]
    url: str
    title: str | None = None
    quote: str | None = None
    tier: Literal["official", "institutional", "press", "community", "weak"]
    confidence: Literal["high", "medium", "low"] = "medium"
    status: Literal["unverified", "verified", "partial", "unsupported"] = "unverified"
    retrieved_at: datetime
    published_at: datetime | None = None
    cell_id: str | None = None
    claim_type: str | None = None
    query: str | None = None
