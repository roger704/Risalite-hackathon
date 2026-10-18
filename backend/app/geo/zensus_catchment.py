"""Spatial aggregation of census grid cells for arbitrary metric catchments."""

import math
from collections import Counter
from typing import Iterable

from pyproj import Transformer
from shapely.geometry import Point, box
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform

from app.domain import GridObservation
from app.geo.inspire_grid import cell_polygon, parse_grid_id
from app.infrastructure.storage import read_records

DEFINITION = (
    "Zensus 2022 census population at the 2022-05-15 reference date "
    "(CKM-perturbed); not current population and not Einwohnerregister residents"
)


def circle_catchment(lon: float, lat: float, radius_m: float):
    """Build a straight-line radius, not walking time, in EPSG:25833."""
    if (
        isinstance(lon, bool)
        or not isinstance(lon, (int, float))
        or not math.isfinite(lon)
        or not -180 <= lon <= 180
        or isinstance(lat, bool)
        or not isinstance(lat, (int, float))
        or not math.isfinite(lat)
        or not -90 <= lat <= 90
    ):
        raise ValueError("lon and lat must be finite WGS84 coordinates")
    if (
        isinstance(radius_m, bool)
        or not isinstance(radius_m, (int, float))
        or not math.isfinite(radius_m)
        or radius_m <= 0
    ):
        raise ValueError("radius_m must be finite and greater than zero")
    transformer = Transformer.from_crs("EPSG:4326", "EPSG:25833", always_xy=True)
    x, y = transformer.transform(lon, lat)
    return Point(x, y).buffer(radius_m, quad_segs=64)


def aggregate_grid(cells: Iterable[GridObservation], catchment: BaseGeometry) -> dict:
    records = tuple(cells)
    if any(not isinstance(record, GridObservation) for record in records):
        raise ValueError(
            "Census grid and register-based area data must not be mixed in one aggregation"
        )
    if not records:
        raise ValueError("At least one census grid cell is required")
    if catchment.is_empty or not catchment.is_valid or catchment.area <= 0:
        raise ValueError("catchment must be nonempty and valid")
    seen_grid_ids: set[str] = set()
    for record in records:
        if record.grid_id in seen_grid_ids:
            raise ValueError(f"duplicate grid_id {record.grid_id}")
        seen_grid_ids.add(record.grid_id)
    for name in (
        "source_id",
        "snapshot_id",
        "metric_code",
        "reference_period",
        "resolution_m",
        "grid_crs",
        "unit",
    ):
        if len({getattr(record, name) for record in records}) != 1:
            raise ValueError(f"census grid cells must share one {name}")
    first = records[0]
    intersecting = 0
    within = 0
    partial = 0
    without_value = 0
    without_value_by_status: Counter[str] = Counter()
    within_sum: int | float = 0
    intersecting_sum: int | float = 0
    centroid_in_sum: int | float = 0
    area_weighted_sum = 0.0
    published_area = 0.0
    valueless_area = 0.0

    for record in records:
        polygon = cell_polygon(parse_grid_id(record.grid_id), "EPSG:25833")
        intersection = polygon.intersection(catchment)
        if intersection.area <= 0:
            continue
        intersecting += 1
        fraction = intersection.area / polygon.area
        is_within = catchment.covers(polygon)
        if is_within:
            within += 1
        else:
            partial += 1
        if record.value is None:
            without_value += 1
            status = getattr(record.value_status, "value", record.value_status)
            without_value_by_status[str(status)] += 1
            valueless_area += intersection.area
            continue
        published_area += intersection.area
        intersecting_sum += record.value
        if is_within:
            within_sum += record.value
        if catchment.covers(polygon.centroid):
            centroid_in_sum += record.value
        area_weighted_sum += record.value * fraction

    catchment_area = catchment.area
    complete = without_value == 0
    warnings = [
        "Values are perturbed by the cell-key method (CKM); sums need not match official totals.",
        "Cells absent from the file have no published value; absence is not zero.",
        "Area-weighted estimates assume uniform density within each cell.",
        "Values describe the historical reference date 2022-05-15, not current population.",
    ]
    if not complete:
        statuses = ", ".join(sorted(without_value_by_status))
        warnings.append(
            f"{without_value} intersecting cells have no published value (statuses {statuses}); "
            "upper bound unavailable and estimate covers known cells only"
        )
    return {
        "source_id": first.source_id,
        "snapshot_id": first.snapshot_id,
        "metric_code": first.metric_code,
        "unit": first.unit,
        "reference_period": first.reference_period,
        "grid_crs": first.grid_crs,
        "resolution_m": first.resolution_m,
        "definition": DEFINITION,
        "catchment_area_m2": round(catchment_area, 1),
        "cells": {
            "intersecting": intersecting,
            "within": within,
            "partial": partial,
            "without_value": without_value,
            "without_value_by_status": dict(without_value_by_status),
        },
        "whole_cells": {
            "within_sum": within_sum,
            "intersecting_sum": intersecting_sum,
            "centroid_in_sum": centroid_in_sum,
            "value_status": "observed",
            "complete": complete,
            "spatial_unit": "union of whole grid cells, not the catchment",
        },
        "intersected_estimate": {
            "value": round(area_weighted_sum, 1),
            "value_status": "estimated",
            "method": "areal_weighting_uniform_density",
        },
        "bounds": {
            "lower": within_sum,
            "upper": intersecting_sum if complete else None,
            "complete": complete,
            "meaning": "sum of fully contained vs all intersecting published cells",
        },
        "coverage": {
            "published_cell_area_in_catchment_m2": round(published_area, 1),
            "share_of_catchment": round(published_area / catchment_area, 4),
            "valueless_cell_area_in_catchment_m2": round(valueless_area, 1),
            "meaning": (
                "share of the catchment covered by cells with published values; valueless "
                "cell area is reported separately"
            ),
        },
        "warnings": warnings,
    }


