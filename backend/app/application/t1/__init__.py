"""T1 LocalLens point path (Deep_Research_Spec §6): catalog, point preparation, candidates."""

from app.application.t1.candidates import build_candidates
from app.application.t1.catalog import Catalog, CatalogType, load_catalog
from app.application.t1.point import prepare_point, resolve_address
from app.application.t1.readers import (
    Poi,
    PointReaders,
    ReferencePlr,
    SnapshotReaders,
    StaticReaders,
)

__all__ = [
    "Catalog",
    "CatalogType",
    "Poi",
    "PointReaders",
    "ReferencePlr",
    "SnapshotReaders",
    "StaticReaders",
    "build_candidates",
    "load_catalog",
    "prepare_point",
    "resolve_address",
]
