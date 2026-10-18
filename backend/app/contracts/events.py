"""§9.1 research event (also the SSE ``data:`` payload)."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ResearchEvent(BaseModel):
    model_config = ConfigDict(extra="allow")

    seq: int = Field(ge=0)
    run_id: str
    ts: str
    type: Literal[
        "status",
        "classify",
        "clarify",
        "area",
        "plan",
        "approved",
        "layer",
        "score",
        "search",
        "extract",
        "finding_accepted",
        "finding_rejected",
        "model_call",
        "tool_error",
        "budget_exhausted",
        "merge",
        "skeptic_risk",
        "fact_check",
        "review",
        "rescore",
        "write",
        "metrics",
        "error",
    ]
    agent: str
    why: str | None = None
    detail: dict | str = Field(default_factory=dict)
    model: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    seconds: float | None = None