def snapshot_catchment(
    service, dataset_id: str, snapshot_id: str, lon: float, lat: float, radius_m: float
) -> dict:
    manifest = service.catalog.get(dataset_id, snapshot_id)
    if len(manifest.normalized_assets) != 1:
        raise ValueError("Snapshot must contain exactly one normalized asset")
    extract_extent = manifest.coverage.get("extract_extent")
    if (
        not isinstance(extract_extent, dict)
        or extract_extent.get("crs") != "EPSG:3035"
        or extract_extent.get("rule") != "cells intersecting bbox"
        or not _valid_bbox(extract_extent.get("bbox"))
    ):
        raise ValueError("snapshot has no recorded extract extent; re-import with adapter 1.1.0")
    bbox = tuple(extract_extent["bbox"])
    catchment = circle_catchment(lon, lat, radius_m)
    to_3035 = Transformer.from_crs("EPSG:25833", "EPSG:3035", always_xy=True)
    catchment_3035 = transform(to_3035.transform, catchment)
    if not box(*bbox).covers(catchment_3035):
        raise ValueError(
            "catchment extends beyond the imported extract extent "
            f"{list(bbox)} (EPSG:3035); results would undercount"
        )
    path = service.root / manifest.normalized_assets[0]
    records = read_records(path)
    result = aggregate_grid(
        (record for record in records if isinstance(record, GridObservation)),
        catchment,
    )
    result.update(
        {
            "snapshot_id": snapshot_id,
            "dataset_id": dataset_id,
            "source_edition": manifest.source_edition,
            "extract_extent": extract_extent,
            "attribution": manifest.attribution,
            "license_id": manifest.license_id,
            "license_url": manifest.license_url,
            "landing_url": manifest.landing_url,
            "catchment": {
                "type": "circle",
                "lon": lon,
                "lat": lat,
                "radius_m": radius_m,
                "crs": "EPSG:25833",
                "distance": "straight-line, not walking time",
            },
        }
    )
    return result


def _valid_bbox(value) -> bool:
    return (
        isinstance(value, (list, tuple))
        and len(value) == 4
        and all(
            isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(item)
            for item in value
        )
        and value[0] < value[2]
        and value[1] < value[3]
    )
