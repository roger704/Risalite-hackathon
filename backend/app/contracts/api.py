from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.contracts.cells import CellsFile
from app.contracts.events import ResearchEvent
from app.contracts.evidence import EvidenceEntry


class ResearchRequest(BaseModel):
    question: str = Field(min_length=3)
    role: str | None = None
    profiles: dict | None = None


class Classification(BaseModel):
    shape: Literal["location", "gap", "point"]
    template: Literal["coffee", "kita", "property"] | None = None
    area: str | None = None
    address: str | None = None
    why: str
    needs_clarification: bool = False
    clarification_question: str | None = None


class PlanLayer(BaseModel):
    id: str
    label: str
    direction: Literal["+", "-"]
    weight: float
    why: str
    enabled: bool = True


class SubQuestion(BaseModel):
    id: str
    text: str
    source_class: Literal["official_geodata", "web", "structured_api"]
    domains: list[str]
    enabled: bool = True


class PaidSource(BaseModel):
    id: str
    name: str
    status: str = "key not provided — skipped"


class WeightAdjustment(BaseModel):
    layer_id: str
    from_weight: float
    to_weight: float
    reason: str


class Plan(BaseModel):
    layers: list[PlanLayer]
    sub_questions: list[SubQuestion]
    paid_sources: list[PaidSource]
    weight_adjustments: list[WeightAdjustment] = Field(default_factory=list)


RunStatus = Literal["awaiting_approval", "needs_clarification", "running", "done", "failed"]


class RunState(BaseModel):
    run_id: str
    status: RunStatus
    question: str
    created_at: datetime
    classification: Classification
    plan: Plan | None = None
    error: str | None = None


class LayerEdit(BaseModel):
    id: str
    enabled: bool | None = None
    weight: float | None = None


class SubQuestionEdit(BaseModel):
    id: str
    enabled: bool


class ApproveRequest(BaseModel):
    layers: list[LayerEdit] | None = None
    sub_questions: list[SubQuestionEdit] | None = None


class RunResult(BaseModel):
    run_id: str
    status: RunStatus
    cells: CellsFile | None = None
    report_md: str | None = None
    evidence: list[EvidenceEntry]
    trace: list[ResearchEvent]
