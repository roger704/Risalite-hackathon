"""Public local-snapshot reads. Acquisition is deliberately a CLI operation."""

import os
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api.research import router as research_router
from app.application.service import DataService
from app.domain import ConnectorError, NormalizedRecord, SnapshotManifest


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


def default_data_dir() -> Path:
    configured = os.environ.get("LOCATION_DATA_DIR", "").strip()
    return (
        Path(configured).expanduser()
        if configured
        else Path(__file__).resolve().parents[3] / "data"
    )


def _error_status(code: str) -> int:
    if code in {"NOT_FOUND", "UNKNOWN_DATASET", "DATASET_NOT_FOUND", "SNAPSHOT_NOT_FOUND"}:
        return 404
    if code in {"SNAPSHOT_INCOMPATIBLE", "INCOMPATIBLE_SNAPSHOTS", "BOUNDARY_EDITION_MISMATCH"}:
        return 409
    if code in {"SOURCE_UNAVAILABLE", "DATA_UNAVAILABLE", "DATASET_UNAVAILABLE"}:
        return 503
    return 422


def create_app(
    service: DataService | None = None,
    data_dir: Path | str | None = None,
    config_dir: Path | str | None = None,
    runs_dir: Path | str | None = None,
    mock: bool | None = None,
) -> FastAPI:
    application = FastAPI(title="AI Location Intelligence", version="0.1.0")
    application.state.data_service = service
    application.state.research_orchestrator = None
    application.state.research_runs_dir = Path(runs_dir) if runs_dir is not None else None
    application.state.research_mock = mock
    application.state.research_tasks = set()

    def get_service() -> DataService:
        if application.state.data_service is None:
            application.state.data_service = DataService(
                data_dir=Path(data_dir) if data_dir is not None else default_data_dir(),
                config_dir=Path(config_dir) if config_dir is not None else None,
            )
        return application.state.data_service

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

    application.include_router(research_router)
    return application


app = create_app()
