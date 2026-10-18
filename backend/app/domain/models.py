"""Small, framework-free contracts shared by connectors and application services.

Geometries are WGS84 GeoJSON. Provider-specific CRS conversion, topology and
cross-dataset completeness checks belong to adapters and ingestion services.
"""

import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from math import isfinite
from typing import Any, Mapping, TypeAlias

from .errors import ConfigError, ValidationError

SCHEMA_VERSION = "1.0"
PUBLIC_REDISTRIBUTION_STATUSES = frozenset({"allowed", "allowed_with_attribution"})


def _text(name: str, value: Any) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} must be a nonempty string")


def _period(value: str) -> None:
    _text("reference_period", value)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValidationError("reference_period is not a valid calendar date") from exc


def _utc(value: datetime) -> None:
    if not isinstance(value, datetime) or value.utcoffset() != timedelta(0):
        raise ValidationError("retrieved_at must be a timezone-aware UTC datetime")


class ValueStatus(str, Enum):
    OBSERVED = "observed"
    ESTIMATED = "estimated"
    FORECAST = "forecast"
    SUPPRESSED = "suppressed"
    MISSING = "missing"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True, kw_only=True)
class DatasetConfig:
    dataset_id: str
    provider: str
    adapter: str
    landing_url: str
    resource_url: str
    allowed_hosts: tuple[str, ...]
    license_id: str
    license_url: str
    attribution: str
    boundary_edition: str
    source_edition: str | None = None
    reference_period: str | None = None
    parser_options: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    max_download_bytes: int = 50_000_000
    timeout_seconds: float = 30.0
    access_validation: str = "unvalidated"
    schema_version: str = SCHEMA_VERSION
    redistribution_status: str = "unknown"
    source_crs: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "dataset_id",
            "provider",
            "adapter",
            "landing_url",
            "resource_url",
            "boundary_edition",
        ):
            _text(name, getattr(self, name))
        if self.schema_version != SCHEMA_VERSION:
            raise ConfigError(f"Unsupported schema version: {self.schema_version}")
        if type(self.enabled) is not bool:
            raise ConfigError("enabled must be a boolean")
        if (
            not isinstance(self.allowed_hosts, (tuple, list))
            or not self.allowed_hosts
            or any(not isinstance(host, str) or not host.strip() for host in self.allowed_hosts)
        ):
            raise ConfigError("allowed_hosts must be a nonempty list or tuple of host strings")
        object.__setattr__(self, "allowed_hosts", tuple(self.allowed_hosts))
        if type(self.max_download_bytes) is not int or self.max_download_bytes <= 0:
            raise ConfigError("max_download_bytes must be a positive integer")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or self.timeout_seconds <= 0
            or not isfinite(self.timeout_seconds)
        ):
            raise ConfigError("timeout_seconds must be a finite positive number")
        if self.access_validation not in {"unvalidated", "validated", "blocked"}:
            raise ConfigError("Invalid access_validation state")
        if not isinstance(self.parser_options, dict):
            raise ConfigError("parser_options must be an object")
        if self.reference_period is not None:
            _period(self.reference_period)


@dataclass(frozen=True, kw_only=True)
class RawAsset:
    path: str
    sha256: str
    size_bytes: int
    resource_url: str
    media_type: str | None = None
    etag: str | None = None
    last_modified: str | None = None

    def __post_init__(self) -> None:
        _text("path", self.path)
        _text("resource_url", self.resource_url)
        if not re.fullmatch(r"[a-fA-F0-9]{64}", self.sha256):
            raise ValidationError("sha256 must be a hexadecimal SHA-256 digest")
        if (
            not isinstance(self.size_bytes, int)
            or isinstance(self.size_bytes, bool)
            or self.size_bytes < 0
        ):
            raise ValidationError("size_bytes must be a nonnegative integer")


@dataclass(frozen=True, kw_only=True)
class RawBundle:
    assets: tuple[RawAsset, ...]
    retrieved_at: datetime
    snapshot_id: str
    request_fingerprint: str

    def __post_init__(self) -> None:
        _utc(self.retrieved_at)
        _text("snapshot_id", self.snapshot_id)
        _text("request_fingerprint", self.request_fingerprint)
        if not self.assets:
            raise ValidationError("A raw bundle must identify at least one source asset")


