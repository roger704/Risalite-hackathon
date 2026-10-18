"""`runs/<id>/trace.jsonl` research event (also the SSE `data:` payload)."""

from pydantic import BaseModel, ConfigDict, Field


class ResearchEvent(BaseModel):
    model_config = ConfigDict(extra="allow")

    seq: int = Field(ge=0)
    run_id: str
    stage: str  # classify | plan | approve | research | skeptic | fact-check | write | done
    role: str
    type: str  # e.g. tool:wfs, tavily:search, reject, rescore, replan, skip, done
    text: str
    why: str | None = None
    model: str | None = None
    tokens: int = 0
    seconds: float = 0.0
    evidence_ids: list[str] = Field(default_factory=list)
    ts: str | None = None
