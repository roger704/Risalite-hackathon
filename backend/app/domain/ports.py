"""Application ports; provider adapters do not publish snapshots themselves."""

from typing import Protocol

from .models import (
    DatasetConfig,
    FetchRequest,
    NormalizedBatch,
    QualityReport,
    RawBundle,
    SourceHealth,
)


class SnapshotConnector(Protocol):
    adapter_version: str

    async def probe(self, config: DatasetConfig) -> SourceHealth: ...

    async def fetch(self, request: FetchRequest) -> RawBundle: ...

    def normalize(self, raw: RawBundle, config: DatasetConfig) -> NormalizedBatch: ...

    def validate(self, batch: NormalizedBatch) -> QualityReport: ...
