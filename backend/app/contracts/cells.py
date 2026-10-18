from typing import Literal

from pydantic import BaseModel, Field


class Layer(BaseModel):
    id: str
    label: str
    direction: Literal["+", "-"]
    weight_default: float
    source: str | None = None
    evidence_id: str | None = None
    why: str


class Modifier(BaseModel):
    type: Literal["future"] = "future"
    label: str
    quote: str
    evidence_id: str
    localization: Literal["plr", "address"] = "plr"
    bonus: float = 0.10
    magnitude: dict | None = None


class Risk(BaseModel):
    label: str
    quote: str
    evidence_id: str
    severity: Literal["low", "medium", "high"]
    claim_type: str


class Cell(BaseModel):
    id: str
    name: str
    geometry_ref: str
    values: dict[str, float | None]
    modifiers: list[Modifier] = Field(default_factory=list)
    score_present: float | None = None
    score_future: float | None = None
    risks: list[Risk] = Field(default_factory=list)
    demoted: bool = False


class Area(BaseModel):
    name: str
    level: str
    cell_count: int


class CellsFile(BaseModel):
    area: Area
    mode_default: str = "present"
    layers: list[Layer]
    cells: list[Cell]
