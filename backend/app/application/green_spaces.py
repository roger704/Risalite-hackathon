"""Straight-line proximity from a point to official green-space polygons.

Distances are measured in EPSG:25833 to the polygon edge (0 when the point lies
inside). They are not walking distances or walking times.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass

import numpy as np
import shapely
from pyproj import Transformer
from shapely.geometry import Point, shape
from shapely.strtree import STRtree

from app.domain import ConnectorError, GreenSpace


def transform(func, geometry):
    return shapely.transform(geometry, lambda xy: np.column_stack(func(xy[:, 0], xy[:, 1])))


ADAPTER = "berlin_green_spaces"
DISTANCE_METHOD = "straight_line_to_polygon_edge_epsg25833"
DISTANCE_NOTE = (
    "Straight-line distance from the query point to the nearest polygon edge in EPSG:25833; "
    "0 when the point is inside. Not a walking distance or walking time: entrances, barriers "
    "and the street network are ignored."
)
BERLIN_BBOX = (13.0, 52.3, 13.9, 52.8)
MAX_RADIUS_M = 5000.0
_TO_METRIC = Transformer.from_crs("EPSG:4326", "EPSG:25833", always_xy=True)


@dataclass(frozen=True)
class _Index:
    records: tuple[GreenSpace, ...]
    geometries: tuple
    tree: STRtree


_CACHE: OrderedDict[tuple[str, str, str], _Index] = OrderedDict()


def _index(service, dataset_id: str, manifest) -> _Index:
    # Published snapshots are immutable, so a small per-snapshot cache is safe.
    key = (str(service.root), dataset_id, manifest.snapshot_id)
    if key in _CACHE:
        _CACHE.move_to_end(key)
        return _CACHE[key]
    records = tuple(
        record for record in service._load_records(manifest) if isinstance(record, GreenSpace)
    )
    geometries = tuple(transform(_TO_METRIC.transform, shape(r.geometry)) for r in records)
    index = _Index(records, geometries, STRtree(geometries))
    _CACHE[key] = index
    while len(_CACHE) > 4:
        _CACHE.popitem(last=False)
    return index


def nearby_green_spaces(
    service,
    dataset_id: str,
    snapshot_id: str,
    lon: float,
    lat: float,
    radius_m: float = 500.0,
    feature_type: str | None = None,
    limit: int = 50,
) -> dict:
    config = service._config(dataset_id)
    if config.adapter != ADAPTER:
        raise ConnectorError("INVALID_FILTER", "Proximity is only available for green-space data")
    if not (BERLIN_BBOX[0] <= lon <= BERLIN_BBOX[2] and BERLIN_BBOX[1] <= lat <= BERLIN_BBOX[3]):
        raise ConnectorError("INVALID_FILTER", "Query point is outside the Berlin bounding box")
    if not 0 <= radius_m <= MAX_RADIUS_M:
        raise ConnectorError("INVALID_FILTER", f"radius_m must be between 0 and {MAX_RADIUS_M:g}")
    if feature_type not in {None, "green_space", "playground"}:
        raise ConnectorError("INVALID_FILTER", "feature_type must be green_space or playground")
    manifest = service.catalog.get(dataset_id, snapshot_id)
    index = _index(service, dataset_id, manifest)
    origin = Point(_TO_METRIC.transform(lon, lat))
    matches = []
    for position in index.tree.query(origin, predicate="dwithin", distance=radius_m):
        record = index.records[int(position)]
        if feature_type is not None and record.feature_type != feature_type:
            continue
        geometry = index.geometries[int(position)]
        matches.append((round(geometry.distance(origin), 1), record, geometry.contains(origin)))
    matches.sort(key=lambda item: (item[0], item[1].record_id))
    counts = {
        kind: sum(record.feature_type == kind for _, record, _ in matches)
        for kind in ("green_space", "playground")
        if feature_type in {None, kind}
    }
    return {
        "schema_version": "1.0",
        "dataset_id": dataset_id,
        "snapshot_id": snapshot_id,
        "retrieved_at": manifest.retrieved_at,
        "source_edition": manifest.source_edition,
        "origin": {"type": "Point", "coordinates": [lon, lat]},
        "radius_m": radius_m,
        "distance_method": DISTANCE_METHOD,
        "distance_note": DISTANCE_NOTE,
        "source_scope": (
            "Official Berlin inventory only; not matched or deduplicated against OSM features."
        ),
        "counts_within_radius": counts,
        "count": len(matches),
        "results": [
            {
                "record_id": record.record_id,
                "source_record_id": record.source_record_id,
                "feature_type": record.feature_type,
                "name": record.name,
                "area_code": record.area_code,
                "straight_line_distance_m": distance,
                "contains_origin": inside,
                "geometry_area_m2": record.geometry_area_m2,
                "display_point": record.display_point,
                "adjacent_record_ids": list(record.adjacent_record_ids),
            }
            for distance, record, inside in matches[:limit]
        ],
        "warnings": list(manifest.quality_report.warnings),
        "attribution": [
            {
                "dataset_id": dataset_id,
                "license_id": manifest.license_id,
                "license_url": manifest.license_url,
                "attribution": manifest.attribution,
                "landing_url": manifest.landing_url,
            }
        ],
    }
