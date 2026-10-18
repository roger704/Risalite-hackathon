"""Public local-snapshot reads. Acquisition is deliberately a CLI operation."""

import json
import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi import Path as PathParam
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from app.api.buildings import create_buildings_router
from app.api.centers import create_centers_router
from app.api.parcels import parcels_router
from app.api.planning import planning_router
from app.api.routing import routing_router
from app.api.runs_fixtures import router as fixture_runs_router
from app.application.area_summary import SNAPSHOT_PARAMETERS, AreaSummaryService
from app.application.childcare import childcare_areas, childcare_facilities
from app.application.green_spaces import nearby_green_spaces
from app.application.mobility import station_layer
from app.application.service import DataService
from app.connectors.population_forecast import expected_change
from app.connectors.valhalla import ValhallaWalkingClient
from app.domain import (
    ConnectorError,
    ForecastObservation,
    NormalizedRecord,
    NotFoundError,
    SnapshotManifest,
    record_from_dict,
)
from app.domain.models import MobilityObservation
from app.domain.routing import load_routing_config
from app.export import EXPORT_FORMATS, Mix, load_run
from app.infrastructure.routing_cache import RoutingCache, default_routing_cache_path


class DatasetResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    dataset_id: str


class RecordsResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    schema_version: str = "1.0"
    dataset_id: str
    snapshot_id: str
    availability: str = "ready"
    freshness: str = "unknown"
    retrieved_at: datetime | None = None
    reference_period: str | None = None
    coverage: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    attribution: list[dict[str, Any]] = Field(default_factory=list)
    records: list[NormalizedRecord | MobilityObservation]
    count: int = Field(ge=0)
    next_cursor: str | None = None


class AddressSearchResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    schema_version: str = "1.0"
    dataset_id: str
    snapshot_id: str
    retrieved_at: datetime
    source_edition: str | None = None
    reference_period: str | None = None
    query: dict[str, Any]
    results: list[dict[str, Any]]
    total: int = Field(ge=0)
    count: int = Field(ge=0)
    next_cursor: str | None = None
    ambiguous: bool
    candidates: list[dict[str, Any]]
    notice: str
    warnings: list[str] = Field(default_factory=list)
    attribution: list[dict[str, Any]] = Field(default_factory=list)


class AreaSummaryResponse(BaseModel):
    """Versioned area summary; see CONNECTOR_BLUEPRINT.md section 7."""

    model_config = ConfigDict(extra="allow")
    schema_version: str
    summary_type: str
    calculation_version: str
    parameters: dict[str, Any]
    area: dict[str, Any]
    residents: dict[str, Any]
    competitors: dict[str, Any]
    workplaces: dict[str, Any]
    transit: dict[str, Any]
    snapshots: list[dict[str, Any]]
    not_integrated: list[str]
    not_provided: list[str]
    limitations: list[str]
    warnings: list[str]


def default_data_dir() -> Path:
    configured = os.environ.get("LOCATION_DATA_DIR", "").strip()
    return (
        Path(configured).expanduser()
        if configured
        else Path(__file__).resolve().parents[3] / "data"
    )


def _error_status(code: str) -> int:
    if code in {
        "NOT_FOUND",
        "UNKNOWN_DATASET",
        "DATASET_NOT_FOUND",
        "SNAPSHOT_NOT_FOUND",
        "AREA_NOT_FOUND",
        "RUN_NOT_FOUND",
        "UNKNOWN_FORMAT",
    }:
        return 404
    if code in {"SNAPSHOT_INCOMPATIBLE", "INCOMPATIBLE_SNAPSHOTS", "BOUNDARY_EDITION_MISMATCH"}:
        return 409
    if code in {"SOURCE_UNAVAILABLE", "DATA_UNAVAILABLE", "DATASET_UNAVAILABLE"}:
        return 503
    if code == "ROUTING_BUDGET_EXCEEDED":
        return 429
    return 422


