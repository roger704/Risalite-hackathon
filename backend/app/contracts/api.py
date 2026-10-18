"""HTTP API bodies for the research flow."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .candidates import CandidatesDocument
from .cells import CellsDocument
from .events import ResearchEvent
from .evidence import EvidenceEntry, Finding


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow")


Shape = Literal["point", "location", "gap"]
RunStatus = Literal[
    "needs_clarification",
    "awaiting_approval",
    "approved",
    "running",
    "researching",
    "done",
    "failed",
]


class Area(_Model):
    area_id: str
    level: str
    name: str
    plr_count: int = Field(ge=0)


class AreaCandidate(_Model):
    area_id: str
    level: str
    name: str
    bezirk: str | None = None


class SubQuestion(_Model):
    id: str
    text: str
    source_class: str = "web"
    sources: str | None = None
    layer_ids: list[str] = Field(default_factory=list)


class PaidSource(_Model):
    name: str
    used_for: str
    status: str = "key not provided — skipped"


class Plan(_Model):
    area: Area | None = None
    area_candidates: list[AreaCandidate] = Field(default_factory=list)
    sub_questions: list[SubQuestion] = Field(default_factory=list)
    layers: list[dict] = Field(default_factory=list)
    hypotheses: list[dict] = Field(default_factory=list)
    paid_sources: list[PaidSource] = Field(default_factory=list)


class ResearchRequest(_Model):
    question: str = Field(min_length=1)
    role: str | None = None
    template_hint: str | None = None
    profile: str | None = None


class ResearchResponse(_Model):
    run_id: str
    run_token: str
    status: RunStatus
    clarify_question: str | None = None
    plan: Plan | None = None
    mode: str | None = None
    question: str | None = None
    role: str | None = None
    profile: str | None = None
    shape: Shape | None = None
    classification: dict | None = None


class LayerApproval(_Model):
    id: str
    enabled: bool
    weight: float = Field(ge=0, le=1)


class ApproveRequest(_Model):
    area_id: str | None = None
    subquestions: list[str] | None = None
    layers: list[LayerApproval] | None = None
    wave2: bool | None = None
    hypothesis_ids: list[str] | None = None


class SelfGradeItem(_Model):
    ok: bool
    text: str


class RunResult(_Model):
    run_id: str
    status: Literal["running", "researching", "done", "failed"]
    report_md: str = ""
    cells: CellsDocument | None = None
    evidence: list[EvidenceEntry] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    metrics: dict[str, dict] = Field(default_factory=dict)
    question: str = ""
    role: str | None = None
    shape: Shape
    plan: Plan | None = None
    cells_geojson: dict | None = None
    candidates: CandidatesDocument | None = None
    self_grade: list[SelfGradeItem] = Field(default_factory=list)
    trace: list[ResearchEvent] = Field(default_factory=list)
