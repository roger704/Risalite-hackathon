"""`runs/<id>/candidates.json` — point path (T1, LocalLens); minimal S3 reading."""

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow")


class Space(_Model):
    address: str
    lat: float
    lon: float
    cell_id: str | None = None
    cell_name: str | None = None
    evidence_id: str | None = None


class Dimension(_Model):
    name: str
    score: float = Field(ge=0, le=10)
    why: str
    evidence_ids: list[str] = Field(default_factory=list)


class Claim(_Model):
    text: str
    evidence_id: str | None = None


class Unknown(_Model):
    text: str
    suggested_validation: str


class Hypothesis(_Model):
    id: str
    name: str
    score: float
    rank_initial: int
    rank_final: int
    dimensions: list[Dimension]
    for_: list[Claim] = Field(default_factory=list, alias="for")
    against: list[Claim] = Field(default_factory=list)
    unknowns: list[Unknown] = Field(default_factory=list)


class Competitor(_Model):
    hypothesis_id: str
    lat: float
    lon: float
    name: str | None = None
    evidence_id: str


class CandidatesDocument(_Model):
    space: Space
    operator: dict | None = None
    hypotheses: list[Hypothesis]
    competitors: list[Competitor] = Field(default_factory=list)
    radius_m: int = 500
