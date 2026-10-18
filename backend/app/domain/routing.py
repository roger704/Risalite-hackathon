"""Contracts and configuration for reviewed walking-routing estimates."""

import hashlib
import json
import math
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from .errors import ConfigError, ConnectorError

BACKEND_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ROUTING_CONFIG = BACKEND_ROOT / "config" / "routing" / "valhalla_berlin_walking.json"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _positive_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and value > 0
    )


@dataclass(frozen=True)
class RoutingConfig:
    routing_id: str
    engine: str
    engine_version: str
    engine_license_id: str
    engine_license_url: str
    container_image: str
    base_url: str
    allowed_hosts: tuple[str, ...]
    denied_hosts: tuple[str, ...]
    graph: Mapping[str, Any]
    profile: Mapping[str, Any]
    limits: Mapping[str, Any]
    assumptions: tuple[str, ...]
    limitations: tuple[str, ...]

    @property
    def profile_hash(self) -> str:
        return hashlib.sha256(_canonical_json(self.profile).encode()).hexdigest()

    def graph_version(self, tileset_last_modified: int) -> str:
        return (
            "valhalla-"
            + hashlib.sha256(
                _canonical_json(
                    {
                        "engine_version": self.engine_version,
                        "tileset_last_modified": tileset_last_modified,
                        "source_sha256": self.graph["source_sha256"],
                        "source_edition": self.graph["source_edition"],
                    }
                ).encode()
            ).hexdigest()[:16]
        )


