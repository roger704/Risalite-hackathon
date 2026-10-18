import json
from datetime import datetime, timezone
from pathlib import Path

from app.agent.interfaces import Ledger
from app.contracts.api import PlanLayer
from app.contracts.cells import Area, Cell, CellsFile, Layer
from app.contracts.evidence import EvidenceEntry

SOURCES = {
    "demand_density": "https://gdi.berlin.de/services/wfs/ua_einwohnerdichte_2025",
    "young_adults": "https://gdi.berlin.de/services/wfs/ua_bevoelkerung_2025",
    "competition": "https://gdi.berlin.de/services/wfs/gewerbedaten",
    "transit": "https://gdi.berlin.de/services/wfs/oepnv_ungestoert",
    "cost": "https://gdi.berlin.de/services/wfs/berliner_bodenrichtwerte",
}
VALUE_ORDER = ["demand_density", "young_adults", "competition", "transit", "cost"]


class JsonlLedger:
    def __init__(self, path: Path):
        self.path = path
        self._entries: list[EvidenceEntry] = []
        if path.exists():
            self._entries = [
                EvidenceEntry.model_validate_json(line)
                for line in path.read_text().splitlines()
                if line
            ]

    def add(self, entry: EvidenceEntry) -> str:
        evidence_id = f"e{len(self._entries) + 1}"
        saved = entry.model_copy(update={"evidence_id": evidence_id})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(saved.model_dump_json() + "\n")
        self._entries.append(saved)
        return evidence_id

    def get(self, evidence_id: str) -> EvidenceEntry | None:
        return next((entry for entry in self._entries if entry.evidence_id == evidence_id), None)

    def all(self) -> list[EvidenceEntry]:
        return list(self._entries)

    def ids(self) -> set[str]:
        return {entry.evidence_id for entry in self._entries}


class FixtureCellScorer:
    def __init__(self, fixture_path: Path | None = None):
        self.fixture_path = (
            fixture_path or Path(__file__).parent / "fixtures/cells_friedrichshain-kreuzberg.json"
        )

    def build_cells(self, area: str, layers: list[PlanLayer], ledger: Ledger) -> CellsFile:
        fixture = json.loads(self.fixture_path.read_text(encoding="utf-8"))
        if area != fixture["area"]:
            area = fixture["area"]
        evidence_by_layer = {}
        for layer in layers:
            evidence_by_layer[layer.id] = ledger.add(
                EvidenceEntry(
                    evidence_id="pending",
                    method="fixture",
                    url=SOURCES.get(layer.id, "https://gdi.berlin.de/services/wfs/fixture"),
                    title=f"Synthetic fixture: {layer.label}",
                    quote="synthetic fixture value",
                    tier="official",
                    confidence="low",
                    retrieved_at=datetime.now(timezone.utc),
                    claim_type="proxy",
                )
            )
        layer_models = [
            Layer(
                id=layer.id,
                label=layer.label,
                direction=layer.direction,
                weight_default=layer.weight,
                source=SOURCES.get(layer.id),
                evidence_id=evidence_by_layer.get(layer.id),
                why=layer.why,
            )
            for layer in layers
        ]
        cells = []
        for row in fixture["cells"]:
            values = dict(zip(VALUE_ORDER, row["values"], strict=False))
            cells.append(
                Cell(
                    id=row["id"],
                    name=row["name"],
                    geometry_ref=f"cells.geojson#{row['id']}",
                    values={layer.id: values.get(layer.id) for layer in layers},
                )
            )
        result = CellsFile(
            area=Area(name=area, level="plr", cell_count=len(cells)),
            layers=layer_models,
            cells=cells,
        )
        return self.rescore(result, layers)

    def rescore(self, cells: CellsFile, layers: list[PlanLayer]) -> CellsFile:
        enabled = [layer for layer in layers if layer.enabled]
        raw_scores = []
        for cell in cells.cells:
            observed = [(layer, cell.values.get(layer.id)) for layer in enabled]
            observed = [(layer, value) for layer, value in observed if value is not None]
            total_weight = sum(layer.weight for layer, _ in observed)
            score = (
                sum(
                    layer.weight * (1 if layer.direction == "+" else -1) * value
                    for layer, value in observed
                )
                / total_weight
                if total_weight
                else 0.0
            )
            raw_scores.append(score)
        low, high = min(raw_scores, default=0), max(raw_scores, default=0)
        for cell, raw in zip(cells.cells, raw_scores, strict=True):
            normalized = (raw - low) / (high - low) if high > low else 0.5
            if cell.demoted:
                normalized = max(0, normalized - 0.15)
            cell.score_present = normalized
            cell.score_future = min(
                1.0, normalized + sum(modifier.bonus for modifier in cell.modifiers)
            )
        return cells


class PassThroughFactChecker:
    async def check(self, entries: list[EvidenceEntry]) -> list[EvidenceEntry]:
        return [
            entry.model_copy(
                update={
                    "status": "verified"
                    if entry.quote and entry.url and entry.method in {"fixture", "mock", "wfs"}
                    else entry.status
                }
            )
            for entry in entries
        ]
