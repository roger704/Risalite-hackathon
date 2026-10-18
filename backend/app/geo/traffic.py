"""Metric proximity and area summaries for road-traffic segments."""

from dataclasses import dataclass
from math import isfinite
from typing import Iterable

from pyproj import Transformer
from shapely.geometry import Point, shape
from shapely.ops import transform
from shapely.strtree import STRtree

from app.domain import Area, RoadTrafficSegment

_TO_METRIC = Transformer.from_crs("EPSG:4326", "EPSG:25833", always_xy=True)
_TO_METRIC_FN = _TO_METRIC.transform


@dataclass(frozen=True)
class NearbyRoadSegment:
    record_id: str
    name: str | None
    straight_line_distance_m: float
    dtv_motor_vehicles_per_day: int | None
    reference_period: str


@dataclass(frozen=True)
class AreaTrafficSummary:
    area_code: str
    reference_period: str | None
    segment_count: int
    segments_with_volume: int
    intersecting_length_m: float
    length_with_volume_m: float
    max_dtv_motor_vehicles_per_day: int | None
    length_weighted_dtv_motor_vehicles_per_day: float | None


def _metric_line(segment: RoadTrafficSegment):
    geometry = transform(_TO_METRIC_FN, shape(segment.geometry))
    if (
        geometry.is_empty
        or not geometry.is_valid
        or geometry.geom_type not in {"LineString", "MultiLineString"}
    ):
        raise ValueError(f"Invalid road-segment geometry: {segment.record_id}")
    return geometry


def road_segments_near_point(
    segments: Iterable[RoadTrafficSegment], lon: float, lat: float, radius_m: float
) -> list[NearbyRoadSegment]:
    if any(
        isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value)
        for value in (lon, lat, radius_m)
    ):
        raise ValueError("Coordinates and radius must be finite numbers")
    if not -180 <= lon <= 180 or not -90 <= lat <= 90:
        raise ValueError("Coordinates must be WGS84 longitude/latitude")
    if radius_m < 0:
        raise ValueError("radius_m must be nonnegative")
    point = transform(_TO_METRIC_FN, Point(lon, lat))
    nearby = []
    for segment in segments:
        distance = point.distance(_metric_line(segment))
        if distance <= radius_m:
            nearby.append(
                NearbyRoadSegment(
                    record_id=segment.record_id,
                    name=segment.name,
                    straight_line_distance_m=distance,
                    dtv_motor_vehicles_per_day=segment.dtv_motor_vehicles_per_day,
                    reference_period=segment.reference_period,
                )
            )
    return sorted(nearby, key=lambda item: (item.straight_line_distance_m, item.record_id))


def summarize_road_traffic_by_area(
    segments: Iterable[RoadTrafficSegment], areas: Iterable[Area]
) -> dict[str, AreaTrafficSummary]:
    segment_list = list(segments)
    periods = {segment.reference_period for segment in segment_list}
    if len(periods) > 1:
        raise ValueError("Road segments have mixed reference periods")
    reference_period = next(iter(periods), None)

    area_list = list(areas)
    area_codes = [area.area_code for area in area_list]
    if len(set(area_codes)) != len(area_codes):
        raise ValueError("Area codes must be unique")
    metric_areas = []
    for area in area_list:
        geometry = transform(_TO_METRIC_FN, shape(area.geometry))
        if (
            geometry.is_empty
            or not geometry.is_valid
            or geometry.geom_type
            not in {
                "Polygon",
                "MultiPolygon",
            }
        ):
            raise ValueError(f"Invalid area geometry: {area.area_code}")
        metric_areas.append(geometry)

    metric_segments = [_metric_line(segment) for segment in segment_list]
    tree = STRtree(metric_segments) if metric_segments else None
    summaries = {}
    for area, area_geometry in zip(area_list, metric_areas, strict=True):
        segment_count = 0
        volume_count = 0
        total_length = 0.0
        volume_length = 0.0
        weighted_volume = 0.0
        max_volume = None
        if tree is not None:
            for index in tree.query(area_geometry):
                segment = segment_list[int(index)]
                clipped_length = metric_segments[int(index)].intersection(area_geometry).length
                if clipped_length <= 0:
                    continue
                segment_count += 1
                total_length += clipped_length
                volume = segment.dtv_motor_vehicles_per_day
                if volume is not None:
                    volume_count += 1
                    volume_length += clipped_length
                    weighted_volume += volume * clipped_length
                    max_volume = volume if max_volume is None else max(max_volume, volume)
        summaries[area.area_code] = AreaTrafficSummary(
            area_code=area.area_code,
            reference_period=reference_period,
            segment_count=segment_count,
            segments_with_volume=volume_count,
            intersecting_length_m=total_length,
            length_with_volume_m=volume_length,
            max_dtv_motor_vehicles_per_day=max_volume,
            length_weighted_dtv_motor_vehicles_per_day=(
                weighted_volume / volume_length if volume_length else None
            ),
        )
    return summaries
