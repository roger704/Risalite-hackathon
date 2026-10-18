"""``runs/<id>/trace.jsonl`` research event contract."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ResearchEvent(BaseModel):
    model_config = ConfigDict(extra="allow")

    run_id: str
    seq: int = Field(ge=0)
    ts: datetime
    role: str
    type: str
    text: str
    why: str
    model: str | None = None
    tokens: int | None = Field(default=None, ge=0)
    seconds: float | None = Field(default=None, ge=0)
    evidence_id: str | None = None
    data: dict[str, Any] | None = None