@dataclass(frozen=True, kw_only=True)
class FetchRequest:
    config: DatasetConfig
    staging_dir: str
    snapshot_id: str

    def __post_init__(self) -> None:
        _text("staging_dir", self.staging_dir)
        _text("snapshot_id", self.snapshot_id)


@dataclass(frozen=True, kw_only=True)
class SourceHealth:
    status: str
    checked_at: datetime
    message: str | None = None

    def __post_init__(self) -> None:
        _text("status", self.status)
        _utc(self.checked_at)


@dataclass(frozen=True, kw_only=True)
class Record:
    source_id: str
    snapshot_id: str
    source_record_id: str
    record_id: str
    schema_version: str = SCHEMA_VERSION
    quality_flags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("source_id", "snapshot_id", "source_record_id", "record_id"):
            _text(name, getattr(self, name))
        if self.schema_version != SCHEMA_VERSION:
            raise ValidationError(f"Unsupported schema version: {self.schema_version}")


def _area_keys(area_code: str, hierarchy: str, boundary_edition: str) -> None:
    # Never coerce numeric identifiers: that would irreversibly lose leading zeros.
    _text("area_code", area_code)
    _text("hierarchy", hierarchy)
    _text("boundary_edition", boundary_edition)


@dataclass(frozen=True, kw_only=True)
class Area(Record):
    geometry: dict[str, Any]
    area_code: str
    name: str
    hierarchy: str
    boundary_edition: str
    attributes: dict[str, str] = field(default_factory=dict)
    record_type: str = field(default="area", init=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        _area_keys(self.area_code, self.hierarchy, self.boundary_edition)
        _text("name", self.name)
        if not isinstance(self.geometry, dict) or self.geometry.get("type") not in {
            "Polygon",
            "MultiPolygon",
        }:
            raise ValidationError("Area geometry must preserve a Polygon or MultiPolygon")
        if not self.geometry.get("coordinates"):
            raise ValidationError("Area geometry must have coordinates")
        if not isinstance(self.attributes, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in self.attributes.items()
        ):
            raise ValidationError("attributes must map strings to strings")


@dataclass(frozen=True, kw_only=True)
class AreaObservation(Record):
    area_code: str
    hierarchy: str
    boundary_edition: str
    metric_code: str
    value: float | int | None
    unit: str
    reference_period: str
    value_status: ValueStatus | str
    age_min: int | None = None
    age_max_exclusive: int | None = None
    record_type: str = field(default="area_observation", init=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        _area_keys(self.area_code, self.hierarchy, self.boundary_edition)
        _text("metric_code", self.metric_code)
        _text("unit", self.unit)
        _period(self.reference_period)
        try:
            status = ValueStatus(self.value_status)
        except ValueError as exc:
            raise ValidationError(f"Unsupported value_status: {self.value_status}") from exc
        object.__setattr__(self, "value_status", status)
        if status in {ValueStatus.MISSING, ValueStatus.SUPPRESSED, ValueStatus.NOT_APPLICABLE}:
            if self.value is not None:
                raise ValidationError("Missing, suppressed and not-applicable values must be null")
        elif (
            isinstance(self.value, bool)
            or not isinstance(self.value, (int, float))
            or not isfinite(self.value)
        ):
            raise ValidationError("Observed, estimated and forecast values must be finite numbers")
        for name in ("age_min", "age_max_exclusive"):
            bound = getattr(self, name)
            if bound is not None and (
                isinstance(bound, bool) or not isinstance(bound, int) or bound < 0
            ):
                raise ValidationError(f"{name} must be a nonnegative integer or null")
        if self.age_max_exclusive is not None and self.age_max_exclusive <= (self.age_min or 0):
            raise ValidationError("Age interval must have a positive width")


def _optional_text(name: str, value: Any) -> None:
    if value is not None:
        _text(name, value)


def _point(geometry: Any) -> None:
    if not isinstance(geometry, dict) or geometry.get("type") != "Point":
        raise ValidationError("Point records require a GeoJSON Point geometry")
    coordinates = geometry.get("coordinates")
    if (
        not isinstance(coordinates, (list, tuple))
        or len(coordinates) != 2
        or any(
            isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value)
            for value in coordinates
        )
    ):
        raise ValidationError("Point coordinates must be two finite numbers")
    longitude, latitude = coordinates
    if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
        raise ValidationError("Point coordinates must be WGS84 longitude/latitude")


def _optional_count(name: str, value: Any) -> None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
        raise ValidationError(f"{name} must be a nonnegative integer or null")


@dataclass(frozen=True, kw_only=True)
class Place(Record):
    """A mapped point of interest. ``geometry`` is a representative WGS84 point;
    ``source_geometry_type`` records the original element/geometry kind."""

    geometry: dict[str, Any]
    source_geometry_type: str
    original_category: str
    category_mapping_version: str
    canonical_category: str | None = None
    name: str | None = None
    area_code: str | None = None
    attributes: dict[str, str] = field(default_factory=dict)
    record_type: str = field(default="place", init=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        _point(self.geometry)
        for name in ("source_geometry_type", "original_category", "category_mapping_version"):
            _text(name, getattr(self, name))
        for name in ("canonical_category", "name", "area_code"):
            _optional_text(name, getattr(self, name))
        if not isinstance(self.attributes, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in self.attributes.items()
        ):
            raise ValidationError("attributes must map strings to strings")


@dataclass(frozen=True, kw_only=True)
class Business(Record):
    """A registered business. Employee bounds are an inclusive interval, not a headcount.
    ``industry_code`` is null when the source supplies no classification."""

    geometry: dict[str, Any]
    industry_code_system: str
    category_mapping_version: str
    industry_code: str | None = None
    industry_description: str | None = None
    canonical_category: str | None = None
    employees_label: str | None = None
    employees_min: int | None = None
    employees_max: int | None = None
    business_age_label: str | None = None
    area_code: str | None = None
    hierarchy: str | None = None
    boundary_edition: str | None = None
    record_type: str = field(default="business", init=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        _point(self.geometry)
        for name in ("industry_code_system", "category_mapping_version"):
            _text(name, getattr(self, name))
        for name in (
            "industry_code",
            "industry_description",
            "canonical_category",
            "employees_label",
            "business_age_label",
            "area_code",
            "hierarchy",
            "boundary_edition",
        ):
            _optional_text(name, getattr(self, name))
        _optional_count("employees_min", self.employees_min)
        _optional_count("employees_max", self.employees_max)
        if (
            self.employees_min is not None
            and self.employees_max is not None
            and self.employees_max < self.employees_min
        ):
            raise ValidationError("employees_max cannot be below employees_min")


@dataclass(frozen=True, kw_only=True)
class TransitStop(Record):
    """A GTFS stop. ``location_type`` follows GTFS: 0 stop/platform, 1 station,
    2 entrance/exit, 3 generic node, 4 boarding area."""

    geometry: dict[str, Any]
    stop_id: str
    name: str
    location_type: int
    parent_station: str | None = None
    wheelchair_boarding: int | None = None
    area_code: str | None = None
    record_type: str = field(default="transit_stop", init=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        _point(self.geometry)
        _text("stop_id", self.stop_id)
        _text("name", self.name)
        if type(self.location_type) is not int or self.location_type not in range(5):
            raise ValidationError("location_type must be a GTFS location type 0-4")
        _optional_text("parent_station", self.parent_station)
        if self.wheelchair_boarding is not None and (
            type(self.wheelchair_boarding) is not int or self.wheelchair_boarding not in range(3)
        ):
            raise ValidationError("wheelchair_boarding must be 0, 1, 2 or null")
        _optional_text("area_code", self.area_code)


@dataclass(frozen=True, kw_only=True)
class StopService(Record):
    """Scheduled departures at one stop or parent station in an explicit local window.

    Measures service supply, not passengers or footfall. Times are GTFS service times
    in ``timezone`` and may exceed 24:00:00.
    """

    stop_id: str
    service_date: str
    window_start: str
    window_end: str
    timezone: str
    departures: int
    trip_count: int
    route_count: int
    route_types: tuple[int, ...] = ()
    area_code: str | None = None
    record_type: str = field(default="stop_service", init=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        _text("stop_id", self.stop_id)
        _text("timezone", self.timezone)
        try:
            date.fromisoformat(self.service_date)
        except (TypeError, ValueError) as exc:
            raise ValidationError("service_date must be an ISO calendar date") from exc
        for name in ("window_start", "window_end"):
            if not isinstance(getattr(self, name), str) or not re.fullmatch(
                r"\d{2,3}:[0-5]\d:[0-5]\d", getattr(self, name)
            ):
                raise ValidationError(f"{name} must be a GTFS HH:MM:SS time")
        to_seconds = lambda text: sum(  # noqa: E731
            int(part) * factor for part, factor in zip(text.split(":"), (3600, 60, 1))
        )
        if to_seconds(self.window_end) <= to_seconds(self.window_start):
            raise ValidationError("window_end must be after window_start")
        for name in ("departures", "trip_count", "route_count"):
            value = getattr(self, name)
            if value is None:
                raise ValidationError(f"{name} is required")
            _optional_count(name, value)
        object.__setattr__(self, "route_types", tuple(self.route_types))
        if any(type(value) is not int for value in self.route_types):
            raise ValidationError("route_types must be integers")
        _optional_text("area_code", self.area_code)


@dataclass(frozen=True, kw_only=True)
class ForecastObservation(Record):
    area_code: str
    hierarchy: str
    boundary_edition: str
    metric_code: str
    value: float | int
    unit: str
    reference_period: str
    value_status: ValueStatus | str
    scenario: str
    forecast_edition: str
    base_period: str
    age_min: int | None = None
    age_max_exclusive: int | None = None
    record_type: str = field(default="forecast_observation", init=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        _area_keys(self.area_code, self.hierarchy, self.boundary_edition)
        _text("metric_code", self.metric_code)
        _text("scenario", self.scenario)
        _text("forecast_edition", self.forecast_edition)
        if self.scenario not in {"middle", "upper", "lower"}:
            raise ValidationError(f"Unsupported forecast scenario: {self.scenario}")
        if self.hierarchy == "land":
            valid_code = self.area_code == "11"
        elif self.hierarchy == "bezirk":
            valid_code = (
                bool(re.fullmatch(r"\d{2}", self.area_code)) and 1 <= int(self.area_code) <= 12
            )
        elif self.hierarchy == "prognoseraum":
            valid_code = (
                bool(re.fullmatch(r"\d{4}", self.area_code)) and 1 <= int(self.area_code[:2]) <= 12
            )
        else:
            valid_code = False
        if not valid_code:
            raise ValidationError(
                f"Invalid area code {self.area_code!r} for hierarchy {self.hierarchy!r}"
            )
        for name in ("reference_period", "base_period"):
            value = getattr(self, name)
            try:
                if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                    raise ValueError
                date.fromisoformat(value)
            except ValueError as exc:
                raise ValidationError(f"{name} must be an ISO calendar date") from exc
        reference = date.fromisoformat(self.reference_period)
        base = date.fromisoformat(self.base_period)
        if reference < base:
            raise ValidationError("reference_period cannot be before base_period")
        try:
            status = ValueStatus(self.value_status)
        except ValueError as exc:
            raise ValidationError(f"Unsupported value_status: {self.value_status}") from exc
        expected_status = ValueStatus.OBSERVED if reference == base else ValueStatus.FORECAST
        if status != expected_status:
            raise ValidationError(
                f"value_status must be {expected_status.value} for this reference_period"
            )
        object.__setattr__(self, "value_status", status)
        if (
            isinstance(self.value, bool)
            or not isinstance(self.value, (int, float))
            or not isfinite(self.value)
            or self.value < 0
        ):
            raise ValidationError("Forecast value must be a finite nonnegative number")
        if self.unit not in {"persons", "thousand_persons"}:
            raise ValidationError(f"Unsupported forecast unit: {self.unit}")
        _text("unit", self.unit)
        lower, upper = self.age_min, self.age_max_exclusive
        if lower is None and upper is not None:
            raise ValidationError("Age bounds must both be null for totals or have an age_min")
        if lower is not None and (
            isinstance(lower, bool) or not isinstance(lower, int) or lower < 0
        ):
            raise ValidationError("age_min must be a nonnegative integer or null")
        if upper is not None and (
            isinstance(upper, bool) or not isinstance(upper, int) or lower is None or upper <= lower
        ):
            raise ValidationError("age_max_exclusive must be greater than age_min or null")


NormalizedRecord: TypeAlias = (
    Area | AreaObservation | ForecastObservation | Place | Business | TransitStop | StopService
)
RECORD_TYPES: dict[str, type] = {
    "area": Area,
    "area_observation": AreaObservation,
    "forecast_observation": ForecastObservation,
    "place": Place,
    "business": Business,
    "transit_stop": TransitStop,
    "stop_service": StopService,
}


@dataclass(frozen=True, kw_only=True)
class NormalizedBatch:
    records: tuple[NormalizedRecord, ...]
    input_rows: int
    rejected_rows: int = 0
    rejections: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.input_rows < 0 or self.rejected_rows < 0 or self.rejected_rows > self.input_rows:
            raise ValidationError("Invalid input/rejected row counts")


@dataclass(frozen=True, kw_only=True)
class QualityReport:
    valid: bool
    input_rows: int
    output_rows: int
    rejected_rows: int = 0
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            min(self.input_rows, self.output_rows, self.rejected_rows) < 0
            or self.rejected_rows > self.input_rows
        ):
            raise ValidationError("Invalid quality-report row counts")
        if self.valid and self.errors:
            raise ValidationError("A valid quality report cannot contain errors")


@dataclass(frozen=True, kw_only=True)
class SnapshotManifest:
    snapshot_id: str
    dataset_id: str
    adapter_version: str
    landing_url: str
    resource_url: str
    retrieved_at: datetime
    raw_assets: tuple[RawAsset, ...]
    request_fingerprint: str
    parser_config_hash: str
    quality_report: QualityReport
    license_id: str
    license_url: str
    attribution: str
    schema_version: str = SCHEMA_VERSION
    source_edition: str | None = None
    reference_period: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    boundary_edition: str | None = None
    coverage: dict[str, Any] = field(default_factory=dict)
    redistribution_status: str = "unknown"
    normalized_assets: tuple[str, ...] = ()
    parent_snapshot_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _utc(self.retrieved_at)
        _text("snapshot_id", self.snapshot_id)
        _text("dataset_id", self.dataset_id)
        if self.reference_period is not None:
            _period(self.reference_period)
        for name in ("valid_from", "valid_to"):
            value = getattr(self, name)
            if value is not None:
                try:
                    date.fromisoformat(value)
                except ValueError as exc:
                    raise ValidationError(f"{name} must be an ISO calendar date") from exc
        if self.valid_from and self.valid_to and self.valid_from > self.valid_to:
            raise ValidationError("valid_from cannot be after valid_to")


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def record_to_dict(record: NormalizedRecord) -> dict[str, Any]:
    return _json_value(asdict(record))


def record_from_dict(data: Mapping[str, Any]) -> NormalizedRecord:
    values = dict(data)
    record_type = values.pop("record_type", None)
    record_class = RECORD_TYPES.get(record_type)
    if record_class is None:
        raise ValidationError(f"Unsupported record_type: {record_type}")
    values["quality_flags"] = tuple(values.get("quality_flags", ()))
    if "route_types" in values:
        values["route_types"] = tuple(values["route_types"])
    try:
        return record_class(**values)
    except TypeError as exc:
        raise ValidationError(f"Invalid {record_type} fields: {exc}") from exc


def manifest_to_dict(manifest: SnapshotManifest) -> dict[str, Any]:
    return _json_value(asdict(manifest))


def manifest_from_dict(data: Mapping[str, Any]) -> SnapshotManifest:
    values = dict(data)
    try:
        values["retrieved_at"] = datetime.fromisoformat(
            values["retrieved_at"].replace("Z", "+00:00")
        )
        values["raw_assets"] = tuple(RawAsset(**asset) for asset in values["raw_assets"])
        report = dict(values["quality_report"])
        report["warnings"] = tuple(report.get("warnings", ()))
        report["errors"] = tuple(report.get("errors", ()))
        values["quality_report"] = QualityReport(**report)
        for name in ("normalized_assets", "parent_snapshot_ids"):
            values[name] = tuple(values.get(name, ()))
        return SnapshotManifest(**values)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValidationError(f"Invalid snapshot manifest: {exc}") from exc
