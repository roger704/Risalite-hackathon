from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class ResearchEvent(BaseModel):
    seq: int
    run_id: str
    ts: datetime
    stage: Literal[
        "classify",
        "plan",
        "approve",
        "research",
        "skeptic",
        "rescore",
        "fact-check",
        "write",
        "budget",
        "error",
        "done",
    ]
    role: str
    type: str
    text: str
    why: str
    model: str | None = None
    tokens: int = 0
    seconds: float = 0.0
    data: dict | None = None