def mount_frontend(application: FastAPI) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    frontend_root = Path(
        os.environ.get("RISALITE_FRONTEND_DIR", str(repo_root / "frontend"))
    ).expanduser()
    fixtures_dir = frontend_root / "fixtures"
    app_dir = frontend_root / "app"
    if fixtures_dir.is_dir():
        application.mount("/fixtures", StaticFiles(directory=str(fixtures_dir)), name="fixtures")
    if app_dir.is_dir():
        application.mount("/", StaticFiles(directory=str(app_dir), html=True), name="frontend")


def create_app(
    service: DataService | None = None,
    data_dir: Path | str | None = None,
    config_dir: Path | str | None = None,
    runs_dir: Path | str | None = None,
    routing_client: ValhallaWalkingClient | None = None,
) -> FastAPI:
    application = FastAPI(title="AI Location Intelligence", version="0.1.0")
    application.state.data_service = service
    application.state.routing_client = routing_client
    runs_mode = os.environ.get("RISALITE_RUNS", "").casefold()
    configured_runs_dir = os.environ.get("RISALITE_RUNS_DIR", "").strip()
    default_runs_dir = (
        Path("data/runs") if runs_mode == "live" else Path(__file__).resolve().parents[3] / "runs"
    )
    resolved_runs_dir = (
        Path(runs_dir).expanduser()
        if runs_dir is not None
        else Path(configured_runs_dir).expanduser()
        if configured_runs_dir
        else default_runs_dir
    )
    application.state.runs_dir = resolved_runs_dir
    application.state.live_runs_dir = resolved_runs_dir if runs_mode == "live" else None
    application.state.live_run_service = None

    def get_service() -> DataService:
        if application.state.data_service is None:
            application.state.data_service = DataService(
                data_dir=Path(data_dir) if data_dir is not None else default_data_dir(),
                config_dir=Path(config_dir) if config_dir is not None else None,
            )
        return application.state.data_service

    def get_routing_client() -> ValhallaWalkingClient:
        if application.state.routing_client is None:
            configured_data_dir = (
                Path(data_dir).expanduser().resolve()
                if data_dir is not None
                else default_data_dir().resolve()
            )
            config = load_routing_config()
            application.state.routing_client = ValhallaWalkingClient(
                config,
                cache=RoutingCache(default_routing_cache_path(configured_data_dir)),
            )
        return application.state.routing_client

    @application.on_event("shutdown")
    async def close_routing_client() -> None:
        client = application.state.routing_client
        if client is not None and client._owns_client:
            await client.__aexit__(None, None, None)

    def get_runs_dir() -> Path:
        return application.state.runs_dir

    @application.middleware("http")
    async def request_context(request: Request, call_next):
        request.state.request_id = str(uuid4())
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    def error_response(request: Request, code: str, message: str, retryable: bool, status: int):
        return JSONResponse(
            status_code=status,
            content={
                "error": {
                    "code": code,
                    "message": message,
                    "retryable": retryable,
                    "request_id": request.state.request_id,
                    "dataset_id": request.path_params.get("dataset_id"),
                }
            },
        )

    @application.exception_handler(ConnectorError)
    async def connector_error(request: Request, exc: ConnectorError):
        return error_response(
            request, exc.code, exc.message, exc.retryable, _error_status(exc.code)
        )

    @application.exception_handler(RequestValidationError)
    async def request_error(request: Request, exc: RequestValidationError):
        # Avoid echoing opaque cursors and supplied query values into the response.
        fields = [".".join(str(part) for part in error["loc"]) for error in exc.errors()]
        return error_response(
            request, "INVALID_REQUEST", "Invalid request fields: " + ", ".join(fields), False, 422
        )

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    application.include_router(create_buildings_router(get_service))

    @application.get("/api/v1/datasets", response_model=list[DatasetResponse])
    def datasets(data: Annotated[DataService, Depends(get_service)]):
        return data.list_datasets()

    @application.get(
        "/api/v1/datasets/{dataset_id}/snapshots/{snapshot_id}",
        response_model=SnapshotManifest,
    )
    def manifest(
        dataset_id: str, snapshot_id: str, data: Annotated[DataService, Depends(get_service)]
    ):
        return data.manifest(dataset_id, snapshot_id)

    @application.get("/api/v1/datasets/{dataset_id}/legend")
    def legend(dataset_id: str, data: Annotated[DataService, Depends(get_service)]):
        return data.legend(dataset_id)

    @application.get("/api/v1/datasets/{dataset_id}/records", response_model=RecordsResponse)
    def records(
        request: Request,
        dataset_id: str,
        data: Annotated[DataService, Depends(get_service)],
        snapshot_id: Annotated[str, Query(min_length=1)],
        area_code: Annotated[str | None, Query(min_length=1)] = None,
        metric_code: Annotated[str | None, Query(min_length=1)] = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
        cursor: Annotated[str | None, Query(min_length=1)] = None,
    ):
        unknown = set(request.query_params) - {
            "snapshot_id",
            "area_code",
            "metric_code",
            "limit",
            "cursor",
        }
        if unknown:
            raise ConnectorError(
                "UNSUPPORTED_FILTER", "Unsupported query filters: " + ", ".join(sorted(unknown))
            )
        return data.records(
            dataset_id,
            snapshot_id,
            area_code=area_code,
            metric_code=metric_code,
            limit=limit,
            cursor=cursor,
        )

    @application.get("/api/v1/datasets/{dataset_id}/green-spaces/nearby")
    def green_spaces_nearby(
        request: Request,
        dataset_id: str,
        data: Annotated[DataService, Depends(get_service)],
        snapshot_id: Annotated[str, Query(min_length=1)],
        lon: Annotated[float, Query(ge=-180, le=180)],
        lat: Annotated[float, Query(ge=-90, le=90)],
        radius_m: Annotated[float, Query(ge=0, le=5000)] = 500.0,
        feature_type: Annotated[str | None, Query(min_length=1)] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ):
        unknown = set(request.query_params) - {
            "snapshot_id",
            "lon",
            "lat",
            "radius_m",
            "feature_type",
            "limit",
        }
        if unknown:
            raise ConnectorError(
                "UNSUPPORTED_FILTER", "Unsupported query filters: " + ", ".join(sorted(unknown))
            )
        return nearby_green_spaces(
            data, dataset_id, snapshot_id, lon, lat, radius_m, feature_type, limit
        )

    @application.get(
        "/api/v1/datasets/{dataset_id}/address-search",
        response_model=AddressSearchResponse,
    )
    def address_search(
        request: Request,
        dataset_id: str,
        data: Annotated[DataService, Depends(get_service)],
        snapshot_id: Annotated[str, Query(min_length=1)],
        q: Annotated[str, Query(min_length=1, max_length=200)],
        postcode: Annotated[str | None, Query(pattern=r"^\d{5}$")] = None,
        district: Annotated[str | None, Query(min_length=1)] = None,
        limit: Annotated[int, Query(ge=1, le=50)] = 10,
        cursor: Annotated[str | None, Query(min_length=1)] = None,
    ):
        unknown = set(request.query_params) - {
            "snapshot_id",
            "q",
            "postcode",
            "district",
            "limit",
            "cursor",
        }
        if unknown:
            raise ConnectorError(
                "UNSUPPORTED_FILTER", "Unsupported query filters: " + ", ".join(sorted(unknown))
            )
        return data.search_addresses(
            dataset_id,
            snapshot_id,
            q,
            postcode=postcode,
            district=district,
            limit=limit,
            cursor=cursor,
        )

    application.include_router(create_centers_router(get_service))

    @application.get("/api/v1/childcare/facilities")
    def childcare_facility_points(
        data: Annotated[DataService, Depends(get_service)],
        snapshot_id: Annotated[str, Query(min_length=1)],
    ) -> dict[str, Any]:
        return childcare_facilities(data, snapshot_id)

    @application.get("/api/v1/childcare/areas")
    def childcare_area_counts(
        data: Annotated[DataService, Depends(get_service)],
        snapshot_id: Annotated[str, Query(min_length=1)],
        population_snapshot_id: Annotated[str | None, Query(min_length=1)] = None,
    ) -> dict[str, Any]:
        return childcare_areas(data, snapshot_id, population_snapshot_id)

    @application.get("/api/v1/datasets/{dataset_id}/wohnlage")
    def wohnlage(
        request: Request,
        dataset_id: str,
        data: Annotated[DataService, Depends(get_service)],
    ):
        allowed = {
            "snapshot_id",
            "address_key",
            "street_code",
            "house_number",
            "house_number_suffix",
            "address_snapshot_id",
            "address_id",
        }
        unknown = set(request.query_params) - allowed
        if unknown:
            raise ConnectorError(
                "INVALID_REQUEST", "Unsupported query parameters: " + ", ".join(sorted(unknown))
            )
        if any(len(request.query_params.getlist(name)) != 1 for name in request.query_params):
            raise ConnectorError("INVALID_REQUEST", "Query parameters must be supplied once")
        values = {name: request.query_params.get(name) for name in allowed}
        if not values["snapshot_id"]:
            raise ConnectorError("INVALID_REQUEST", "snapshot_id is required")
        address_key_mode = values["address_key"] is not None
        street_mode = any(
            values[name] is not None
            for name in ("street_code", "house_number", "house_number_suffix")
        )
        address_mode = any(
            values[name] is not None for name in ("address_snapshot_id", "address_id")
        )
        if sum((address_key_mode, street_mode, address_mode)) != 1:
            raise ConnectorError(
                "INVALID_REQUEST",
                "Supply exactly one of address_key, street_code/house_number, or "
                "address_snapshot_id/address_id",
            )
        if street_mode and (values["street_code"] is None or values["house_number"] is None):
            raise ConnectorError(
                "INVALID_REQUEST", "street_code and house_number must be supplied together"
            )
        if address_mode and (not values["address_snapshot_id"] or not values["address_id"]):
            raise ConnectorError(
                "INVALID_REQUEST", "address_snapshot_id and address_id must be supplied together"
            )
        return data.wohnlage(
            dataset_id,
            values["snapshot_id"],
            address_key=values["address_key"],
            street_code=values["street_code"],
            house_number=values["house_number"],
            house_number_suffix=values["house_number_suffix"],
            address_snapshot_id=values["address_snapshot_id"],
            address_id=values["address_id"],
        )

    @application.get("/api/v1/datasets/{dataset_id}/layers/counters")
    def counters(
        request: Request,
        dataset_id: str,
        data: Annotated[DataService, Depends(get_service)],
        snapshot_id: Annotated[str, Query(min_length=1)],
        window_id: Annotated[str, Query(min_length=1)],
        year: Annotated[int, Query(ge=1900, le=2100)],
    ):
        unknown = set(request.query_params) - {"snapshot_id", "window_id", "year"}
        if unknown:
            raise ConnectorError(
                "INVALID_FILTER", "Unsupported query filters: " + ", ".join(sorted(unknown))
            )
        return station_layer(data, dataset_id, snapshot_id, window_id, year)
    @application.get("/api/v1/population-forecast/expected-change")
    def population_forecast_change(
        data: Annotated[DataService, Depends(get_service)],
        snapshot_id: Annotated[str, Query(min_length=1)],
        area_code: Annotated[str, Query(min_length=1)],
        target_year: int,
        scenario: str = "middle",
        hierarchy: str | None = None,
        metric_code: str = "population_total",
        age_min: Annotated[int | None, Query(ge=0)] = None,
        age_max_exclusive: Annotated[int | None, Query(ge=0)] = None,
    ):
        config = data.configs.get("berlin_population_forecast")
        if config is None:
            raise ConnectorError("UNKNOWN_DATASET", "Unknown dataset: berlin_population_forecast")
        if scenario not in {"middle", "upper", "lower"}:
            raise ConnectorError(
                "INVALID_FILTER", "scenario must be one of middle, upper, or lower"
            )
        if hierarchy is not None and hierarchy not in {"land", "bezirk", "prognoseraum"}:
            raise ConnectorError(
                "INVALID_FILTER",
                "hierarchy must be one of land, bezirk, or prognoseraum",
            )
        if re.fullmatch(r"\d{8}", area_code):
            raise ConnectorError(
                "INVALID_FILTER",
                "Population forecasts are published only to Prognoseraum resolution; "
                f"use area_code {area_code[:4]} instead of Planungsraum {area_code}.",
            )
        if age_max_exclusive is not None and (age_min is None or age_max_exclusive <= age_min):
            raise ConnectorError(
                "INVALID_FILTER",
                "age_max_exclusive must be greater than age_min; provide both for bounded ages",
            )

        matches: list[ForecastObservation] = []
        cursor = None
        while True:
            page = data.records(
                "berlin_population_forecast",
                snapshot_id,
                area_code=area_code,
                limit=1000,
                cursor=cursor,
            )
            for item in page["records"]:
                record = record_from_dict(item)
                if (
                    isinstance(record, ForecastObservation)
                    and record.metric_code == metric_code
                    and record.scenario == scenario
                    and record.age_min == age_min
                    and record.age_max_exclusive == age_max_exclusive
                    and (hierarchy is None or record.hierarchy == hierarchy)
                ):
                    matches.append(record)
            cursor = page["next_cursor"]
            if cursor is None:
                break

        if hierarchy is None:
            matched_hierarchies = {record.hierarchy for record in matches}
            if len(matched_hierarchies) > 1:
                choices = [
                    f"hierarchy={candidate}"
                    for candidate in ("land", "bezirk", "prognoseraum")
                    if candidate in matched_hierarchies
                ]
                raise ConnectorError(
                    "INVALID_FILTER",
                    f"Area code {area_code} is ambiguous; specify " + " or ".join(choices) + ".",
                )
        if not matches:
            raise ConnectorError(
                "NOT_FOUND",
                f"No {scenario} {metric_code} data for area {area_code} and target year "
                f"{target_year} with the requested age interval.",
            )
        base_periods = {record.base_period for record in matches}
        if len(base_periods) != 1:
            raise ConnectorError(
                "INVALID_FILTER",
                "Forecast records in this snapshot do not share one base period.",
            )
        base_period = next(iter(base_periods))
        try:
            target_period = date(target_year, 12, 31).isoformat()
        except ValueError as exc:
            raise ConnectorError(
                "INVALID_FILTER", "target_year must be a valid calendar year"
            ) from exc
        if date.fromisoformat(target_period) <= date.fromisoformat(base_period):
            raise ConnectorError(
                "INVALID_FILTER",
                f"target_year must be later than the snapshot base period {base_period}.",
            )
        bases = [
            record
            for record in matches
            if record.reference_period == base_period and record.value_status == "observed"
        ]
        targets = [
            record
            for record in matches
            if record.reference_period == target_period and record.value_status == "forecast"
        ]
        common_units = {record.unit for record in bases} & {record.unit for record in targets}
        preferred_unit = "thousand_persons" if scenario != "middle" else "persons"
        unit = preferred_unit if preferred_unit in common_units else next(iter(common_units), None)
        bases = [record for record in bases if record.unit == unit]
        targets = [record for record in targets if record.unit == unit]
        if len(bases) != 1 or len(targets) != 1:
            raise ConnectorError(
                "NOT_FOUND",
                f"No {scenario} {metric_code} data for area {area_code} and target year "
                f"{target_year} with the requested age interval.",
        )
        return expected_change(bases[0], targets[0])

    def get_area_summaries(
        data: Annotated[DataService, Depends(get_service)],
    ) -> AreaSummaryService:
        current = getattr(application.state, "area_summaries", None)
        if current is None or current.data is not data:
            current = application.state.area_summaries = AreaSummaryService(data)
        return current

    @application.get("/api/v1/areas/{area_code}/summary", response_model=AreaSummaryResponse)
    def area_summary(
        request: Request,
        area_code: Annotated[str, PathParam(pattern=r"^\d{8}$")],
        summaries: Annotated[AreaSummaryService, Depends(get_area_summaries)],
        category: Annotated[str, Query(pattern=r"^[a-z][a-z0-9_]{0,63}$")] = "coffee_shop",
        include_geometry: bool = True,
        lor_snapshot_id: Annotated[str | None, Query(min_length=1)] = None,
        names_snapshot_id: Annotated[str | None, Query(min_length=1)] = None,
        population_snapshot_id: Annotated[str | None, Query(min_length=1)] = None,
        osm_snapshot_id: Annotated[str | None, Query(min_length=1)] = None,
        ihk_snapshot_id: Annotated[str | None, Query(min_length=1)] = None,
        gtfs_snapshot_id: Annotated[str | None, Query(min_length=1)] = None,
    ):
        unknown = set(request.query_params) - {"category", "include_geometry", *SNAPSHOT_PARAMETERS}
        if unknown:
            raise ConnectorError(
                "UNSUPPORTED_FILTER", "Unsupported query parameters: " + ", ".join(sorted(unknown))
            )
        pins = {
            "lor_snapshot_id": lor_snapshot_id,
            "names_snapshot_id": names_snapshot_id,
            "population_snapshot_id": population_snapshot_id,
            "osm_snapshot_id": osm_snapshot_id,
            "ihk_snapshot_id": ihk_snapshot_id,
            "gtfs_snapshot_id": gtfs_snapshot_id,
        }
        return summaries.summary(
            area_code,
            category=category,
            include_geometry=include_geometry,
            snapshot_ids=pins,
        )

    application.include_router(routing_router(get_routing_client))

    @application.get("/api/v1/runs/{run_id:path}/export/{fmt}")
    def export_run(request: Request, run_id: str, fmt: str):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", run_id):
            raise NotFoundError("Research run not found", "RUN_NOT_FOUND")
        if fmt not in EXPORT_FORMATS:
            raise NotFoundError("Unknown export format", "UNKNOWN_FORMAT")
        unknown = set(request.query_params) - {"layers", "mode"}
        if unknown:
            raise ConnectorError(
                "UNSUPPORTED_FILTER", "Unsupported query filters: " + ", ".join(sorted(unknown))
            )
        mode = request.query_params.get("mode")
        if mode is not None and mode not in {"present", "future"}:
            raise ConnectorError("INVALID_REQUEST", "mode must be present or future")
        layers_value = request.query_params.get("layers")
        layers = (
            frozenset(layer.strip() for layer in layers_value.split(",") if layer.strip())
            if layers_value is not None
            else None
        )
        mix = Mix(layers_on=layers, mode=mode)
        run_dir = get_runs_dir() / run_id
        try:
            persisted_run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            persisted_run = {}
        if isinstance(persisted_run, dict) and "token_hash" in persisted_run:
            from app.api.runs_live import service as live_run_service
            from app.application.runs import RunError

            try:
                live_run_service(request).authorize_export(
                    run_id, request.headers.get("authorization")
                )
            except RunError as exc:
                raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
        bundle = load_run(run_dir)
        media_type, renderer = EXPORT_FORMATS[fmt]
        if fmt.startswith("report."):
            body = renderer(bundle, mix=mix)
        else:
            body = renderer(bundle)
        if fmt == "sources.csv":
            body = "\ufeff" + body
        disposition = "inline" if fmt == "report.html" else "attachment"
        headers = {"Content-Disposition": f'{disposition}; filename="risalite-{run_id}-{fmt}"'}
        return Response(content=body, media_type=media_type, headers=headers)

    application.include_router(planning_router(get_service))
    if runs_mode == "live":
        from app.api.runs_live import router as live_runs_router

        application.include_router(live_runs_router)
    elif runs_mode != "off":
        application.include_router(fixture_runs_router)

    application.include_router(parcels_router(get_service))

    # Keep this last: the "/" mount catches every otherwise-unmatched route.
    mount_frontend(application)
    return application


app = create_app()
