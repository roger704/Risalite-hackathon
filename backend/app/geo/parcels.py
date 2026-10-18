"""Spatial parcel lookup and provenance-bearing cross-layer relationships."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

from pyproj import Transformer
from shapely.geometry import Point, box, shape
from shapely.ops import transform
from shapely.strtree import STRtree

from app.domain import Parcel

PARCEL_INTERPRETATION_GUARD = (
    "Parcel geometry and planning intersections describe official cadastral boundaries "
    "and recorded planning procedures only. They do not imply development permission, "
    "available premises, commercial tenancy, or an offer to acquire the property."
)
EXCLUDED_CLAIMS = (
    "development_permission",
    "available_premises",
    "commercial_tenancy",
    "acquisition_offer",
)
AREA_CRS = "EPSG:25833"
BOUNDARY_TOLERANCE_M = 0.05
RELATIONSHIP_METHOD = (
    "Geometric intersection of WGS84 GeoJSON reprojected with always_xy to EPSG:25833; "
    "no legal assignment is inferred."
)


@dataclass(frozen=True)
class ParcelPointMatch:
    match_type: str
    parcel: Parcel


@dataclass(frozen=True)
class ParcelRelation:
    parcel_record_id: str
    parcel_id: str
    related_dataset_id: str
    related_snapshot_id: str
    related_record_id: str
    relation: str
    intersection_area_m2: float | None = None
    parcel_area_share: float | None = None
    related_area_share: float | None = None
    method: str = RELATIONSHIP_METHOD
    crs: str = AREA_CRS


def summarize_by_related(relations: list[ParcelRelation]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, set[str]] = {}
    for relation in relations:
        grouped.setdefault(relation.related_record_id, set()).add(relation.parcel_id)
    return {
        record_id: {
            "parcel_ids": sorted(parcel_ids),
            "multiple_parcels": len(parcel_ids) > 1,
        }
        for record_id, parcel_ids in sorted(grouped.items())
    }


class ParcelIndex:
    def __init__(self, parcels):
        self.parcels = tuple(parcels)
        if any(not isinstance(parcel, Parcel) for parcel in self.parcels):
            raise TypeError("ParcelIndex accepts Parcel records only")
        self._by_id = {parcel.parcel_id: parcel for parcel in self.parcels}
        self._by_record_id = {parcel.record_id: parcel for parcel in self.parcels}
        self._to_metric = Transformer.from_crs("EPSG:4326", AREA_CRS, always_xy=True)
        self._to_wgs84 = Transformer.from_crs(AREA_CRS, "EPSG:4326", always_xy=True)
        self._metric_geometries = tuple(
            transform(self._to_metric.transform, shape(parcel.geometry)) for parcel in self.parcels
        )
        self._metric_by_id = {
            parcel.parcel_id: geometry
            for parcel, geometry in zip(self.parcels, self._metric_geometries, strict=True)
        }
        self._tree = STRtree(self._metric_geometries) if self._metric_geometries else None

    def get(self, parcel_id: str) -> Parcel | None:
        return self._by_id.get(parcel_id)

    def get_record(self, record_id: str) -> Parcel | None:
        return self._by_record_id.get(record_id)

    def prefilter_geometry_intersects_parcel_envelope(self, parcel_id: str, geometry) -> bool:
        parcel_geometry = self._metric_by_id.get(parcel_id)
        if parcel_geometry is None or geometry.is_empty:
            return False
        metric_geometry = transform(self._to_metric.transform, geometry)
        search_envelope = parcel_geometry.envelope.buffer(BOUNDARY_TOLERANCE_M)
        return metric_geometry.intersects(search_envelope)

    def at_point(
        self, lon: float, lat: float, boundary_tolerance_m: float = BOUNDARY_TOLERANCE_M
    ) -> list[ParcelPointMatch]:
        if (
            isinstance(lon, bool)
            or isinstance(lat, bool)
            or not isinstance(lon, (int, float))
            or not isinstance(lat, (int, float))
            or not math.isfinite(lon)
            or not math.isfinite(lat)
            or not -180 <= lon <= 180
            or not -90 <= lat <= 90
        ):
            raise ValueError("point coordinates must be finite WGS84 longitude/latitude")
        if (
            isinstance(boundary_tolerance_m, bool)
            or not isinstance(boundary_tolerance_m, (int, float))
            or not math.isfinite(boundary_tolerance_m)
            or boundary_tolerance_m < 0
        ):
            raise ValueError("boundary_tolerance_m must be a finite nonnegative number")
        if self._tree is None:
            return []
        point = transform(self._to_metric.transform, Point(float(lon), float(lat)))
        matches = []
        candidates = point.buffer(boundary_tolerance_m) if boundary_tolerance_m else point
        for index in self._tree.query(candidates):
            parcel, geometry = self.parcels[int(index)], self._metric_geometries[int(index)]
            boundary_distance = geometry.boundary.distance(point)
            if boundary_distance <= boundary_tolerance_m:
                matches.append(ParcelPointMatch("boundary", parcel))
            elif geometry.contains(point):
                matches.append(ParcelPointMatch("interior", parcel))
        return sorted(matches, key=lambda match: match.parcel.parcel_id)

    def in_bbox(self, west: float, south: float, east: float, north: float, limit: int):
        values = (west, south, east, north)
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            for value in values
        ):
            raise ValueError("bbox coordinates must be finite numbers")
        if west > east or south > north:
            raise ValueError("bbox must be ordered west,south,east,north")
        if not -180 <= west <= east <= 180 or not -90 <= south <= north <= 90:
            raise ValueError("bbox must use WGS84 longitude/latitude ranges")
        if (east - west) * (north - south) > 0.05:
            raise ValueError("bbox exceeds the 0.05 square-degree map-area limit")
        if type(limit) is not int or limit < 1:
            raise ValueError("limit must be a positive integer")
        if self._tree is None:
            return []
        metric_bbox = transform(self._to_metric.transform, box(west, south, east, north))
        indices = sorted(
            {int(index) for index in self._tree.query(metric_bbox)},
            key=lambda index: self.parcels[index].parcel_id,
        )
        return [
            self.parcels[index]
            for index in indices
            if self._metric_geometries[index].intersects(metric_bbox)
        ][:limit]

    def relate_geometries(
        self,
        dataset_id: str,
        snapshot_id: str,
        records,
        *,
        touch_parcel_id: str | None = None,
    ):
        relations: list[ParcelRelation] = []
        touches_only = 0
        if self._tree is None:
            return relations, touches_only
        for record in records:
            if not isinstance(getattr(record, "record_id", None), str):
                raise ValueError("related records must expose a string record_id")
            geometry_data = getattr(record, "geometry", None)
            try:
                related_wgs84 = shape(geometry_data)
            except Exception as exc:
                raise ValueError(
                    f"related record has invalid GeoJSON geometry: {record.record_id}"
                ) from exc
            if related_wgs84.is_empty:
                continue
            related = transform(self._to_metric.transform, related_wgs84)
            candidate_indices = self._tree.query(related)
            if related.geom_type == "Point":
                for match in self.at_point(related_wgs84.x, related_wgs84.y):
                    relations.append(
                        ParcelRelation(
                            parcel_record_id=match.parcel.record_id,
                            parcel_id=match.parcel.parcel_id,
                            related_dataset_id=dataset_id,
                            related_snapshot_id=snapshot_id,
                            related_record_id=record.record_id,
                            relation="contains_point"
                            if match.match_type == "interior"
                            else "on_boundary",
                        )
                    )
                continue
            if related.geom_type not in {"Polygon", "MultiPolygon"}:
                continue
            for index in candidate_indices:
                parcel = self.parcels[int(index)]
                parcel_geometry = self._metric_geometries[int(index)]
                if not parcel_geometry.intersects(related):
                    continue
                intersection = parcel_geometry.intersection(related)
                area = intersection.area
                if area <= 0:
                    if touch_parcel_id is None or parcel.parcel_id == touch_parcel_id:
                        touches_only += 1
                    continue
                relations.append(
                    ParcelRelation(
                        parcel_record_id=parcel.record_id,
                        parcel_id=parcel.parcel_id,
                        related_dataset_id=dataset_id,
                        related_snapshot_id=snapshot_id,
                        related_record_id=record.record_id,
                        relation="intersects",
                        intersection_area_m2=round(area, 2),
                        parcel_area_share=round(area / parcel_geometry.area, 8)
                        if parcel_geometry.area > 0
                        else None,
                        related_area_share=round(area / related.area, 8)
                        if related.area > 0
                        else None,
                    )
                )
        relations.sort(key=lambda relation: (relation.related_record_id, relation.parcel_id))
        return relations, touches_only


def relation_to_dict(relation: ParcelRelation) -> dict[str, Any]:
    return asdict(relation)
