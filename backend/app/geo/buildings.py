"""Spatial relationships for ALKIS buildings and compatible WGS84 parcel records."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from pyproj import Transformer
from shapely.geometry import Point, shape
from shapely.ops import transform
from shapely.strtree import STRtree

from app.domain import Building

_TO_METRIC = Transformer.from_crs("EPSG:4326", "EPSG:25833", always_xy=True)
_TO_METRIC_FN = _TO_METRIC.transform
_TO_WGS84 = Transformer.from_crs("EPSG:25833", "EPSG:4326", always_xy=True)
_TO_WGS84_FN = _TO_WGS84.transform


@dataclass(frozen=True)
class BuildingIndex:
    records: tuple[Building, ...]
    geometries: tuple
    tree: STRtree

    def __init__(self, records):
        buildings = tuple(record for record in records if isinstance(record, Building))
        geometries = tuple(transform(_TO_METRIC_FN, shape(record.geometry)) for record in buildings)
        object.__setattr__(self, "records", buildings)
        object.__setattr__(self, "geometries", geometries)
        object.__setattr__(self, "tree", STRtree(geometries) if geometries else None)


def normalize_street(value: str) -> str:
    """Normalize street labels compatibly with the separate helper in issue #47."""
    normalized = value.casefold().replace("ß", "ss")
    normalized = re.sub(r"str\.", "strasse", normalized)
    normalized = re.sub(r"\bstrasse\b", "strasse", normalized)
    return re.sub(r"[\s-]+", "", normalized).strip()


def _street_code_consistent(record: Building, street_code: str | None) -> bool | None:
    if street_code is None or not record.street_codes:
        return None
    return street_code in record.street_codes


def _metric_point(lon: float, lat: float) -> Point:
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
        raise ValueError("longitude and latitude must be finite WGS84 coordinates")
    return transform(_TO_METRIC_FN, Point(lon, lat))


def buildings_at_point(index: BuildingIndex, lon: float, lat: float, radius_m: float):
    if (
        isinstance(radius_m, bool)
        or not isinstance(radius_m, (int, float))
        or not math.isfinite(radius_m)
        or radius_m < 0
    ):
        raise ValueError("radius_m must be finite and nonnegative")
    point = _metric_point(lon, lat)
    if index.tree is None:
        return []
    candidate_indices = index.tree.query(point, predicate="dwithin", distance=radius_m)
    results = []
    for position in candidate_indices:
        geometry = index.geometries[int(position)]
        distance = geometry.distance(point)
        if geometry.contains(point):
            relation = "contains"
            distance = 0.0
        elif distance <= radius_m:
            relation = "nearby"
        else:
            continue
        results.append(
            (
                distance,
                index.records[int(position)].record_id,
                index.records[int(position)],
                relation,
            )
        )
    results.sort(key=lambda item: (item[0], item[1]))
    return [
        {"record": record, "relation": relation, "distance_m": round(distance, 2)}
        for distance, _, record, relation in results
    ]


def _street_number(label: str) -> tuple[str, str | None, str | None] | None:
    match = re.fullmatch(r"(?P<street>.+?)\s+(?P<number>\d+)(?P<suffix>[A-Za-z]*)", label.strip())
    if not match:
        return None
    return (
        normalize_street(match.group("street")),
        match.group("number"),
        match.group("suffix").upper() or None,
    )


def _address_number(number: str, suffix: str | None) -> tuple[str, str | None]:
    match = re.fullmatch(r"\s*(\d+)\s*([A-Za-z]*)\s*", number)
    if not match:
        raise ValueError("house_number must contain a numeric base and optional suffix")
    return match.group(1), (suffix or match.group(2) or "").strip().upper() or None


