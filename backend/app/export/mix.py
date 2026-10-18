"""Dashboard-equivalent deterministic area scoring."""

from dataclasses import dataclass
from typing import Any, Literal

from app.domain import ConnectorError


@dataclass(frozen=True)
class Mix:
    layers_on: frozenset[str] | None = None
    mode: Literal["present", "future"] | None = None


@dataclass(frozen=True)
class RankedCell:
    cell: dict[str, Any]
    score: float
    rank: int


def rank_cells(cells_doc: dict[str, Any], mix: Mix | None = None) -> list[RankedCell]:
    mix = mix or Mix()
    layers = cells_doc.get("layers", [])
    known_layers = {layer["id"] for layer in layers}
    selected = known_layers if mix.layers_on is None else set(mix.layers_on)
    if not selected:
        raise ConnectorError("INVALID_REQUEST", "at least one layer")
    unknown = selected - known_layers
    if unknown:
        raise ConnectorError("UNKNOWN_LAYER", "Unknown layer: " + ", ".join(sorted(unknown)))
    mode = mix.mode or cells_doc.get("mode_default", "present")

    raw_scores = []
    for cell in cells_doc.get("cells", []):
        raw = sum(
            layer.get("weight_default", 0)
            * (1 if layer.get("direction") == "+" else -1)
            * cell.get("values", {}).get(layer["id"], 0)
            for layer in layers
            if layer["id"] in selected
        )
        raw_scores.append(raw)
    if not raw_scores:
        return []
    low, high = min(raw_scores), max(raw_scores)
    scored = []
    for cell, raw in zip(cells_doc.get("cells", []), raw_scores, strict=True):
        base = 0.5 if high == low else (raw - low) / (high - low)
        adjusted = base
        if any(risk.get("severity") == "high" for risk in cell.get("risks", [])):
            adjusted -= 0.08
        if mode == "future":
            adjusted += sum(modifier.get("bonus", 0.10) for modifier in cell.get("modifiers", []))
        scored.append((cell, adjusted))
    scored.sort(key=lambda item: item[1], reverse=True)
    return [
        RankedCell(cell, max(0.0, min(1.0, score)), rank)
        for rank, (cell, score) in enumerate(scored, start=1)
    ]
