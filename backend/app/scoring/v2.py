"""Scoring v2 (Deep_Research_Spec §5.8). Interface stub: bodies are implemented by the scoring work unit."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

Direction = Literal["+", "-"]
Mode = Literal["present", "future"]
Unit = Literal["dwellings", "kita_places", "children_0_6"]


@dataclass(frozen=True)
class LayerSpec:
    id: str
    label: str
    direction: Direction
    weight: float
    transform: Literal["identity", "log1p"] = "identity"
    required: bool = False
    unit: str | None = None


@dataclass(frozen=True)
class Template:
    id: str
    layers: tuple[LayerSpec, ...]
    min_population: int = 2000
    tie_band: float = 0.02
    # modifier kind -> effect direction on the score (e.g. T3 "kita_opening": "-")
    future_effect: dict[str, Direction] = field(default_factory=dict)


@dataclass
class CellInput:
    cell_id: str
    name: str
    residents: float | None
    raw: dict[str, float | None]
    parent_name: str | None = None
    incomplete: bool = False
    gap_low: float | None = None
    gap_high: float | None = None
    layer_evidence: dict[str, str] = field(default_factory=dict)  # layer id -> ledger id


@dataclass
class FutureModifier:
    cell_id: str
    kind: str
    unit: Unit
    amount: float
    evidence_id: str
    label: str
    quote: str


@dataclass
class ScoredCell:
    cell_id: str
    name: str
    values: dict[str, float | None]  # directional 0..1 within area (1 = good)
    raw_values: dict[str, float | None]
    score_present: float | None
    score_berlin: float | None
    score_future: float | None
    modifiers: list[dict] = field(default_factory=list)
    confidence: Literal["high", "medium", "low"] = "high"
    confidence_reasons: list[str] = field(default_factory=list)
    tier: int = 0
    top3_share: float = 0.0
    incomplete: bool = False
    gap_low: float | None = None
    gap_high: float | None = None
    risks: list[dict] = field(default_factory=list)
    demoted_by: list[str] = field(default_factory=list)
    parent_name: str | None = None


@dataclass
class ScoreResult:
    template_id: str
    cells: list[ScoredCell]
    excluded: list[dict]  # {"cell_id", "name", "reason"}


def load_template(template_id: str) -> Template:
    """Read backend/config/templates/<template_id>.json."""
    raise NotImplementedError


def percentile_rank(value: float, reference: Sequence[float]) -> float:
    """Mid-rank percentile of value within reference, 0..1 (shared with T1)."""
    raise NotImplementedError


def bonus_size(unit: Unit, amount: float) -> tuple[str, float]:
    """(size label, magnitude) per §5.8.4 unit thresholds."""
    raise NotImplementedError


def score_area(
    cells: list[CellInput],
    template: Template,
    *,
    berlin_reference: list[CellInput] | None = None,
    modifiers: Sequence[FutureModifier] = (),
    extra_confidence_reasons: dict[str, list[str]] | None = None,
) -> ScoreResult:
    raise NotImplementedError


def rank(
    cells: list[ScoredCell],
    mode: Mode = "present",
    *,
    high_risk: dict[str, list[str]] | None = None,
) -> list[ScoredCell]:
    """§5.5/§5.8.6: sets demoted_by, incomplete cells after all eligible ones."""
    raise NotImplementedError


def to_cells_document(result: ScoreResult, area: dict, template: Template, *, mode_default: Mode = "present"):
    """Build app.contracts.cells.CellsDocument (+ §5.8.7 extra fields)."""
    raise NotImplementedError
