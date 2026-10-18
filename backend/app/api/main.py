"""Public local-snapshot reads. Acquisition is deliberately a CLI operation."""

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

from fastapi import Depends, FastAPI, Query, Request
from fastapi import Path as PathParam
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from app.api.runs_fixtures import router as fixture_runs_router
from app.application.area_summary import SNAPSHOT_PARAMETERS, AreaSummaryService
from app.application.service import DataService
from app.domain import ConnectorError, NormalizedRecord, NotFoundError, SnapshotManifest
from app.export import EXPORT_FORMATS, Mix, load_run


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
    records: list[NormalizedRecord]
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
) -> FastAPI:
    application = FastAPI(title="AI Location Intelligence", version="0.1.0")
    application.state.data_service = service

    def get_service() -> DataService:
        if application.state.data_service is None:
            application.state.data_service = DataService(
                data_dir=Path(data_dir) if data_dir is not None else default_data_dir(),
                config_dir=Path(config_dir) if config_dir is not None else None,
            )
        return application.state.data_service

    def get_runs_dir() -> Path:
        configured = os.environ.get("RISALITE_RUNS_DIR", "").strip()
        return (
            Path(runs_dir).expanduser()
            if runs_dir is not None
            else Path(configured).expanduser()
            if configured
            else Path(__file__).resolve().parents[3] / "runs"
        )

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
        bundle = load_run(get_runs_dir() / run_id)
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

    if os.environ.get("RISALITE_RUNS", "").casefold() != "off":
        application.include_router(fixture_runs_router)

    # Keep this last: the "/" mount catches every otherwise-unmatched route.
    mount_frontend(application)
    return application


app = create_app()
