"""Read-only ALKIS building lookup routes backed by immutable local snapshots."""

from __future__ import annotations

import math
import threading
from collections import OrderedDict
from dataclasses import asdict
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from pyproj import Transformer
from shapely.geometry import box
from shapely.ops import transform

from app.application.service import _hash
from app.connectors.berlin_alkis_buildings import extent_from_resource_url
from app.domain import Building, ConnectorError, NotFoundError, record_to_dict
from app.geo.buildings import BuildingIndex, address_to_buildings, buildings_at_point
from app.infrastructure.storage import read_records

DATASET_ID = "berlin_alkis_buildings"
_CACHE: OrderedDict[tuple[str, str, int], BuildingIndex] = OrderedDict()
_CACHE_LOCK = threading.Lock()
_TO_METRIC = Transformer.from_crs("EPSG:4326", "EPSG:25833", always_xy=True)
_TO_WGS84 = Transformer.from_crs("EPSG:25833", "EPSG:4326", always_xy=True)


def _index_for_snapshot(service, snapshot_id: str):
    config = service._config(DATASET_ID)
    manifest = service.catalog.get(DATASET_ID, snapshot_id)
    path = service._snapshot_path(manifest)
    key = (snapshot_id, str(path), path.stat().st_mtime_ns)
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached is not None:
            _CACHE.move_to_end(key)
            return config, manifest, cached
        records = read_records(path)
        index = BuildingIndex(record for record in records if isinstance(record, Building))
        for stale_key in [candidate for candidate in _CACHE if candidate[0] == snapshot_id]:
            del _CACHE[stale_key]
        _CACHE[key] = index
        while len(_CACHE) > 2:
            _CACHE.popitem(last=False)
    return config, manifest, index


def _extent(config, manifest) -> dict[str, Any] | None:
    asset_extents = [extent_from_resource_url(asset.resource_url) for asset in manifest.raw_assets]
    if (
        not asset_extents
        or asset_extents[0] is None
        or any(extent != asset_extents[0] for extent in asset_extents[1:])
    ):
        return None
    minx, miny, maxx, maxy = asset_extents[0]
    polygon = transform(_TO_WGS84.transform, box(minx, miny, maxx, maxy))
    config_matches_snapshot = manifest.parser_config_hash == _hash(asdict(config))
    configured_extent = config.parser_options.get("extent", {})
    configured_bbox = configured_extent.get("bbox_25833")
    name = (
        configured_extent.get("name")
        if config_matches_snapshot
        and isinstance(configured_bbox, (list, tuple))
        and tuple(float(value) for value in configured_bbox) == asset_extents[0]
        else None
    )
    return {
        "name": name,
        "bbox_25833": list(asset_extents[0]),
        "bbox": list(polygon.bounds),
        "complete": manifest.quality_report.valid,
    }


def _metadata(config, manifest) -> dict[str, Any]:
    config_matches_snapshot = manifest.parser_config_hash == _hash(asdict(config))
    warnings = list(manifest.quality_report.warnings)

    def snapshot_value(name: str):
        value = getattr(manifest, name, None)
        if value is not None or hasattr(manifest, name):
            return value
        if config_matches_snapshot:
            return getattr(config, name, None)
        warnings.append("metadata reflects current config because snapshot metadata is unavailable")
        return getattr(config, name, None)

    extent = _extent(config, manifest)
    complete = extent is not None and manifest.quality_report.valid
    if extent is None:
        warnings.append("extent of this snapshot cannot be reconstructed")
    return {
        "dataset_id": DATASET_ID,
        "snapshot_id": manifest.snapshot_id,
        "retrieved_at": manifest.retrieved_at,
        "source_edition": snapshot_value("source_edition"),
        "license_id": snapshot_value("license_id"),
        "license_url": snapshot_value("license_url"),
        "attribution": snapshot_value("attribution"),
        "landing_url": snapshot_value("landing_url"),
        "extent_coverage": extent,
        "complete": complete,
        "warnings": warnings,
        "notice": (
            "ALKIS function is not current use, tenant, vacancy, or permitted use. "
            "Footprint area multiplied by storeys is not usable or rentable floor area."
        ),
        "method": {"metric_crs": "EPSG:25833", "distance": "straight-line"},
    }


def _validate_lon_lat(lon: float, lat: float, allowed_bbox: list[float]) -> None:
    if (
        not math.isfinite(lon)
        or not math.isfinite(lat)
        or not allowed_bbox[0] <= lon <= allowed_bbox[2]
        or not allowed_bbox[1] <= lat <= allowed_bbox[3]
    ):
        raise ConnectorError("INVALID_FILTER", "lon and lat must fall within the Berlin bbox")


def _point_inside_extent(lon: float, lat: float, extent: dict | None) -> bool:
    if extent is None:
        return False
    minx, miny, maxx, maxy = extent["bbox_25833"]
    x, y = _TO_METRIC.transform(lon, lat)
    return minx <= x <= maxx and miny <= y <= maxy


