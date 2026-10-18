import json
from typing import Any

from pydantic import BaseModel, ConfigDict


class Evidence(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    method: str
    title: str
    url: str
    quote: str
    tier: str
    confidence: str
    status: str = "unverified"
    claim_type: str | None = None
    source_snapshot_id: str | None = None
    retrieved_at: str | None = None
    units: str | None = None
    value: float | int | str | None = None
    details: dict[str, Any] | None = None
    created_by: str = "s2_scoring"

    def to_jsonl_line(self) -> str:
        return json.dumps(
            self.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            allow_nan=False,
        )
