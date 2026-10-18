"""Read-only nearby endpoint for the berlin_centers snapshot."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from app.application.service import DataService
from app.domain import ConnectorError, RetailCenter, record_from_dict
from app.geo.centers import center_relationships

_BERLIN_BBOX = (13.0, 52.3, 13.9, 52.8)
_METHOD = {
    "crs": "EPSG:25833",
    "distance": "straight-line metres to nearest published boundary or published center point; not walking distance or time",
    "membership": "point-in-polygon only for published Zentrumsbereich polygons; point features have no boundary, so membership is not determined",
}
_CONTEXT_NOTICE = (
    "StEP Zentren 2040 is an official planning designation. Proximity is context only, "
    "not measured footfall, revenue, or a guarantee of suitability."
)


def create_centers_router(get_service) -> APIRouter:
    router = APIRouter()

    @router.get("/api/v1/datasets/berlin_centers/nearby")
    def nearby(
        request: Request,
        snapshot_id: Annotated[str, Query(min_length=1)],
        lon: float,
        lat: float,
        radius_m: Annotated[float, Query(gt=0, le=5000)] = 1500,
        data: DataService = Depends(get_service),
    ):
        unknown = set(request.query_params) - {"snapshot_id", "lon", "lat", "radius_m"}
        if unknown:
            raise ConnectorError(
                "UNSUPPORTED_FILTER", "Unsupported query filters: " + ", ".join(sorted(unknown))
            )
        if not all(
            _BERLIN_BBOX[index] <= value <= _BERLIN_BBOX[index + 2]
            for index, value in ((0, lon), (1, lat))
        ):
            raise ConnectorError(
                "INVALID_REQUEST", "Coordinates must be within the configured Berlin bbox"
            )
        centers = []
        cursor = None
        while True:
            page = data.records("berlin_centers", snapshot_id, limit=1000, cursor=cursor)
            centers.extend(
                record
                for value in page["records"]
                if isinstance((record := record_from_dict(value)), RetailCenter)
            )
            cursor = page["next_cursor"]
            if cursor is None:
                break
        manifest = data.manifest("berlin_centers", snapshot_id)
        try:
            relationships = center_relationships(centers, lon, lat, radius_m)
        except ValueError as exc:
            raise ConnectorError("INVALID_REQUEST", str(exc)) from exc
        inside_boundary_ids = [
            item["record_id"] for item in relationships if item["relation"] == "inside_boundary"
        ]
        return {
            "dataset_id": "berlin_centers",
            "snapshot_id": snapshot_id,
            "candidate": {"lon": lon, "lat": lat},
            "radius_m": radius_m,
            "method": _METHOD,
            "context_notice": _CONTEXT_NOTICE,
            "centers": relationships,
            "inside_boundary_ids": inside_boundary_ids,
            "count": len(relationships),
            "license": manifest.get("license_id"),
            "license_url": manifest.get("license_url"),
            "attribution": manifest.get("attribution"),
            "landing_url": manifest.get("landing_url"),
        }

    return router
