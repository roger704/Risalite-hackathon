"""Structured output models used by the research inference gateway."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class GapQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=3, max_length=400)
    rationale: str = Field(min_length=1, max_length=1000)


class CandidatePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(min_length=1)
    gap_queries: list[GapQuery] = Field(max_length=5)


class ResearchPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidates: list[CandidatePlan]


ClaimKind = Literal["data", "web", "assumption", "proxy"]


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=2000)
    kind: ClaimKind
    evidence_ids: list[str] = []

    @model_validator(mode="after")
    def web_claim_requires_evidence(self) -> "Claim":
        if self.kind == "web" and not self.evidence_ids:
            raise ValueError("web claims require at least one evidence id")
        return self


class Synthesis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claims: list[Claim]


class Challenge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim: str = Field(min_length=1)
    evidence_ids: list[str] = []
    severity: Literal["low", "medium", "high"]
    adjustment_type: Literal["none", "penalty", "exclude"]
    magnitude: float = Field(ge=0, le=1)
    is_assumption: bool

    @model_validator(mode="after")
    def validate_adjustment(self) -> "Challenge":
        if self.adjustment_type == "none" and self.magnitude != 0:
            raise ValueError("none adjustment requires zero magnitude")
        if self.adjustment_type == "penalty" and self.magnitude <= 0:
            raise ValueError("penalty adjustment requires positive magnitude")
        return self


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid")

    challenges: list[Challenge]
