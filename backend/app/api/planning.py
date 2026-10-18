"""Read-only development-plan spatial query endpoints."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pyproj import Transformer

from app.domain import ConnectorError, DevelopmentPlan, NotFoundError, record_from_dict
from app.geo.development_plans import (
    EXCLUDED_CLAIMS,
    PLAN_INTERPRETATION_GUARD,
    DevelopmentPlanIndex,
)

DATASET_ID = "berlin_planning"
DISTANCE_SEMANTICS = "straight-line metres in EPSG:25833, not walking time"


@lru_cache(maxsize=4)
def _plan_index(service, snapshot_id: str) -> DevelopmentPlanIndex:
    records = []
    cursor = None
    while True:
        page = service.records(DATASET_ID, snapshot_id, limit=1000, cursor=cursor)
        records.extend(
            item if isinstance(item, DevelopmentPlan) else record_from_dict(item)
            for item in page["records"]
        )
        cursor = page.get("next_cursor")
        if cursor is None:
            break
    return DevelopmentPlanIndex(item for item in records if isinstance(item, DevelopmentPlan))


def _manifest(service, snapshot_id: str):
    manifest = service.manifest(DATASET_ID, snapshot_id)
    return manifest.model_dump(mode="json") if hasattr(manifest, "model_dump") else manifest


def _warnings(manifest: dict) -> list[str]:
    warnings = list(manifest.get("quality_report", {}).get("warnings", ()))
    warnings.extend(manifest.get("coverage", {}).get("warnings", ()))
    return warnings


def _response_meta(snapshot_id: str, manifest: dict) -> dict:
    return {
        "snapshot_id": snapshot_id,
        "provenance": {
            "source_url": manifest.get("resource_url"),
            "landing_url": manifest.get("landing_url"),
            "retrieved_at": manifest.get("retrieved_at"),
            "license_id": manifest.get("license_id"),
            "license_url": manifest.get("license_url"),
            "attribution": manifest.get("attribution"),
        },
        "quality": {"warnings": _warnings(manifest)},
        "interpretation_guard": PLAN_INTERPRETATION_GUARD,
        "excluded_claims": EXCLUDED_CLAIMS,
        "distance_semantics": DISTANCE_SEMANTICS,
    }


def _plan_item(plan: DevelopmentPlan, match=None) -> dict:
    item = {
        "plan_id": plan.plan_id,
        "record_id": plan.record_id,
        "name": plan.name,
        "status": plan.status,
        "raw_status": plan.raw_status,
        "source_layer": plan.source_layer,
        "district": plan.district,
        "plan_type": plan.plan_type,
        "procedure_type": plan.procedure_type,
        "area_description": plan.area_description,
        "land_use_text": plan.land_use_text,
        "aufstellungsbeschluss_date": plan.aufstellungsbeschluss_date,
        "festsetzung_date": plan.festsetzung_date,
        "dates": plan.dates,
        "attributes": plan.attributes,
        "geometry": plan.geometry,
        "quality_flags": list(plan.quality_flags),
        "documents": [
            {
                "source_field": source_field,
                "url": url,
                "availability": "not_checked",
                "extraction": None,
            }
            for source_field, url in plan.document_urls.items()
        ],
        "document_status": "linked" if plan.document_urls else "none_published",
    }
    if match is not None:
        item.update(
            intersection_area_m2=match.intersection_area_m2,
            plan_area_share=match.plan_area_share,
            query_area_share=match.query_area_share,
            distance_m=match.distance_m,
        )
    return item


def _unknown_query(request: Request, allowed: set[str]) -> None:
    unknown = set(request.query_params) - allowed
    if unknown:
        raise ConnectorError(
            "UNSUPPORTED_FILTER", "Unsupported query filters: " + ", ".join(sorted(unknown))
        )


def planning_router(get_service) -> APIRouter:
    router = APIRouter()

    @router.get("/api/v1/planning/plans/near")
    def near(
        request: Request,
        snapshot_id: Annotated[str, Query(min_length=1)],
        lon: Annotated[float, Query(ge=-180, le=180)],
        lat: Annotated[float, Query(ge=-90, le=90)],
        radius_m: Annotated[float, Query(ge=1, le=5000)],
        data=Depends(get_service),
        status: Annotated[list[str] | None, Query()] = None,
    ):
        _unknown_query(request, {"snapshot_id", "lon", "lat", "radius_m", "status"})
        manifest = _manifest(data, snapshot_id)
        transformer = Transformer.from_crs("EPSG:4326", "EPSG:25833", always_xy=True)
        x, y = transformer.transform(lon, lat)
        matches = _plan_index(data, snapshot_id).within_radius(x, y, radius_m, statuses=status)
        return {
            **_response_meta(snapshot_id, manifest),
            "plans": [_plan_item(match.plan, match) for match in matches],
        }

    @router.get("/api/v1/planning/plans/in-area")
    def in_area(
        request: Request,
        snapshot_id: Annotated[str, Query(min_length=1)],
        area_code: Annotated[str, Query(min_length=1)],
        data=Depends(get_service),
        lor_snapshot_id: Annotated[str | None, Query(min_length=1)] = None,
    ):
        _unknown_query(request, {"snapshot_id", "area_code", "lor_snapshot_id"})
        manifest = _manifest(data, snapshot_id)
        if lor_snapshot_id is None:
            parent_ids = manifest.get("parent_snapshot_ids", ())
            if not parent_ids:
                raise ConnectorError(
                    "SNAPSHOT_NOT_FOUND", "Planning snapshot has no pinned LOR snapshot"
                )
            lor_snapshot_id = parent_ids[0]
        parent_page = data.records(
            "berlin_lor",
            lor_snapshot_id,
            area_code=area_code,
            limit=1000,
        )
        areas = [
            item if hasattr(item, "geometry") else record_from_dict(item)
            for item in parent_page["records"]
        ]
        area = next(
            (
                item
                for item in areas
                if getattr(item, "hierarchy", None) == "planungsraum"
                and getattr(item, "area_code", None) == area_code
            ),
            None,
        )
        if area is None:
            raise NotFoundError(f"Planungsraum not found: {area_code}")
        matches = _plan_index(data, snapshot_id).intersecting_area(area)
        return {
            **_response_meta(snapshot_id, manifest),
            "area_code": area_code,
            "lor_snapshot_id": lor_snapshot_id,
            "plans": [_plan_item(match.plan, match) for match in matches],
        }

    @router.get("/api/v1/planning/plans/{plan_id:path}")
    def detail(
        request: Request,
        plan_id: str,
        snapshot_id: Annotated[str, Query(min_length=1)],
        data=Depends(get_service),
    ):
        _unknown_query(request, {"snapshot_id"})
        manifest = _manifest(data, snapshot_id)
        plans = _plan_index(data, snapshot_id)
        plan = plans.get_plan(plan_id)
        if plan is None:
            raise NotFoundError(f"Development plan not found: {plan_id}")
        return {**_response_meta(snapshot_id, manifest), "plan": _plan_item(plan)}

    return router
