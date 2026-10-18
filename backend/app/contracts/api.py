"""HTTP API bodies: POST /research, POST /runs/{id}/approve, GET /runs/{id}/result."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .candidates import CandidatesDocument
from .cells import CellsDocument, Layer
from .events import ResearchEvent
from .evidence import EvidenceEntry


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow")


Shape = Literal["point", "location", "gap"]


class ResearchRequest(_Model):
    question: str = Field(min_length=1)
    role: str | None = None
    profile: str | None = None


class Classification(_Model):
    shape: Shape
    why: str
    area: str | None = None
    address: str | None = None


class SubQuestion(_Model):
    id: str
    text: str
    source_class: str
    sources: str | None = None
    layer_ids: list[str] = Field(default_factory=list)


class PaidSource(_Model):
    name: str
    used_for: str
    status: str = "key not provided — skipped"


class Plan(_Model):
    sub_questions: list[SubQuestion]
    layers: list[Layer] = Field(default_factory=list)
    hypotheses: list[dict] = Field(default_factory=list)
    paid_sources: list[PaidSource] = Field(default_factory=list)
    area_cells: int | None = None


class ResearchResponse(_Model):
    run_id: str
    status: Literal["awaiting_approval", "needs_clarification", "running", "done", "failed"]
    question: str
    classification: Classification | None = None
    clarification: str | None = None
    plan: Plan | None = None


class ApproveRequest(_Model):
    sub_question_ids: list[str] | None = None
    layer_weights: dict[str, float] | None = None
    disabled_layers: list[str] = Field(default_factory=list)
    hypothesis_ids: list[str] | None = None
    keys: dict[str, str] = Field(default_factory=dict)


class SelfGradeItem(_Model):
    ok: bool
    text: str


class RunResult(_Model):
    run_id: str
    status: Literal["running", "done", "failed"]
    question: str
    role: str | None = None
    shape: Shape
    plan: Plan | None = None
    summary: str = ""  # inline citations as [evidence_id]
    cells: CellsDocument | None = None
    cells_geojson: dict | None = None
    candidates: CandidatesDocument | None = None
    evidence: list[EvidenceEntry] = Field(default_factory=list)
    trace: list[ResearchEvent] = Field(default_factory=list)
    models: dict[str, str] = Field(default_factory=dict)
    self_grade: list[SelfGradeItem] = Field(default_factory=list)
