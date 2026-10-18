"""`runs/<id>/cells.json` — area result (T2/T3), exactly as in the spec plus optional fields."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow")


class Area(_Model):
    name: str
    level: str = "plr"
    cell_count: int = Field(ge=0)


class Layer(_Model):
    id: str
    label: str
    direction: Literal["+", "-"]
    weight_default: float = Field(ge=0)
    source: str | None = None
    evidence_id: str
    why: str | None = None
    # Optional (S3): unit of `raw_values`, e.g. "cafés" or "residents/ha".
    unit: str | None = None


class Modifier(_Model):
    type: Literal["future"] = "future"
    label: str
    quote: str
    evidence_id: str
    localization: Literal["plr", "address"] = "plr"
    bonus: float = 0.10
    magnitude: dict[str, float | int | str] | None = None


class Risk(_Model):
    """Optional (S3): skeptic finding attached to a cell."""

    severity: Literal["low", "medium", "high"]
    text: str
    quote: str | None = None
    evidence_id: str
    demotion: float = 0.0


class Cell(_Model):
    id: str
    name: str
    geometry_ref: str | None = None
    # Normalized 0–1 within the area; None = missing (never zero).
    values: dict[str, float | None]
    modifiers: list[Modifier] = Field(default_factory=list)
    score_present: float | None = None
    score_future: float | None = None
    # Optional (S3) additions.
    raw_values: dict[str, float | int | None] = Field(default_factory=dict)
    risks: list[Risk] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    parent_name: str | None = None


class CellsDocument(_Model):
    area: Area
    mode_default: Literal["present", "future"] = "present"
    layers: list[Layer]
    cells: list[Cell]