def address_to_buildings(
    index: BuildingIndex,
    street_name: str,
    house_number: str,
    house_number_suffix: str | None = None,
    street_code: str | None = None,
    lon: float | None = None,
    lat: float | None = None,
) -> dict:
    if not isinstance(street_name, str) or not street_name.strip():
        raise ValueError("street_name must be nonempty")
    if not isinstance(house_number, str) or not house_number.strip():
        raise ValueError("house_number must be nonempty")
    if (lon is None) != (lat is None):
        raise ValueError("lon and lat must be supplied together")
    base, suffix = _address_number(house_number, house_number_suffix)
    target_street = normalize_street(street_name)
    collected: dict[str, dict] = {}
    exact_suffix_ids = set()

    for record in index.records:
        matching_labels = []
        has_base = False
        has_exact = False
        for label in record.address_labels:
            parsed = _street_number(label)
            if parsed is None or parsed[0] != target_street or parsed[1] != base:
                continue
            if suffix and parsed[2] == suffix:
                has_exact = True
                matching_labels.append(label)
            elif not suffix and parsed[2] is None:
                has_exact = True
                matching_labels.append(label)
            elif suffix and parsed[2] is None:
                has_base = True
                matching_labels.append(label)
        if has_exact or has_base:
            collected[record.record_id] = {
                "record": record,
                "match_methods": {"address_reference"},
                "reference_kind": record.address_reference_kind,
                "street_code_consistent": _street_code_consistent(record, street_code),
                "base_number_only": bool(suffix and has_base and not has_exact),
            }
            if has_exact:
                exact_suffix_ids.add(record.record_id)

    contained = []
    if lon is not None:
        contained = [
            match
            for match in buildings_at_point(index, lon, lat, 0)
            if match["relation"] == "contains"
        ]
        for match in contained:
            record = match["record"]
            candidate = collected.setdefault(
                record.record_id,
                {
                    "record": record,
                    "match_methods": set(),
                    "reference_kind": record.address_reference_kind,
                    "street_code_consistent": _street_code_consistent(record, street_code),
                    "base_number_only": False,
                },
            )
            candidate["match_methods"].add("contains_address_point")

    if suffix and exact_suffix_ids:
        collected = {
            key: value
            for key, value in collected.items()
            if key in exact_suffix_ids or "contains_address_point" in value["match_methods"]
        }
    matches = [
        {
            "record_id": value["record"].record_id,
            "match_methods": sorted(value["match_methods"]),
            "reference_kind": value["reference_kind"],
            "street_code_consistent": value["street_code_consistent"],
            "base_number_only": value["base_number_only"],
            "record": value["record"],
        }
        for value in sorted(collected.values(), key=lambda item: item["record"].record_id)
    ]
    nearby = []
    if not matches and lon is not None:
        nearby = [
            match
            for match in buildings_at_point(index, lon, lat, 25)
            if match["relation"] in {"contains", "nearby"}
        ]
    return {
        "status": "unique" if len(matches) == 1 else "ambiguous" if matches else "none",
        "matches": matches,
        "nearby_candidates": nearby,
    }


def building_to_parcels(
    building,
    parcels,
    min_overlap_share: float = 0.01,
    min_overlap_m2: float = 0.5,
) -> dict:
    if (
        isinstance(min_overlap_share, bool)
        or not isinstance(min_overlap_share, (int, float))
        or not math.isfinite(min_overlap_share)
        or min_overlap_share < 0
        or isinstance(min_overlap_m2, bool)
        or not isinstance(min_overlap_m2, (int, float))
        or not math.isfinite(min_overlap_m2)
        or min_overlap_m2 < 0
    ):
        raise ValueError("overlap thresholds must be finite and nonnegative")
    building_geometry = transform(_TO_METRIC_FN, shape(building.geometry))
    footprint_area = building_geometry.area
    matches = []
    for parcel in parcels:
        parcel_geometry = transform(_TO_METRIC_FN, shape(parcel.geometry))
        overlap = building_geometry.intersection(parcel_geometry).area
        share = overlap / footprint_area if footprint_area else 0
        if overlap >= min_overlap_m2 and share >= min_overlap_share:
            matches.append(
                {
                    "record_id": parcel.record_id,
                    "overlap_m2": overlap,
                    "share_of_footprint": share,
                }
            )
    matches.sort(key=lambda item: (-item["share_of_footprint"], item["record_id"]))
    return {
        "status": "unique" if len(matches) == 1 else "multiple" if matches else "none",
        "matches": matches,
    }
