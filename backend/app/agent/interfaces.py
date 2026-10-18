from collections.abc import Sequence
from typing import Literal, Protocol

from pydantic import BaseModel

from app.contracts.api import PlanLayer
from app.contracts.cells import CellsFile
from app.contracts.evidence import EvidenceEntry
from app.domain.research import ResearchConnector, WebEvidence


class Ledger(Protocol):
    """S4's app.evidence.ledger.EvidenceLedger is intended; map add to append and all to entries."""

    def add(self, entry: EvidenceEntry) -> str: ...

    def get(self, evidence_id: str) -> EvidenceEntry | None: ...

    def all(self) -> list[EvidenceEntry]: ...

    def ids(self) -> set[str]: ...


class CellScorer(Protocol):
    def build_cells(self, area: str, layers: list[PlanLayer], ledger: Ledger) -> CellsFile: ...

    def rescore(self, cells: CellsFile, layers: list[PlanLayer]) -> CellsFile: ...


class FactChecker(Protocol):
    async def check(self, entries: list[EvidenceEntry]) -> list[EvidenceEntry]: ...


class ModelClient(Protocol):
    async def structured(
        self,
        role: str,
        operation: Literal["plan", "synthesize", "review"],
        context: dict,
        output_model: type[BaseModel],
        evidence: Sequence[WebEvidence] = (),
    ) -> tuple[BaseModel, int]: ...

    def model_name(self, role: str) -> str: ...


__all__ = ["CellScorer", "FactChecker", "Ledger", "ModelClient", "ResearchConnector"]