def _validate_config(values: Any) -> RoutingConfig:
    if not isinstance(values, dict):
        raise ConfigError("Routing config must be a JSON object")
    required_strings = (
        "routing_id",
        "engine",
        "engine_version",
        "engine_license_id",
        "engine_license_url",
        "container_image",
        "base_url",
    )
    for name in required_strings:
        if not isinstance(values.get(name), str) or not values[name].strip():
            raise ConfigError(f"Routing config {name} must be a non-empty string")
    if values["engine"] != "valhalla":
        raise ConfigError("Routing engine must be valhalla")

    allowed = values.get("allowed_hosts")
    denied = values.get("denied_hosts")
    if (
        not isinstance(allowed, list)
        or not allowed
        or any(not isinstance(host, str) or not host.strip() for host in allowed)
        or not isinstance(denied, list)
        or any(not isinstance(host, str) or not host.strip() for host in denied)
    ):
        raise ConfigError("Routing allowed_hosts and denied_hosts must be host lists")

    try:
        parsed = urlsplit(values["base_url"])
        host = parsed.hostname
        _ = parsed.port
    except ValueError as exc:
        raise ConfigError("Routing base_url is malformed") from exc
    allowed_hosts = {item.rstrip(".").lower() for item in allowed}
    denied_hosts = {item.rstrip(".").lower() for item in denied}
    normalized_host = (host or "").rstrip(".").lower()
    if parsed.username is not None or parsed.password is not None:
        raise ConfigError("Routing base_url must not contain userinfo")
    if parsed.scheme.lower() not in {"http", "https"} or not normalized_host:
        raise ConfigError("Routing base_url must use http(s) and include a hostname")
    if normalized_host in denied_hosts:
        raise ConfigError(
            "Public demo servers are not a production backend; use a self-hosted Valhalla service"
        )
    if normalized_host not in allowed_hosts:
        raise ConfigError("Routing base_url host must be explicitly allowed")

    graph = values.get("graph")
    if not isinstance(graph, dict):
        raise ConfigError("Routing graph must be an object")
    graph_strings = (
        "source_url",
        "source_sha256",
        "source_edition",
        "landing_url",
        "license_id",
        "license_url",
        "attribution",
    )
    for name in graph_strings:
        if not isinstance(graph.get(name), str) or not graph[name].strip():
            raise ConfigError(f"Routing graph {name} must be a non-empty string")
    if len(graph["source_sha256"]) != 64 or any(
        c not in "0123456789abcdef" for c in graph["source_sha256"]
    ):
        raise ConfigError("Routing graph source_sha256 must be a lowercase SHA-256 digest")
    bbox = graph.get("coverage_bbox")
    if (
        not isinstance(bbox, list)
        or len(bbox) != 4
        or any(not _finite_number(value) for value in bbox)
        or bbox[0] >= bbox[2]
        or bbox[1] >= bbox[3]
        or not -180 <= bbox[0] <= 180
        or not -180 <= bbox[2] <= 180
        or not -90 <= bbox[1] <= 90
        or not -90 <= bbox[3] <= 90
    ):
        raise ConfigError("Routing coverage_bbox must be [west,south,east,north] in WGS84")

    profile = values.get("profile")
    if not isinstance(profile, dict):
        raise ConfigError("Routing profile must be an object")
    if (
        not isinstance(profile.get("profile_id"), str)
        or not profile["profile_id"].strip()
        or profile.get("costing") != "pedestrian"
    ):
        raise ConfigError("Routing profile must define profile_id and pedestrian costing")
    costing_options = profile.get("costing_options")
    location_options = profile.get("location_options")
    isochrone = profile.get("isochrone")
    if not isinstance(costing_options, dict) or not costing_options:
        raise ConfigError("Routing profile costing_options must be an object")
    if not isinstance(location_options, dict) or not location_options:
        raise ConfigError("Routing profile location_options must be an object")
    if not isinstance(isochrone, dict) or not isochrone:
        raise ConfigError("Routing profile isochrone options must be an object")
    if any(not _positive_or_zero(value) for value in costing_options.values()):
        raise ConfigError("Routing costing options must be finite nonnegative numbers")
    for key, value in location_options.items():
        if (
            not isinstance(key, str)
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise ConfigError("Routing location options must be nonnegative finite numbers")
    for key in ("generalize", "denoise"):
        if key not in isochrone or not _positive_number(isochrone[key]):
            raise ConfigError(f"Routing isochrone {key} must be positive")

    limits = values.get("limits")
    required_limits = (
        "max_snap_distance_m",
        "max_route_straight_line_m",
        "max_isochrone_minutes",
        "max_contours",
        "max_requests",
        "max_concurrency",
        "timeout_seconds",
        "status_ttl_seconds",
        "budget_window_seconds",
        "max_response_bytes",
    )
    if not isinstance(limits, dict):
        raise ConfigError("Routing limits must be an object")
    for name in required_limits:
        value = limits.get(name)
        if not _positive_number(value):
            raise ConfigError(f"Routing limit {name} must be positive")
        if name not in {"timeout_seconds", "budget_window_seconds"} and type(value) is not int:
            raise ConfigError(f"Routing limit {name} must be an integer")

    manifest_path = graph.get("manifest_path")
    if manifest_path is not None and (
        not isinstance(manifest_path, str) or not manifest_path.strip()
    ):
        raise ConfigError("Routing graph manifest_path must be a non-empty path or null")

    for name in ("assumptions", "limitations"):
        items = values.get(name)
        if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
            raise ConfigError(f"Routing {name} must be a list of strings")

    return RoutingConfig(
        routing_id=values["routing_id"],
        engine=values["engine"],
        engine_version=values["engine_version"],
        engine_license_id=values["engine_license_id"],
        engine_license_url=values["engine_license_url"],
        container_image=values["container_image"],
        base_url=values["base_url"].rstrip("/"),
        allowed_hosts=tuple(allowed),
        denied_hosts=tuple(denied),
        graph=dict(graph),
        profile=dict(profile),
        limits=dict(limits),
        assumptions=tuple(values["assumptions"]),
        limitations=tuple(values["limitations"]),
    )


def _positive_or_zero(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and value >= 0
    )


def _finite_number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def load_routing_config(
    path: str | Path | None = None, env: Mapping[str, str] | None = None
) -> RoutingConfig:
    config_path = Path(path) if path is not None else DEFAULT_ROUTING_CONFIG
    try:
        values = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"Unable to load routing config: {config_path}") from exc
    values = dict(values) if isinstance(values, dict) else values
    if isinstance(values, dict):
        environment = env if env is not None else os.environ
        base_url = environment.get("VALHALLA_BASE_URL")
        manifest_path = environment.get("VALHALLA_GRAPH_MANIFEST")
        if base_url:
            values["base_url"] = base_url
        if manifest_path and isinstance(values.get("graph"), dict):
            values["graph"] = dict(values["graph"])
            values["graph"]["manifest_path"] = manifest_path
    return _validate_config(values)


@dataclass(frozen=True)
class Coordinate:
    lon: float
    lat: float

    def __post_init__(self) -> None:
        if (
            isinstance(self.lon, bool)
            or isinstance(self.lat, bool)
            or not isinstance(self.lon, (int, float))
            or not isinstance(self.lat, (int, float))
            or not math.isfinite(self.lon)
            or not math.isfinite(self.lat)
            or not -180 <= self.lon <= 180
            or not -90 <= self.lat <= 90
        ):
            raise ConnectorError("INVALID_COORDINATES", "Coordinates must be finite WGS84 lon,lat")

    def to_list(self) -> list[float]:
        return [float(self.lon), float(self.lat)]