def _radius_inside_extent(lon: float, lat: float, radius_m: float, extent: dict | None) -> bool:
    if extent is None:
        return False
    minx, miny, maxx, maxy = extent["bbox_25833"]
    x, y = _TO_METRIC.transform(lon, lat)
    return (
        x - radius_m >= minx
        and x + radius_m <= maxx
        and y - radius_m >= miny
        and y + radius_m <= maxy
    )


def _check_unknown(request: Request, allowed: set[str]) -> None:
    unknown = set(request.query_params) - allowed
    if unknown:
        raise ConnectorError(
            "UNSUPPORTED_FILTER", "Unsupported query parameters: " + ", ".join(sorted(unknown))
        )


def create_buildings_router(get_service) -> APIRouter:
    router = APIRouter()

    @router.get(f"/api/v1/datasets/{DATASET_ID}/at")
    def at(
        request: Request,
        snapshot_id: Annotated[str, Query(min_length=1)],
        lon: float,
        lat: float,
        radius_m: Annotated[float, Query(ge=0, le=100)] = 0,
    ):
        _check_unknown(request, {"snapshot_id", "lon", "lat", "radius_m"})
        service = get_service()
        config, manifest, index = _index_for_snapshot(service, snapshot_id)
        _validate_lon_lat(lon, lat, config.parser_options["allowed_bbox"])
        matches = buildings_at_point(index, lon, lat, radius_m)
        response = _metadata(config, manifest)
        extent = response["extent_coverage"]
        if extent is not None and not _point_inside_extent(lon, lat, extent):
            response["warnings"].append(
                "outside imported extent; absence of a building is not evidence"
            )
        response["coverage_partial"] = not _radius_inside_extent(lon, lat, radius_m, extent)
        if response["coverage_partial"] and extent is not None:
            response["warnings"].append(
                "search radius extends beyond imported extent; buildings outside it are missing"
            )
        response.update(
            {
                "records": [record_to_dict(item["record"]) for item in matches],
                "matches": [
                    {
                        "record_id": item["record"].record_id,
                        "relation": item["relation"],
                        "distance_m": item["distance_m"],
                    }
                    for item in matches
                ],
            }
        )
        return response

    @router.get(f"/api/v1/datasets/{DATASET_ID}/buildings/{{record_id}}")
    def by_id(request: Request, record_id: str, snapshot_id: Annotated[str, Query(min_length=1)]):
        _check_unknown(request, {"snapshot_id"})
        service = get_service()
        config, manifest, index = _index_for_snapshot(service, snapshot_id)
        record = next((item for item in index.records if item.record_id == record_id), None)
        if record is None:
            raise NotFoundError(f"Building not found in snapshot: {record_id}")
        response = _metadata(config, manifest)
        response["building"] = record_to_dict(record)
        return response

    @router.get(f"/api/v1/datasets/{DATASET_ID}/by-address")
    def by_address(
        request: Request,
        snapshot_id: Annotated[str, Query(min_length=1)],
        street: Annotated[str, Query(min_length=1)],
        house_number: Annotated[str, Query(min_length=1)],
        house_number_suffix: str | None = None,
        street_code: str | None = None,
        lon: float | None = None,
        lat: float | None = None,
    ):
        _check_unknown(
            request,
            {
                "snapshot_id",
                "street",
                "house_number",
                "house_number_suffix",
                "street_code",
                "lon",
                "lat",
            },
        )
        if (lon is None) != (lat is None):
            raise ConnectorError("INVALID_FILTER", "lon and lat must be supplied together")
        service = get_service()
        config, manifest, index = _index_for_snapshot(service, snapshot_id)
        if lon is not None:
            _validate_lon_lat(lon, lat, config.parser_options["allowed_bbox"])
        try:
            result = address_to_buildings(
                index,
                street,
                house_number,
                house_number_suffix=house_number_suffix,
                street_code=street_code,
                lon=lon,
                lat=lat,
            )
        except ValueError as exc:
            raise ConnectorError("INVALID_FILTER", str(exc)) from exc
        response = _metadata(config, manifest)
        if lon is not None:
            extent = response["extent_coverage"]
            response["coverage_partial"] = not _point_inside_extent(lon, lat, extent)
            if extent is not None and response["coverage_partial"]:
                response["warnings"].append(
                    "outside imported extent; absence of a building is not evidence"
                )
        own_address_match_count = sum(
            item["reference_kind"] == "house_number"
            and "address_reference" in item["match_methods"]
            for item in result["matches"]
        )
        pseudonumber_match_count = sum(
            item["reference_kind"] == "pseudonumber"
            and "address_reference" in item["match_methods"]
            for item in result["matches"]
        )
        response.update(
            {
                "status": result["status"],
                "own_address_match_count": own_address_match_count,
                "pseudonumber_match_count": pseudonumber_match_count,
                "records": [record_to_dict(item["record"]) for item in result["matches"]],
                "matches": [
                    {
                        **{key: value for key, value in item.items() if key != "record"},
                        "building": record_to_dict(item["record"]),
                    }
                    for item in result["matches"]
                ],
                "nearby_candidates": [
                    {
                        "record_id": item["record"].record_id,
                        "relation": item["relation"],
                        "distance_m": item["distance_m"],
                        "building": record_to_dict(item["record"]),
                    }
                    for item in result["nearby_candidates"]
                ],
            }
        )
        return response

    return router
