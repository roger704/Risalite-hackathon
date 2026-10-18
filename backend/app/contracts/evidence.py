"""``runs/<id>/evidence.jsonl`` ledger entry contract."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

EVIDENCE_ID_PATTERN = r"^e[1-9][0-9]*$"

Method = Literal[
    "wfs", "wfs-cached", "tavily-extract", "tavily-search", "local-snapshot", "overpass"
]
Tier = Literal["official", "institutional", "press", "community", "weak"]
Confidence = Literal["high", "medium", "low"]
Status = Literal["unverified", "verified", "partial", "unsupported"]

_UNSAFE_URL_CHARS = re.compile(r"[\x00-\x20\x7f\\]")


def validate_http_url(value: str) -> str:
    """Accept only absolute http(s) URLs with a host; never ``javascript:``/``data:`` etc."""
    if not isinstance(value, str) or not value or _UNSAFE_URL_CHARS.search(value):
        raise ValueError("url must be an absolute http(s) URL without whitespace")
    try:
        parts = urlsplit(value)
        host = parts.hostname
    except ValueError as exc:
        raise ValueError("url must be an absolute http(s) URL") from exc
    if parts.scheme.lower() not in {"http", "https"} or not host or parts.username:
        raise ValueError("url must be an absolute http(s) URL")
    return value


class SourceRef(BaseModel):
    """Provenance needed to re-run the request (WFS layer, local snapshot or web provider)."""

    model_config = ConfigDict(extra="allow")

    dataset_id: str | None = None
    snapshot_id: str | None = None
    query: dict[str, Any] | str | None = None
    value: Any = None
    content_hash: str | None = None
    provider_request_id: str | None = None
    web_evidence_id: str | None = None


class FactCheck(BaseModel):
    model_config = ConfigDict(extra="allow")

    checked_at: datetime
    ratio: float | None = None
    reason: str
    fetch_method: str | None = None


class LedgerEntry(BaseModel):
    """One line of ``evidence.jsonl``. Later lines with the same ``evidence_id`` are revisions."""

    model_config = ConfigDict(extra="allow")

    evidence_id: str = Field(pattern=EVIDENCE_ID_PATTERN)
    url: str
    quote: str = Field(min_length=1)
    retrieved_at: datetime
    method: Method
    tier: Tier
    confidence: Confidence
    status: Status = "unverified"
    title: str | None = None
    claim_type: str | None = None
    source_ref: SourceRef | None = None
    fact_check: FactCheck | None = None
    revision: int = Field(default=0, ge=0)

    @field_validator("url")
    @classmethod
    def _http_url(cls, value: str) -> str:
        return validate_http_url(value)

    @property
    def citation_number(self) -> int:
        return int(self.evidence_id[1:])
