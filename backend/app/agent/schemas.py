from typing import Literal

from pydantic import BaseModel, Field


class ModifierProposal(BaseModel):
    evidence_id: str
    label: str


class ModifierOutput(BaseModel):
    modifiers: list[ModifierProposal] = Field(default_factory=list)


class RiskProposal(BaseModel):
    evidence_id: str
    label: str
    severity: Literal["low", "medium", "high"]
    claim_type: str = "web"


class RiskOutput(BaseModel):
    risks: list[RiskProposal] = Field(default_factory=list)


class WrittenSentence(BaseModel):
    text: str
    claim_type: Literal["data", "web", "proxy", "assumption"]
    evidence_ids: list[str] = Field(default_factory=list)


class WriterOutput(BaseModel):
    sentences: list[WrittenSentence] = Field(default_factory=list)
