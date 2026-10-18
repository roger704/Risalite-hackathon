from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Contract(BaseModel):
    model_config = ConfigDict(extra="allow")


class Layer(Contract):
    id: str
    label: str
    direction: Literal["+", "-"]
    weight_default: float
    source: str
    evidence_id: str
    why: str
    units: str | None = None
    method: str | None = None
    required: bool | None = None


class Modifier(Contract):
    type: str
    label: str
    quote: str
    evidence_id: str
    localization: str = "plr"
    bonus: float = 0.10
    magnitude: dict | None = None


class Risk(Contract):
    text: str
    quote: str
    evidence_id: str
    severity: str


class Cell(Contract):
    id: str
    name: str
    geometry_ref: str
    values: dict[str, float]
    modifiers: list[Modifier] = Field(default_factory=list)
    score_present: float
    score_future: float
    raw_values: dict[str, float | None] | None = None
    evidence_ids: dict[str, str] | None = None
    risks: list[Risk] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    missing_layers: list[str] = Field(default_factory=list)


class ExcludedCell(Contract):
    id: str
    name: str
    reason: str


class AreaInfo(Contract):
    name: str
    level: str = "plr"
    cell_count: int
    matched_on: str | None = None
    plr_ids: list[str] | None = None


class CellsDocument(Contract):
    area: AreaInfo
    mode_default: Literal["present", "future"] = "present"
    layers: list[Layer]
    cells: list[Cell]
    excluded_cells: list[ExcludedCell] = Field(default_factory=list)
    template: str | None = None
    scoring_version: str | None = None
    generated_at: str | None = None


class LocateResult(Contract):
    address: dict
    point: dict
    plr_id: str
    plr_name: str
    values: dict[str, float | None]
    raw_values: dict[str, float | None]
    osm: list[dict]
    ihk: list[dict]
    evidence: list[dict]
