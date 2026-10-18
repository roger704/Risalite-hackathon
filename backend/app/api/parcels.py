"""Read-only parcel lookup and geometric cross-snapshot relationships."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Annotated, Any, Callable

from fastapi import APIRouter, Depends, Query, Request
from shapely.geometry import box, shape

from app.application.service import DataService
from app.domain import ConnectorError, NotFoundError, Parcel, record_to_dict
from app.geo.parcels import (
    EXCLUDED_CLAIMS,
    PARCEL_INTERPRETATION_GUARD,
    ParcelIndex,
    relation_to_dict,
    summarize_by_related,
)

_INDEX_CACHE: dict[tuple[str, str], ParcelIndex] = {}
_MAX_RELATED_RECORDS = 200_000


def parcels_router(get_service: Callable[[], DataService]) -> APIRouter:
    router = APIRouter()

    def check_query(request: Request, allowed: set[str]) -> None:
        unknown = set(request.query_params) - allowed
        if unknown:
            raise ConnectorError(
                "UNSUPPORTED_FILTER", "Unsupported query filters: " + ", ".join(sorted(unknown))
            )

    def manifest_and_meta(service: DataService, snapshot_id: str):
        manifest = service.catalog.get("berlin_alkis_parcels", snapshot_id)
        if not service._snapshot_available(manifest):
            raise ConnectorError(
                "SOURCE_UNAVAILABLE", "Published parcel snapshot data is unavailable"
            )
        meta = {
            "dataset_id": manifest.dataset_id,
            "snapshot_id": manifest.snapshot_id,
            "retrieved_at": manifest.retrieved_at.isoformat(),
            "provenance": {
                "resource_url": manifest.resource_url,
                "landing_url": manifest.landing_url,
                "retrieved_at": manifest.retrieved_at.isoformat(),
                "license": manifest.license_id,
                "license_url": manifest.license_url,
                "attribution": manifest.attribution,
                "source_edition": manifest.source_edition,
                "raw_asset_hashes_count": len(manifest.raw_assets),
            },
            "quality_warnings": list(manifest.quality_report.warnings),
            "interpretation_guard": PARCEL_INTERPRETATION_GUARD,
            "excluded_claims": list(EXCLUDED_CLAIMS),
            "area_semantics": {
                "official_area_m2": "Source-provided ALKIS afl (Amtliche Fläche), if available.",
                "geometry_area_m2": (
                    "Derived planar area of the original source geometry in EPSG:25833; "
                    "not a replacement for official_area_m2."
                ),
            },
            "coverage": manifest.coverage,
        }
        return manifest, meta

    def index_for(service: DataService, snapshot_id: str):
        manifest, meta = manifest_and_meta(service, snapshot_id)
        key = (str(service.root), manifest.snapshot_id)
        index = _INDEX_CACHE.get(key)
        if index is None:
            records = []
            cursor = None
            while True:
                page = service.records(
                    "berlin_alkis_parcels",
                    snapshot_id,
                    limit=1000,
                    cursor=cursor,
                )
                records.extend(page["records"])
                cursor = page["next_cursor"]
                if cursor is None:
                    break
            index = ParcelIndex(records_from_dicts(records))
            if len(_INDEX_CACHE) >= 8:
                _INDEX_CACHE.pop(next(iter(_INDEX_CACHE)))
            _INDEX_CACHE[key] = index
        return index, manifest, meta

    @router.get("/api/v1/parcels/at")
    def at_point(
        request: Request,
        snapshot_id: Annotated[str, Query(min_length=1)],
        lon: float,
        lat: float,
        service: DataService = Depends(get_service),
    ):
        check_query(request, {"snapshot_id", "lon", "lat"})
        index, _, meta = index_for(service, snapshot_id)
        try:
            matches = index.at_point(lon, lat)
        except ValueError as exc:
            raise ConnectorError("INVALID_REQUEST", str(exc)) from exc
        return {
            "meta": meta,
            "matches": [
                {
                    "match_type": match.match_type,
                    "parcel": record_to_dict(match.parcel),
                }
                for match in matches
            ],
            "match_count": len(matches),
        }

    @router.get("/api/v1/parcels/in-bbox")
    def in_bbox(
        request: Request,
        snapshot_id: Annotated[str, Query(min_length=1)],
        bbox: Annotated[str, Query(min_length=1)],
        limit: Annotated[int, Query(ge=1, le=2000)] = 500,
        service: DataService = Depends(get_service),
    ):
        check_query(request, {"snapshot_id", "bbox", "limit"})
        try:
            values = tuple(float(value) for value in bbox.split(","))
            if len(values) != 4:
                raise ValueError
        except ValueError as exc:
            raise ConnectorError(
                "INVALID_REQUEST", "bbox must contain west,south,east,north"
            ) from exc
        index, _, meta = index_for(service, snapshot_id)
        try:
            matches = index.in_bbox(*values, limit=limit + 1)
        except ValueError as exc:
            raise ConnectorError("INVALID_REQUEST", str(exc)) from exc
        truncated = len(matches) > limit
        features = []
        for parcel in matches[:limit]:
            properties = record_to_dict(parcel)
            geometry = properties.pop("geometry")
            features.append(
                {
                    "type": "Feature",
                    "id": parcel.record_id,
                    "geometry": geometry,
                    "properties": properties,
                }
            )
        return {
            **meta,
            "type": "FeatureCollection",
            "features": features,
            "truncated": truncated,
        }

    @router.get("/api/v1/parcels/{parcel_id}")
    def detail(
        request: Request,
        parcel_id: str,
        snapshot_id: Annotated[str, Query(min_length=1)],
        service: DataService = Depends(get_service),
    ):
        check_query(request, {"snapshot_id"})
        index, _, meta = index_for(service, snapshot_id)
        parcel = index.get(parcel_id)
        if parcel is None:
            raise NotFoundError(f"Parcel not found: {parcel_id}")
        return {"meta": meta, "parcel": record_to_dict(parcel)}

    @router.get("/api/v1/parcels/{parcel_id}/relations")
    def relations(
        request: Request,
        parcel_id: str,
        snapshot_id: Annotated[str, Query(min_length=1)],
        related_dataset_id: Annotated[str, Query(min_length=1)],
        related_snapshot_id: Annotated[str, Query(min_length=1)],
        service: DataService = Depends(get_service),
    ):
        check_query(
            request,
            {"snapshot_id", "related_dataset_id", "related_snapshot_id"},
        )
        index, _, meta = index_for(service, snapshot_id)
        parcel = index.get(parcel_id)
        if parcel is None:
            raise NotFoundError(f"Parcel not found: {parcel_id}")
        related_manifest = service.catalog.get(related_dataset_id, related_snapshot_id)
        if related_manifest.coverage.get("record_count", 0) > _MAX_RELATED_RECORDS:
            raise ConnectorError(
                "INVALID_REQUEST",
                f"Related snapshot exceeds the {_MAX_RELATED_RECORDS}-record relationship cap",
            )

        west, south, east, north = shape(parcel.geometry).bounds
        parcel_bbox = box(west, south, east, north)
        related_records = []
        cursor = None
        while True:
            page = service.records(
                related_dataset_id,
                related_snapshot_id,
                limit=1000,
                cursor=cursor,
            )
            for value in page["records"]:
                geometry_data = value.get("geometry")
                if geometry_data is None:
                    continue
                try:
                    geometry = shape(geometry_data)
                except Exception:
                    continue
                if geometry.intersects(parcel_bbox):
                    related_records.append(
                        SimpleNamespace(record_id=value["record_id"], geometry=geometry_data)
                    )
                    if len(related_records) > _MAX_RELATED_RECORDS:
                        raise ConnectorError(
                            "INVALID_REQUEST",
                            f"Related snapshot exceeds the {_MAX_RELATED_RECORDS}-record relationship cap",
                        )
            cursor = page["next_cursor"]
            if cursor is None:
                break
        relation_rows, touches_only = ParcelIndex((parcel,)).relate_geometries(
            related_dataset_id, related_snapshot_id, related_records
        )
        related_meta = {
            "dataset_id": related_manifest.dataset_id,
            "snapshot_id": related_manifest.snapshot_id,
            "retrieved_at": related_manifest.retrieved_at.isoformat(),
            "source_edition": related_manifest.source_edition,
        }
        return {
            "meta": meta,
            "relations": [relation_to_dict(relation) for relation in relation_rows],
            "touches_only": touches_only,
            "summaries_by_related": summarize_by_related(relation_rows),
            "pinned_inputs": {
                "parcels": snapshot_id,
                "related": related_meta,
            },
        }

    return router


def records_from_dicts(records: list[dict[str, Any]]) -> tuple[Parcel, ...]:
    from app.domain import record_from_dict

    return tuple(record_from_dict(record) for record in records)
