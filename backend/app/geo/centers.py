"""Metric proximity to Berlin StEP Zentren 2040 planning designations."""

from math import isfinite
from typing import Iterable

from pyproj import Transformer
from shapely.geometry import Point, shape
from shapely.ops import transform

from app.domain import RetailCenter

_TO_METRIC = Transformer.from_crs("EPSG:4326", "EPSG:25833", always_xy=True)
_TO_METRIC_FN = _TO_METRIC.transform


def center_relationships(
    centers: Iterable[RetailCenter], lon: float, lat: float, radius_m: float
) -> list[dict]:
    if any(
        isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value)
        for value in (lon, lat, radius_m)
    ):
        raise ValueError("Coordinates and radius must be finite numbers")
    if not -180 <= lon <= 180 or not -90 <= lat <= 90:
        raise ValueError("Coordinates must be WGS84 longitude/latitude")
    if not 0 < radius_m <= 5000:
        raise ValueError("radius_m must be in (0, 5000]")
    candidate = transform(_TO_METRIC_FN, Point(lon, lat))
    nearby = []
    for center in centers:
        geometry = transform(_TO_METRIC_FN, shape(center.geometry))
        if geometry.is_empty or not geometry.is_valid:
            raise ValueError(f"Invalid retail-center geometry: {center.record_id}")
        if geometry.geom_type in {"Polygon", "MultiPolygon"}:
            relation = "inside_boundary" if geometry.covers(candidate) else "outside_boundary"
            distance_basis = "nearest_point_of_published_area"
        elif geometry.geom_type == "Point":
            relation = "point_only_no_boundary"
            distance_basis = "published_center_point"
        else:
            raise ValueError(f"Unsupported retail-center geometry: {center.record_id}")
        distance = geometry.distance(candidate)
        if distance <= radius_m:
            nearby.append(
                {
                    "record_id": center.record_id,
                    "source_record_id": center.source_record_id,
                    "name": center.name,
                    "center_type": center.center_type,
                    "center_category": center.center_category,
                    "center_role": center.center_role,
                    "hierarchy_level": center.hierarchy_level,
                    "layer": center.layer,
                    "geometry_type": geometry.geom_type,
                    "relation": relation,
                    "distance_m": round(distance, 1),
                    "distance_basis": distance_basis,
                    "parent_center_id": center.parent_center_id,
                    "source_url": center.source_url,
                }
            )
    return sorted(nearby, key=lambda item: (item["distance_m"], item["record_id"]))
