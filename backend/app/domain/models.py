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


GREEN_SPACE_FEATURE_TYPES = frozenset({"green_space", "playground"})


def _optional_area(name: str, value: Any) -> None:
    if value is not None and (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not isfinite(value)
        or value < 0
    ):
        raise ValidationError(f"{name} must be a nonnegative finite number or null")


@dataclass(frozen=True, kw_only=True)
class GreenSpace(Record):
    """An official public green space or playground polygon (WGS84).

    ``display_point`` is a derived marker position, never the feature itself.
    ``adjacent_record_ids`` lists records of the other feature type in the same
    snapshot whose polygons touch or overlap within the configured tolerance.
    """

    geometry: dict[str, Any]
    display_point: dict[str, Any]
    display_point_method: str
    feature_type: str
    original_category: str
    category_mapping_version: str
    geometry_area_m2: float
    name: str | None = None
    area_code: str | None = None
    hierarchy: str | None = None
    boundary_edition: str | None = None
    source_area_m2: float | None = None
    net_play_area_m2: float | None = None
    adjacent_record_ids: tuple[str, ...] = ()
    attributes: dict[str, str] = field(default_factory=dict)
    record_type: str = field(default="green_space", init=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        if not isinstance(self.geometry, dict) or self.geometry.get("type") not in {
            "Polygon",
            "MultiPolygon",
        }:
            raise ValidationError("GreenSpace geometry must preserve a Polygon or MultiPolygon")
        if not self.geometry.get("coordinates"):
            raise ValidationError("GreenSpace geometry must have coordinates")
        _point(self.display_point)
        for name in (
            "display_point_method",
            "original_category",
            "category_mapping_version",
        ):
            _text(name, getattr(self, name))
        if self.feature_type not in GREEN_SPACE_FEATURE_TYPES:
            raise ValidationError(f"Unsupported green space feature_type: {self.feature_type}")
        for name in ("name", "area_code", "hierarchy", "boundary_edition"):
            _optional_text(name, getattr(self, name))
        if self.area_code is not None and (self.hierarchy is None or self.boundary_edition is None):
            raise ValidationError("area_code requires hierarchy and boundary_edition")
        if self.geometry_area_m2 is None:
            raise ValidationError("geometry_area_m2 is required")
        for name in ("geometry_area_m2", "source_area_m2", "net_play_area_m2"):
            _optional_area(name, getattr(self, name))
        object.__setattr__(self, "adjacent_record_ids", tuple(self.adjacent_record_ids))
        if any(not isinstance(value, str) or not value for value in self.adjacent_record_ids):
            raise ValidationError("adjacent_record_ids must be nonempty strings")
        if not isinstance(self.attributes, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in self.attributes.items()
        ):
            raise ValidationError("attributes must map strings to strings")


DEVELOPMENT_PLAN_STATUSES = frozenset(
    {"in_progress", "adopted", "partially_in_force", "repealed", "unmapped"}
)
"""Coarse status values for Berlin development-plan procedures."""


@dataclass(frozen=True, kw_only=True)
class DevelopmentPlan(Record):
    """A Berlin development-plan procedure with WGS84 polygon geometry."""

    geometry: dict[str, Any]
    plan_id: str
    name: str | None
    status: str
    raw_status: str | None
    source_layer: str
    district: str | None
    plan_type: str | None
    procedure_type: str | None
    area_description: str | None
    land_use_text: str | None
    aufstellungsbeschluss_date: str | None
    festsetzung_date: str | None
    dates: dict[str, str]
    document_urls: dict[str, str]
    attributes: dict[str, str]
    record_type: str = field(default="development_plan", init=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        _text("plan_id", self.plan_id)
        _text("source_layer", self.source_layer)
        if self.status not in DEVELOPMENT_PLAN_STATUSES:
            raise ValidationError(f"Unsupported development-plan status: {self.status}")
        if not isinstance(self.geometry, dict) or self.geometry.get("type") not in {
            "Polygon",
            "MultiPolygon",
        }:
            raise ValidationError("Development-plan geometry must be a Polygon or MultiPolygon")
        if not self.geometry.get("coordinates"):
            raise ValidationError("Development-plan geometry must have coordinates")
        for name in (
            "name",
            "raw_status",
            "district",
            "plan_type",
            "procedure_type",
            "area_description",
            "land_use_text",
            "aufstellungsbeschluss_date",
            "festsetzung_date",
        ):
            _optional_text(name, getattr(self, name))
        for name in ("dates", "document_urls", "attributes"):
            value = getattr(self, name)
            if not isinstance(value, dict) or any(
                not isinstance(key, str) or not isinstance(item, str) for key, item in value.items()
            ):
                raise ValidationError(f"{name} must map strings to strings")


@dataclass(frozen=True, kw_only=True)
class Address(Record):
    """An official address point (WGS84). Never implies vacancy, availability or permitted use."""

    geometry: dict[str, Any]
    street_name: str
    address_type: str
    street_code: str | None = None
    house_number: int | None = None
    house_number_suffix: str | None = None
    postcode: str | None = None
    district_code: str | None = None
    district_name: str | None = None
    locality_code: str | None = None
    locality_name: str | None = None
    area_code: str | None = None
    area_name: str | None = None
    attributes: dict[str, str] = field(default_factory=dict)
    record_type: str = field(default="address", init=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        _point(self.geometry)
        _text("street_name", self.street_name)
        _text("address_type", self.address_type)
        for name in (
            "street_code",
            "house_number_suffix",
            "postcode",
            "district_code",
            "district_name",
            "locality_code",
            "locality_name",
            "area_code",
            "area_name",
        ):
            _optional_text(name, getattr(self, name))
        _optional_count("house_number", self.house_number)
        if not isinstance(self.attributes, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in self.attributes.items()
        ):
            raise ValidationError("attributes must map strings to strings")


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


@dataclass(frozen=True, kw_only=True)
class Building(Record):
    """An official ALKIS building footprint (WGS84 Polygon/MultiPolygon). Function is the cadastral
    building function, not current use; storeys are recorded counts; no floor-area claim."""

    geometry: dict[str, Any]
    alkis_object_type: str
    function_code: str | None = None
    function_label: str | None = None
    storeys_above_ground: int | None = None
    storeys_below_ground: int | None = None
    building_name: str | None = None
    footprint_area_m2: float | None = None
    street_codes: tuple[str, ...] = ()
    address_labels: tuple[str, ...] = ()
    address_reference_kind: str | None = None
    source_url: str | None = None
    attributes: dict[str, str] = field(default_factory=dict)
    record_type: str = field(default="building", init=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        _text("alkis_object_type", self.alkis_object_type)
        for name in ("function_code", "function_label", "building_name", "address_reference_kind"):
            _optional_text(name, getattr(self, name))
        _optional_text("source_url", self.source_url)
        for name in ("storeys_above_ground", "storeys_below_ground"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 0):
                raise ValidationError(f"{name} must be a nonnegative integer or null")
        area = self.footprint_area_m2
        if area is not None and (
            isinstance(area, bool)
            or not isinstance(area, (int, float))
            or not isfinite(area)
            or area < 0
        ):
            raise ValidationError("footprint_area_m2 must be finite and nonnegative or null")
        for name in ("street_codes", "address_labels"):
            values = getattr(self, name)
            if not isinstance(values, (tuple, list)) or any(
                not isinstance(value, str) or not value.strip() for value in values
            ):
                raise ValidationError(f"{name} must contain nonempty strings")
            object.__setattr__(self, name, tuple(values))
        if not isinstance(self.geometry, dict) or self.geometry.get("type") not in {
            "Polygon",
            "MultiPolygon",
        }:
            raise ValidationError("Building geometry must be a Polygon or MultiPolygon")
        from shapely.geometry import shape

        try:
            geometry = shape(self.geometry)
        except Exception as exc:
            raise ValidationError("Building geometry is invalid") from exc
        if (
            geometry.is_empty
            or not geometry.is_valid
            or geometry.geom_type
            not in {
                "Polygon",
                "MultiPolygon",
            }
        ):
            raise ValidationError("Building geometry must be a valid nonempty polygon")
        if not isinstance(self.attributes, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in self.attributes.items()
        ):
            raise ValidationError("attributes must map strings to strings")


@dataclass(frozen=True, kw_only=True)
class ResidentialLocationClass(Record):
    geometry: dict[str, Any]
    address_key: str
    street_code: str
    house_number: int
    house_number_suffix: str | None
    house_number_label: str
    street_name: str | None
    postcode: str | None
    district_name: str | None
    planning_area_name: str | None
    east_west_marker: str | None
    wohnlage_class: str | None
    class_status: str
    source_label: str | None
    edition: str
    valid_from: str
    valid_to: str
    attributes: dict[str, str] = field(default_factory=dict)
    record_type: str = field(default="residential_location_class", init=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        _point(self.geometry)
        _text("address_key", self.address_key)
        if not re.fullmatch(r"\d{8}[A-Z]{0,2}", self.address_key):
            raise ValidationError("address_key must be an 8-digit key with optional suffix")
        if not re.fullmatch(r"\d{5}", self.street_code):
            raise ValidationError("street_code must contain five digits")
        if type(self.house_number) is not int or not 0 <= self.house_number <= 999:
            raise ValidationError("house_number must be an integer from 0 to 999")
        _optional_text("house_number_suffix", self.house_number_suffix)
        if self.house_number_suffix is not None and not re.fullmatch(
            r"[A-Z]{1,2}", self.house_number_suffix
        ):
            raise ValidationError("house_number_suffix must be one or two uppercase letters")
        _text("house_number_label", self.house_number_label)
        if self.house_number_label != f"{self.house_number:03d}{self.house_number_suffix or ''}":
            raise ValidationError("house_number_label does not match its numeric and suffix fields")
        if self.address_key != f"{self.street_code}{self.house_number_label}":
            raise ValidationError("address_key does not match street code and house number")
        if self.source_record_id != self.address_key:
            raise ValidationError("source_record_id must equal address_key")
        if not isinstance(self.valid_from, str) or self.record_id != (
            f"wohnlage:{self.valid_from[:4]}:{self.address_key}"
        ):
            raise ValidationError("record_id must use the Wohnlage edition year and address key")
        for name in (
            "street_name",
            "postcode",
            "district_name",
            "planning_area_name",
            "east_west_marker",
            "wohnlage_class",
            "source_label",
        ):
            _optional_text(name, getattr(self, name))
        if self.class_status not in {"assigned", "unassigned", "unmapped"}:
            raise ValidationError("class_status must be assigned, unassigned or unmapped")
        if self.class_status == "assigned":
            if (
                self.wohnlage_class not in {"einfach", "mittel", "gut"}
                or self.source_label != self.wohnlage_class
            ):
                raise ValidationError("assigned Wohnlage records require a known class")
        elif self.wohnlage_class is not None:
            raise ValidationError("unassigned or unmapped records cannot have a class")
        if self.class_status == "unassigned" and self.source_label is not None:
            raise ValidationError("unassigned records cannot have an original class label")
        if self.class_status == "unmapped" and (
            self.source_label is None or self.source_label in {"einfach", "mittel", "gut"}
        ):
            raise ValidationError("unmapped records require an unknown original source label")
        _text("edition", self.edition)
        for name in ("valid_from", "valid_to"):
            try:
                date.fromisoformat(getattr(self, name))
            except (TypeError, ValueError) as exc:
                raise ValidationError(f"{name} must be an ISO calendar date") from exc
        if self.valid_from > self.valid_to:
            raise ValidationError("valid_from cannot be after valid_to")
        if not isinstance(self.attributes, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in self.attributes.items()
        ):
            raise ValidationError("attributes must map strings to strings")


@dataclass(frozen=True, kw_only=True)
class AreaIndicatorClass(Record):
    """Ordinal class a published indicator assigns to an area; not a numeric measurement."""

    area_code: str
    hierarchy: str
    boundary_edition: str
    indicator_code: str
    class_code: str | None
    class_label: str | None
    class_rank: int | None
    class_count: int
    rank_direction: str
    reference_period: str
    value_status: ValueStatus | str
    attributes: dict[str, str] = field(default_factory=dict)
    record_type: str = field(default="area_indicator_class", init=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        _area_keys(self.area_code, self.hierarchy, self.boundary_edition)
        _text("indicator_code", self.indicator_code)
        _text("rank_direction", self.rank_direction)
        _period(self.reference_period)
        if type(self.class_count) is not int or self.class_count < 1:
            raise ValidationError("class_count must be a positive integer")
        try:
            status = ValueStatus(self.value_status)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"Unsupported value_status: {self.value_status}") from exc
        object.__setattr__(self, "value_status", status)
        if status == ValueStatus.FORECAST:
            raise ValidationError("Area indicator classes cannot be forecasts")
        if status in {
            ValueStatus.MISSING,
            ValueStatus.SUPPRESSED,
            ValueStatus.NOT_APPLICABLE,
        }:
            if any(
                value is not None for value in (self.class_code, self.class_label, self.class_rank)
            ):
                raise ValidationError("Missing classes must have null class fields")
        else:
            _text("class_code", self.class_code)
            _text("class_label", self.class_label)
            if type(self.class_rank) is not int or not 1 <= self.class_rank <= self.class_count:
                raise ValidationError("class_rank must be within 1..class_count")
        if not isinstance(self.attributes, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in self.attributes.items()
        ):
            raise ValidationError("attributes must map strings to strings")


@dataclass(frozen=True, kw_only=True)
class GridObservation(Record):
    geometry: dict[str, Any]
    grid_id: str
    grid_crs: str
    resolution_m: int
    metric_code: str
    value: float | int | None
    unit: str
    reference_period: str
    value_status: ValueStatus | str
    area_code: str | None = None
    record_type: str = field(default="grid_observation", init=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        if not isinstance(self.geometry, dict) or self.geometry.get("type") != "Polygon":
            raise ValidationError("Grid geometry must be a GeoJSON Polygon")
        coordinates = self.geometry.get("coordinates")
        if not isinstance(coordinates, (list, tuple)) or not coordinates:
            raise ValidationError("Grid geometry must have coordinates")
        for ring in coordinates:
            if not isinstance(ring, (list, tuple)) or not ring:
                raise ValidationError("Grid geometry must have polygon coordinate rings")
            for position in ring:
                if (
                    not isinstance(position, (list, tuple))
                    or len(position) != 2
                    or any(
                        isinstance(value, bool)
                        or not isinstance(value, (int, float))
                        or not isfinite(value)
                        for value in position
                    )
                ):
                    raise ValidationError("Grid coordinates must be finite WGS84 positions")
                longitude, latitude = position
                if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
                    raise ValidationError("Grid coordinates must be WGS84 longitude/latitude")
        for name in ("grid_id", "grid_crs", "metric_code", "unit"):
            _text(name, getattr(self, name))
        if type(self.resolution_m) is not int or self.resolution_m <= 0:
            raise ValidationError("resolution_m must be a positive integer")
        _period(self.reference_period)
        try:
            status = ValueStatus(self.value_status)
        except (TypeError, ValueError) as exc:
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
        _optional_text("area_code", self.area_code)


@dataclass(frozen=True, kw_only=True)
class Facility(Record):
    """A registered facility at a WGS84 point. Capacity and vacancies are nullable;
    a present value needs its own definition and ISO reference date."""

    geometry: dict[str, Any]
    facility_type: str
    provider_facility_id: str
    original_category: str
    category_mapping_version: str
    canonical_category: str | None = None
    name: str | None = None
    capacity: int | None = None
    capacity_definition: str | None = None
    capacity_reference_date: str | None = None
    vacancies: int | None = None
    vacancies_reference_date: str | None = None
    area_code: str | None = None
    attributes: dict[str, str] = field(default_factory=dict)
    record_type: str = field(default="facility", init=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        _point(self.geometry)
        for name in (
            "facility_type",
            "provider_facility_id",
            "original_category",
            "category_mapping_version",
        ):
            _text(name, getattr(self, name))
        for name in ("canonical_category", "name", "capacity_definition", "area_code"):
            _optional_text(name, getattr(self, name))
        for value, reference in (
            ("capacity", "capacity_reference_date"),
            ("vacancies", "vacancies_reference_date"),
        ):
            _optional_count(value, getattr(self, value))
            reference_date = getattr(self, reference)
            if reference_date is not None:
                try:
                    date.fromisoformat(reference_date)
                except (TypeError, ValueError) as exc:
                    raise ValidationError(f"{reference} must be an ISO calendar date") from exc
            if getattr(self, value) is not None and reference_date is None:
                raise ValidationError(f"{value} requires {reference}")
        if self.capacity is not None and self.capacity_definition is None:
            raise ValidationError("capacity requires capacity_definition")
        if not isinstance(self.attributes, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in self.attributes.items()
        ):
            raise ValidationError("attributes must map strings to strings")


NormalizedRecord: TypeAlias = (
    Area
    | AreaObservation
    | ForecastObservation
    | Place
    | Business
    | TransitStop
    | StopService
    | GreenSpace
    | DevelopmentPlan
    | AreaIndicatorClass
    | GridObservation
    | Address
    | Facility
    | Building
    | ResidentialLocationClass
)
RECORD_TYPES: dict[str, type] = {
    "area": Area,
    "area_observation": AreaObservation,
    "forecast_observation": ForecastObservation,
    "place": Place,
    "business": Business,
    "transit_stop": TransitStop,
    "stop_service": StopService,
    "green_space": GreenSpace,
    "address": Address,
    "residential_location_class": ResidentialLocationClass,
    "development_plan": DevelopmentPlan,
    "area_indicator_class": AreaIndicatorClass,
    "grid_observation": GridObservation,
    "facility": Facility,
    "building": Building,
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


@dataclass(frozen=True, kw_only=True)
class RetailCenter(Record):
    """Planning-designated retail center or specialist retail site; WGS84 GeoJSON geometry."""

    geometry: dict[str, Any]
    layer: str
    wfs_feature_id: str
    center_type: str | None
    center_category: str | None
    center_role: str | None
    hierarchy_level: int | None
    name: str | None
    parent_center_id: str | None
    category_mapping_version: str
    source_url: str
    attributes: dict[str, str] = field(default_factory=dict)
    record_type: str = field(default="retail_center", init=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        if not isinstance(self.geometry, dict) or self.geometry.get("type") not in {
            "Point",
            "Polygon",
            "MultiPolygon",
        }:
            raise ValidationError("RetailCenter geometry must be a Point, Polygon or MultiPolygon")
        coordinates = self.geometry.get("coordinates")
        if not isinstance(coordinates, (list, tuple)) or not coordinates:
            raise ValidationError("RetailCenter geometry must have coordinates")

        def position(value: Any) -> tuple[float, ...]:
            if (
                not isinstance(value, (list, tuple))
                or len(value) < 2
                or any(
                    type(coordinate) not in (int, float) or not isfinite(coordinate)
                    for coordinate in value
                )
                or not -180 <= value[0] <= 180
                or not -90 <= value[1] <= 90
            ):
                raise ValidationError("RetailCenter coordinates must be finite WGS84 positions")
            return tuple(value)

        def ring(value: Any) -> None:
            if not isinstance(value, (list, tuple)) or len(value) < 4:
                raise ValidationError(
                    "RetailCenter polygon rings must have at least four positions"
                )
            positions = [position(item) for item in value]
            if positions[0] != positions[-1]:
                raise ValidationError("RetailCenter polygon rings must be closed")

        def polygon(value: Any) -> None:
            if not isinstance(value, (list, tuple)) or not value:
                raise ValidationError("RetailCenter polygons must have at least one ring")
            for polygon_ring in value:
                ring(polygon_ring)

        geometry_type = self.geometry["type"]
        if geometry_type == "Point":
            position(coordinates)
        elif geometry_type == "Polygon":
            polygon(coordinates)
        else:
            if not isinstance(coordinates, (list, tuple)) or not coordinates:
                raise ValidationError("RetailCenter multipolygons must contain polygons")
            for component in coordinates:
                polygon(component)
        for name in ("layer", "wfs_feature_id", "category_mapping_version", "source_url"):
            _text(name, getattr(self, name))
        for name in (
            "center_type",
            "center_category",
            "center_role",
            "name",
            "parent_center_id",
        ):
            _optional_text(name, getattr(self, name))
        if self.center_role not in {
            None,
            "center",
            "center_area",
            "special_center",
            "specialist_retail_site",
        }:
            raise ValidationError("Unsupported retail-center role")
        if self.hierarchy_level is not None and (
            type(self.hierarchy_level) is not int or self.hierarchy_level not in range(1, 5)
        ):
            raise ValidationError("hierarchy_level must be 1-4 or null")
        if not isinstance(self.attributes, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in self.attributes.items()
        ):
            raise ValidationError("attributes must map strings to strings")


NormalizedRecord = (
    Area
    | AreaObservation
    | ForecastObservation
    | Place
    | Business
    | TransitStop
    | StopService
    | GreenSpace
    | Address
    | DevelopmentPlan
    | ResidentialLocationClass
    | AreaIndicatorClass
    | GridObservation
    | Facility
    | RetailCenter
)
RECORD_TYPES["retail_center"] = RetailCenter


@dataclass(frozen=True, kw_only=True)
class MobilityObservation(Record):
    """Counted traffic at one fixed counter for an explicit local interval.

    A count measures the stated mode at that location only; it is not footfall,
    customers or revenue and must not be projected onto other streets.
    """

    geometry: dict
    location_id: str
    location_name: str
    mode: str
    metric_code: str
    unit: str
    aggregation: str
    interval_start: str
    interval_end: str
    timezone: str
    measurement_status: str
    value: int | float | None = None
    value_status: ValueStatus = ValueStatus.MISSING
    direction: str | None = None
    window_id: str | None = None
    expected_intervals: int = 0
    observed_intervals: int = 0
    reconstructed_intervals: int = 0
    implausible_intervals: int = 0
    missing_intervals: int = 0
    area_code: str | None = None
    attributes: dict[str, str] = field(default_factory=dict)
    record_type: str = field(default="mobility_observation", init=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        _point(self.geometry)
        for name in (
            "location_id",
            "location_name",
            "mode",
            "metric_code",
            "unit",
            "aggregation",
            "interval_start",
            "interval_end",
            "timezone",
            "measurement_status",
        ):
            _text(name, getattr(self, name))
        for name in ("direction", "window_id", "area_code"):
            _optional_text(name, getattr(self, name))
        try:
            status = ValueStatus(self.value_status)
        except ValueError as exc:
            raise ValidationError(f"Unsupported value_status: {self.value_status}") from exc
        object.__setattr__(self, "value_status", status)
        null_status = status in {
            ValueStatus.MISSING,
            ValueStatus.NOT_APPLICABLE,
            ValueStatus.SUPPRESSED,
        }
        if self.value is None:
            if not null_status:
                raise ValidationError("A null mobility value requires a null value_status")
        elif (
            isinstance(self.value, bool)
            or not isinstance(self.value, (int, float))
            or not isfinite(self.value)
            or self.value < 0
            or null_status
        ):
            raise ValidationError("Mobility values must be nonnegative finite numbers")
        for name in (
            "expected_intervals",
            "observed_intervals",
            "reconstructed_intervals",
            "implausible_intervals",
            "missing_intervals",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValidationError(f"{name} must be a nonnegative integer")
        if (
            self.observed_intervals
            + self.reconstructed_intervals
            + self.implausible_intervals
            + self.missing_intervals
            != self.expected_intervals
        ):
            raise ValidationError("Mobility interval counts must sum to expected_intervals")
        if self.aggregation not in {"day", "window"}:
            raise ValidationError("aggregation must be day or window")
        if self.aggregation == "window" and self.window_id is None:
            raise ValidationError("Window observations require a window_id")
        if not isinstance(self.attributes, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in self.attributes.items()
        ):
            raise ValidationError("attributes must map strings to strings")


RECORD_TYPES["mobility_observation"] = MobilityObservation

NormalizedRecord = NormalizedRecord | MobilityObservation


@dataclass(frozen=True, kw_only=True)
class RoadTrafficSegment(Record):
    """Road segment with average daily motor-vehicle traffic (vehicles/day); WGS84 line geometry."""

    geometry: dict[str, Any]
    reference_period: str
    dtv_motor_vehicles_per_day: int | None = None
    vehicle_class_volumes_per_day: dict[str, int] = field(default_factory=dict)
    name: str | None = None
    source_length_m: int | None = None
    area_code: str | None = None
    attributes: dict[str, str] = field(default_factory=dict)
    record_type: str = field(default="road_traffic_segment", init=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        _period(self.reference_period)
        if (
            not isinstance(self.geometry, dict)
            or self.geometry.get("type") not in {"LineString", "MultiLineString"}
            or not self.geometry.get("coordinates")
        ):
            raise ValidationError("Road traffic geometry must have LineString coordinates")
        _optional_count("dtv_motor_vehicles_per_day", self.dtv_motor_vehicles_per_day)
        _optional_count("source_length_m", self.source_length_m)
        for name in ("name", "area_code"):
            _optional_text(name, getattr(self, name))
        if not isinstance(self.vehicle_class_volumes_per_day, dict) or any(
            not isinstance(key, str)
            or isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            for key, value in self.vehicle_class_volumes_per_day.items()
        ):
            raise ValidationError(
                "vehicle_class_volumes_per_day must map strings to nonnegative integers"
            )
        if not isinstance(self.attributes, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in self.attributes.items()
        ):
            raise ValidationError("attributes must map strings to strings")


RECORD_TYPES["road_traffic_segment"] = RoadTrafficSegment
NormalizedRecord = NormalizedRecord | RoadTrafficSegment


POPULATION_BLOCK_AGE_BANDS = (
    "0-6",
    "6-10",
    "10-18",
    "18-65",
    "65-70",
    "70-75",
    "75-80",
    "80+",
)


@dataclass(frozen=True, kw_only=True)
class PopulationBlock(Record):
    """Umweltatlas ISU5 block (or block part) with registered residents as published."""

    geometry: dict[str, Any]
    block_id: str
    area_ha: float
    residents: int | None
    residents_status: ValueStatus | str
    density_per_ha: float | None
    reference_period: str
    residents_by_age: dict[str, int | None] = field(default_factory=dict)
    land_use_type: str | None = None
    land_use_type_en: str | None = None
    area_code: str | None = None
    hierarchy: str | None = None
    boundary_edition: str | None = None
    area_overlap_share: float | None = None
    record_type: str = field(default="population_block", init=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        _text("block_id", self.block_id)
        if (
            not isinstance(self.geometry, dict)
            or self.geometry.get("type") not in {"Polygon", "MultiPolygon"}
            or not self.geometry.get("coordinates")
        ):
            raise ValidationError("Population block geometry must be a polygon with coordinates")
        if (
            isinstance(self.area_ha, bool)
            or not isinstance(self.area_ha, (int, float))
            or not isfinite(self.area_ha)
            or self.area_ha <= 0
        ):
            raise ValidationError("area_ha must be a finite positive number")
        try:
            status = ValueStatus(self.residents_status)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"Unsupported residents_status: {self.residents_status}") from exc
        if status not in {ValueStatus.OBSERVED, ValueStatus.SUPPRESSED}:
            raise ValidationError("residents_status must be observed or suppressed")
        object.__setattr__(self, "residents_status", status)
        if status is ValueStatus.OBSERVED:
            if type(self.residents) is not int or self.residents < 0:
                raise ValidationError("Observed residents must be a nonnegative integer")
            if (
                isinstance(self.density_per_ha, bool)
                or not isinstance(self.density_per_ha, (int, float))
                or not isfinite(self.density_per_ha)
                or self.density_per_ha < 0
            ):
                raise ValidationError("Observed density_per_ha must be finite and nonnegative")
        elif (
            self.residents is not None
            or self.density_per_ha is not None
            or bool(self.residents_by_age)
        ):
            raise ValidationError("Suppressed population values and age bands must be missing")
        if not isinstance(self.residents_by_age, dict):
            raise ValidationError("residents_by_age must be an object")
        if any(key not in POPULATION_BLOCK_AGE_BANDS for key in self.residents_by_age):
            raise ValidationError("residents_by_age contains an unsupported age band")
        if self.residents_by_age and set(self.residents_by_age) != set(POPULATION_BLOCK_AGE_BANDS):
            raise ValidationError("residents_by_age must be empty or contain every age band")
        if any(
            value is not None and (type(value) is not int or value < 0)
            for value in self.residents_by_age.values()
        ):
            raise ValidationError("Age-band residents must be nonnegative integers or missing")
        _period(self.reference_period)
        for name in ("land_use_type", "land_use_type_en"):
            _optional_text(name, getattr(self, name))
        parent_values = (self.area_code, self.hierarchy, self.boundary_edition)
        if any(value is not None for value in (*parent_values, self.area_overlap_share)):
            if any(value is None for value in (*parent_values, self.area_overlap_share)):
                raise ValidationError("Parent assignment fields must be all set or all missing")
            _area_keys(*parent_values)
            share = self.area_overlap_share
            if (
                isinstance(share, bool)
                or not isinstance(share, (int, float))
                or not isfinite(share)
                or not 0 < share <= 1
            ):
                raise ValidationError("area_overlap_share must be finite and in (0, 1]")


RECORD_TYPES["population_block"] = PopulationBlock
NormalizedRecord = NormalizedRecord | PopulationBlock
NormalizedRecord = NormalizedRecord | Address | Building


@dataclass(frozen=True, kw_only=True)
class Parcel(Record):
    geometry: dict[str, Any]
    parcel_id: str
    cadastral_district_code: str
    parcel_numerator: str
    cadastral_district_name: str | None = None
    cadastral_section: str | None = None
    parcel_denominator: str | None = None
    municipality_code: str | None = None
    municipality_name: str | None = None
    responsible_office: str | None = None
    official_area_m2: float | None = None
    official_area_source: str = "ALKIS afl (Amtliche Fläche), source-provided"
    geometry_area_m2: float
    geometry_area_method: str = "planar area of source geometry in EPSG:25833"
    area_unit: str = "m2"
    created_date: str | None = None
    lifespan_start: str | None = None
    record_type: str = field(default="parcel", init=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        for name in (
            "parcel_id",
            "cadastral_district_code",
            "parcel_numerator",
            "official_area_source",
            "geometry_area_method",
            "area_unit",
        ):
            _text(name, getattr(self, name))
        for name in (
            "cadastral_district_name",
            "cadastral_section",
            "parcel_denominator",
            "municipality_code",
            "municipality_name",
            "responsible_office",
        ):
            _optional_text(name, getattr(self, name))
        if not isinstance(self.geometry, dict) or self.geometry.get("type") not in {
            "Polygon",
            "MultiPolygon",
        }:
            raise ValidationError("Parcel geometry must preserve a Polygon or MultiPolygon")
        if not self.geometry.get("coordinates"):
            raise ValidationError("Parcel geometry must have coordinates")
        if self.official_area_m2 is not None and (
            isinstance(self.official_area_m2, bool)
            or not isinstance(self.official_area_m2, (int, float))
            or not isfinite(self.official_area_m2)
            or self.official_area_m2 <= 0
        ):
            raise ValidationError("official_area_m2 must be a finite positive number or null")
        if (
            isinstance(self.geometry_area_m2, bool)
            or not isinstance(self.geometry_area_m2, (int, float))
            or not isfinite(self.geometry_area_m2)
            or self.geometry_area_m2 < 0
        ):
            raise ValidationError("geometry_area_m2 must be a finite nonnegative number")
        for name in ("created_date", "lifespan_start"):
            value = getattr(self, name)
            if value is not None:
                try:
                    date.fromisoformat(value)
                except (TypeError, ValueError) as exc:
                    raise ValidationError(f"{name} must be an ISO calendar date or null") from exc


RECORD_TYPES["parcel"] = Parcel
NormalizedRecord = NormalizedRecord | Parcel