def parse_coordinate(value: str, config: RoutingConfig | None = None) -> Coordinate:
    config = config or load_routing_config()
    if not isinstance(value, str) or len(value) > 64:
        raise ConnectorError(
            "INVALID_COORDINATES", "Coordinates must be formatted as lon,lat (WGS84)"
        )
    try:
        parts = value.split(",")
        if len(parts) != 2:
            raise ValueError
        coordinate = Coordinate(float(parts[0]), float(parts[1]))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ConnectorError(
            "INVALID_COORDINATES", "Coordinates must be formatted as lon,lat (WGS84)"
        ) from exc
    west, south, east, north = config.graph["coverage_bbox"]
    if not west <= coordinate.lon <= east or not south <= coordinate.lat <= north:
        raise ConnectorError(
            "COORDINATE_OUT_OF_COVERAGE",
            f"Coordinates must be lon,lat (WGS84) inside [{west}, {south}, {east}, {north}]",
        )
    return coordinate


class RouteStatus(str, Enum):
    OK = "ok"
    UNREACHABLE = "unreachable"
    INVALID_SNAP = "invalid_snap"


def _json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


@dataclass(frozen=True)
class WalkingRoute:
    status: RouteStatus | str
    origin: list[float]
    destination: list[float]
    snapped_origin: list[float] | None
    snapped_destination: list[float] | None
    snap_distance_m: dict[str, float | None]
    duration_s: float | None
    distance_m: float | None
    geometry: dict[str, Any] | None
    straight_line: dict[str, Any]
    routing: dict[str, Any]
    assumptions: tuple[str, ...]
    quality_flags: tuple[str, ...]
    failure: dict[str, str] | None
    computed_at: datetime
    cache_hit: bool = False
    schema_version: str = "1.0"
    kind: str = "walking_route"
    measure: str = "network_walking_estimate"

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["status"] = self.status.value if isinstance(self.status, Enum) else self.status
        return _json_value(value)


@dataclass(frozen=True)
class ReachableArea:
    status: RouteStatus | str
    origin: list[float]
    snapped_origin: list[float] | None
    snap_distance_m: float | None
    contours: tuple[dict[str, Any], ...]
    routing: dict[str, Any]
    assumptions: tuple[str, ...]
    quality_flags: tuple[str, ...]
    failure: dict[str, str] | None
    computed_at: datetime
    cache_hit: bool = False
    schema_version: str = "1.0"
    kind: str = "walking_reachable_area"
    measure: str = "network_walking_isochrone_estimate"

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["status"] = self.status.value if isinstance(self.status, Enum) else self.status
        return _json_value(value)


@dataclass(frozen=True)
class GraphInfo:
    engine_version: str
    tileset_last_modified: int
    graph_version: str
    graph_source_verified: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RoutingBudget:
    max_requests: int
    used: int = 0
    window_seconds: float | None = None
    clock: Callable[[], float | datetime] = time.monotonic
    window_start: float = field(init=False)

    def __post_init__(self) -> None:
        if type(self.max_requests) is not int or self.max_requests <= 0:
            raise ConfigError("max_requests must be a positive integer")
        if type(self.used) is not int or not 0 <= self.used <= self.max_requests:
            raise ConfigError("Routing budget usage must be within its limit")
        if self.window_seconds is not None and not _positive_number(self.window_seconds):
            raise ConfigError("window_seconds must be a positive number or null")
        if not callable(self.clock):
            raise ConfigError("clock must be callable")
        self.window_start = self._clock_seconds()

    def _clock_seconds(self) -> float:
        value = self.clock()
        return value.timestamp() if isinstance(value, datetime) else float(value)

    def _refresh_window(self) -> None:
        if self.window_seconds is None:
            return
        now = self._clock_seconds()
        if now - self.window_start >= self.window_seconds:
            self.used = 0
            self.window_start = now

    def consume(self) -> None:
        self._refresh_window()
        if self.used >= self.max_requests:
            raise ConnectorError(
                "ROUTING_BUDGET_EXCEEDED",
                f"Routing request budget exceeded: limit {self.max_requests}, used {self.used}",
                retryable=True,
            )
        self.used += 1

    def to_dict(self) -> dict[str, int | float | None]:
        self._refresh_window()
        return {
            "max_requests": self.max_requests,
            "used": self.used,
            "window_seconds": self.window_seconds,
        }
