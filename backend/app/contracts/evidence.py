from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class EvidenceEntry(BaseModel):
    model_config = ConfigDict(extra="allow")

    evidence_id: str
    method: Literal[
        "wfs", "wfs-cached", "tavily-extract", "tavily-search", "local-snapshot", "overpass"
    ]
    url: str
    title: str | None = None
    quote: str = Field(min_length=1)
    tier: Literal["official", "institutional", "press", "community", "weak"]
    confidence: Literal["high", "medium", "low"] = "medium"
    status: Literal["unverified", "verified", "partial", "unsupported"] = "unverified"
    retrieved_at: datetime
    source_ref: dict | None = None
    revision: int = 0
    published_at: datetime | None = None
    cell_id: str | None = None
    claim_type: str | None = None
    query: str | None = None
