"""Self-hosted Valhalla adapter for bounded walking estimates."""

import asyncio
import json
import math
import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

import httpx
from pyproj import Transformer
from shapely.geometry import shape
from shapely.ops import transform as transform_geometry

from app.domain.errors import ConnectorError, SourceUnavailableError
from app.domain.routing import (
    Coordinate,
    GraphInfo,
    ReachableArea,
    RouteStatus,
    RoutingBudget,
    RoutingConfig,
    WalkingRoute,
    parse_coordinate,
)
from app.geo.distance import straight_line_distance_m
from app.infrastructure.routing_cache import RoutingCache

_TO_EPSG_25833 = Transformer.from_crs("EPSG:4326", "EPSG:25833", always_xy=True)


def decode_polyline6(encoded: str) -> list[list[float]]:
    """Decode Valhalla's precision-6 encoded polyline to [lon, lat] positions."""
    if not isinstance(encoded, str):
        raise ValueError("encoded shape must be text")
    values: list[int] = []
    index = 0
    while index < len(encoded):
        result = shift = 0
        while True:
            if index >= len(encoded):
                raise ValueError("truncated encoded shape")
            byte = ord(encoded[index]) - 63
            index += 1
            if byte < 0 or byte > 63:
                raise ValueError("invalid encoded shape")
            result |= (byte & 0x1F) << shift
            shift += 5
            if byte < 0x20:
                break
            if shift > 60:
                raise ValueError("encoded shape integer is too large")
        values.append(~(result >> 1) if result & 1 else result >> 1)
    if len(values) < 2 or len(values) % 2:
        raise ValueError("encoded shape must contain at least one coordinate")
    lat = lon = 0
    coordinates = []
    for offset in range(0, len(values), 2):
        lat += values[offset]
        lon += values[offset + 1]
        coordinates.append([lon / 1_000_000, lat / 1_000_000])
    return coordinates


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _now_utc(clock: Any) -> datetime:
    value = clock() if clock is not None else datetime.now(timezone.utc)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    return datetime.fromtimestamp(float(value), timezone.utc)


def _clock_value(clock: Any) -> float:
    value = clock() if clock is not None else time.monotonic()
    return value.timestamp() if isinstance(value, datetime) else float(value)


def _finite_nonnegative(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and value >= 0
    )


def _iso_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class ValhallaWalkingClient:
    def __init__(
        self,
        config: RoutingConfig,
        cache: RoutingCache | None = None,
        budget: RoutingBudget | None = None,
        client: httpx.AsyncClient | None = None,
        clock: Any = None,
    ):
        self.config = config
        self.cache = cache
        self.budget = budget or RoutingBudget(config.limits["max_requests"])
        self._client = client
        self._owns_client = client is None
        self._clock = clock
        self._status_cached_at: float | None = None
        self._status_cache: GraphInfo | None = None
        self._semaphore = asyncio.Semaphore(config.limits["max_concurrency"])

    async def __aenter__(self) -> "ValhallaWalkingClient":
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.config.limits["timeout_seconds"], follow_redirects=False
            )
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _http_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.config.limits["timeout_seconds"], follow_redirects=False
            )
        return self._client

    def _base(self) -> str:
        return self.config.base_url.rstrip("/")

    async def _request_json(self, method: str, endpoint: str, **kwargs: Any) -> Any:
        try:
            response = await self._http_client().request(
                method,
                f"{self._base()}/{endpoint.lstrip('/')}",
                timeout=self.config.limits["timeout_seconds"],
                follow_redirects=False,
                **kwargs,
            )
        except (
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.RemoteProtocolError,
            OSError,
        ) as exc:
            raise SourceUnavailableError(
                "Valhalla request failed due to a connection error"
            ) from exc
        except asyncio.TimeoutError as exc:
            raise SourceUnavailableError("Valhalla request timed out") from exc
        if response.status_code >= 500:
            raise SourceUnavailableError(f"Valhalla returned HTTP {response.status_code}")
        if response.status_code not in {200, 400}:
            raise SourceUnavailableError(
                f"Valhalla returned HTTP {response.status_code}", retryable=False
            )
        try:
            body = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise SourceUnavailableError(
                "Valhalla returned a malformed non-JSON response", retryable=False
            ) from exc
        return response, body

    async def status(self, force: bool = False) -> GraphInfo:
        now = _clock_value(self._clock)
        ttl = self.config.limits["status_ttl_seconds"]
        if (
            not force
            and self._status_cache is not None
            and self._status_cached_at is not None
            and now - self._status_cached_at < ttl
        ):
            return self._status_cache
        response, body = await self._request_json("GET", "/status")
        if response.status_code != 200:
            raise SourceUnavailableError(
                f"Valhalla status returned HTTP {response.status_code}",
                retryable=response.status_code >= 500,
            )
        if (
            not isinstance(body, dict)
            or not isinstance(body.get("version"), str)
            or type(body.get("tileset_last_modified")) is not int
        ):
            raise SourceUnavailableError("Valhalla status response is malformed", retryable=False)
        version = body["version"]
        if version != self.config.engine_version:
            raise SourceUnavailableError(
                f"Valhalla engine version {version} does not match reviewed "
                f"{self.config.engine_version}",
                retryable=False,
            )
        graph_info = GraphInfo(
            engine_version=version,
            tileset_last_modified=body["tileset_last_modified"],
            graph_version=self.config.graph_version(body["tileset_last_modified"]),
        )
        self._status_cache = graph_info
        self._status_cached_at = now
        return graph_info

    def _coordinate(self, value: Coordinate | str | Sequence[float]) -> Coordinate:
        if isinstance(value, str):
            return parse_coordinate(value, self.config)
        if isinstance(value, Coordinate):
            coordinate = value
        else:
            try:
                coordinate = Coordinate(value[0], value[1])
            except (TypeError, IndexError) as exc:
                raise ConnectorError(
                    "INVALID_COORDINATES", "Coordinates must be finite WGS84 lon,lat"
                ) from exc
        west, south, east, north = self.config.graph["coverage_bbox"]
        if not west <= coordinate.lon <= east or not south <= coordinate.lat <= north:
            raise ConnectorError(
                "COORDINATE_OUT_OF_COVERAGE",
                f"Coordinates must be lon,lat (WGS84) inside [{west}, {south}, {east}, {north}]",
            )
        return coordinate

    def _routing_metadata(self, graph: GraphInfo) -> dict[str, Any]:
        return {
            "engine": self.config.engine,
            "engine_version": graph.engine_version,
            "graph_version": graph.graph_version,
            "tileset_last_modified": _utc_iso(
                datetime.fromtimestamp(graph.tileset_last_modified, timezone.utc)
            ),
            "source_url": self.config.graph["source_url"],
            "source_edition": self.config.graph["source_edition"],
            "source_sha256": self.config.graph["source_sha256"],
            "graph_license_id": self.config.graph["license_id"],
            "graph_attribution": self.config.graph["attribution"],
            "engine_license_id": self.config.engine_license_id,
            "profile_id": self.config.profile["profile_id"],
            "profile_hash": self.config.profile_hash,
        }

    def _make_route(
        self,
        origin: Coordinate,
        destination: Coordinate,
        straight_distance: float,
        graph: GraphInfo,
        *,
        status: RouteStatus,
        snapped_origin: list[float] | None = None,
        snapped_destination: list[float] | None = None,
        snap_distance_origin: float | None = None,
        snap_distance_destination: float | None = None,
        duration_s: float | None = None,
        distance_m: float | None = None,
        geometry: dict[str, Any] | None = None,
        failure: dict[str, str] | None = None,
        quality_flags: tuple[str, ...] = (),
    ) -> WalkingRoute:
        return WalkingRoute(
            status=status,
            origin=origin.to_list(),
            destination=destination.to_list(),
            snapped_origin=snapped_origin,
            snapped_destination=snapped_destination,
            snap_distance_m={
                "origin": snap_distance_origin,
                "destination": snap_distance_destination,
            },
            duration_s=duration_s,
            distance_m=distance_m,
            geometry=geometry,
            straight_line={
                "measure": "straight_line",
                "distance_m": straight_distance,
                "crs": "EPSG:25833",
                "duration_s": None,
            },
            routing=self._routing_metadata(graph),
            assumptions=self.config.assumptions,
            quality_flags=quality_flags,
            failure=failure,
            computed_at=_now_utc(self._clock),
        )

    @staticmethod
    def _route_from_dict(value: Mapping[str, Any]) -> WalkingRoute:
        return WalkingRoute(
            status=value["status"],
            origin=value["origin"],
            destination=value["destination"],
            snapped_origin=value["snapped_origin"],
            snapped_destination=value["snapped_destination"],
            snap_distance_m=value["snap_distance_m"],
            duration_s=value["duration_s"],
            distance_m=value["distance_m"],
            geometry=value["geometry"],
            straight_line=value["straight_line"],
            routing=value["routing"],
            assumptions=tuple(value["assumptions"]),
            quality_flags=tuple(value["quality_flags"]),
            failure=value["failure"],
            computed_at=_iso_datetime(value["computed_at"]),
            cache_hit=value.get("cache_hit", False),
        )

    async def route(
        self,
        origin: Coordinate | str | Sequence[float],
        destination: Coordinate | str | Sequence[float],
    ) -> WalkingRoute:
        origin, destination = self._coordinate(origin), self._coordinate(destination)
        straight_distance = straight_line_distance_m(origin, destination)
        if straight_distance > self.config.limits["max_route_straight_line_m"]:
            raise ConnectorError(
                "ROUTING_LIMIT_EXCEEDED",
                f"Route straight-line distance exceeds {self.config.limits['max_route_straight_line_m']} m",
            )
        graph = await self.status()
        coordinates = [origin.to_list(), destination.to_list()]
        if self.cache is not None:
            cached = self.cache.get(
                "walking_route",
                graph.graph_version,
                self.config.profile_hash,
                coordinates,
            )
            if cached is not None:
                return self._route_from_dict(cached)
        self.budget.consume()
        options = dict(self.config.profile["location_options"])
        locations = [
            {
                "lon": coordinate.lon,
                "lat": coordinate.lat,
                "search_cutoff": self.config.limits["max_snap_distance_m"],
                **options,
            }
            for coordinate in (origin, destination)
        ]
        request = {
            "locations": locations,
            "costing": self.config.profile["costing"],
            "costing_options": {
                self.config.profile["costing"]: dict(self.config.profile["costing_options"])
            },
            "units": "kilometers",
            "directions_type": "none",
        }
        async with self._semaphore:
            response, body = await self._request_json("POST", "/route", json=request)
        result: WalkingRoute
        if response.status_code == 200:
            try:
                trip = body["trip"]
                summary = trip["summary"]
                legs = trip["legs"]
                if not isinstance(legs, list) or not legs:
                    raise ValueError("route has no legs")
                leg_positions = [decode_polyline6(leg["shape"]) for leg in legs]
                positions = list(leg_positions[0])
                for leg in leg_positions[1:]:
                    positions.extend(leg[1:] if positions[-1] == leg[0] else leg)
                duration, length_km = summary["time"], summary["length"]
                if not _finite_nonnegative(duration) or not _finite_nonnegative(length_km):
                    raise ValueError("invalid route summary measures")
                snapped_origin, snapped_destination = positions[0], positions[-1]
                snap_origin = straight_line_distance_m(
                    origin, Coordinate(snapped_origin[0], snapped_origin[1])
                )
                snap_destination = straight_line_distance_m(
                    destination, Coordinate(snapped_destination[0], snapped_destination[1])
                )
            except (ConnectorError, KeyError, IndexError, TypeError, ValueError) as exc:
                raise SourceUnavailableError(
                    "Valhalla route response is malformed", retryable=False
                ) from exc
            if (
                snap_origin > self.config.limits["max_snap_distance_m"]
                or snap_destination > self.config.limits["max_snap_distance_m"]
            ):
                result = self._make_route(
                    origin,
                    destination,
                    straight_distance,
                    graph,
                    status=RouteStatus.INVALID_SNAP,
                    snapped_origin=snapped_origin,
                    snapped_destination=snapped_destination,
                    snap_distance_origin=snap_origin,
                    snap_distance_destination=snap_destination,
                    failure={
                        "code": "snap_distance_exceeded",
                        "message": "A snapped route endpoint exceeds the configured maximum snap distance",
                    },
                )
            else:
                if len(positions) == 1:
                    positions = positions * 2
                ferry = (
                    any(
                        isinstance(leg, dict)
                        and isinstance(leg.get("summary"), dict)
                        and leg["summary"].get("has_ferry") is True
                        for leg in legs
                    )
                    or summary.get("has_ferry") is True
                )
                result = self._make_route(
                    origin,
                    destination,
                    straight_distance,
                    graph,
                    status=RouteStatus.OK,
                    snapped_origin=snapped_origin,
                    snapped_destination=snapped_destination,
                    snap_distance_origin=snap_origin,
                    snap_distance_destination=snap_destination,
                    duration_s=float(duration),
                    distance_m=float(length_km) * 1000,
                    geometry={"type": "LineString", "coordinates": positions},
                    quality_flags=("includes_ferry",) if ferry else (),
                )
        elif response.status_code == 400:
            result = self._route_error(origin, destination, straight_distance, graph, body)
        else:
            raise SourceUnavailableError(
                f"Valhalla route returned HTTP {response.status_code}",
                retryable=response.status_code >= 500,
            )
        if self.cache is not None:
            self.cache.put(
                "walking_route",
                graph.graph_version,
                self.config.profile_hash,
                coordinates,
                result.to_dict(),
                _utc_iso(result.computed_at),
            )
        return result

    def _route_error(
        self,
        origin: Coordinate,
        destination: Coordinate,
        straight_distance: float,
        graph: GraphInfo,
        body: Any,
    ) -> WalkingRoute:
        if (
            not isinstance(body, dict)
            or type(body.get("error_code")) is not int
            or not isinstance(body.get("error"), str)
        ):
            raise SourceUnavailableError(
                "Valhalla route error response is malformed", retryable=False
            )
        code = body["error_code"]
        if 150 <= code <= 158:
            raise ConnectorError("ROUTING_LIMIT_EXCEEDED", body["error"])
        if code in {442, 170}:
            return self._make_route(
                origin,
                destination,
                straight_distance,
                graph,
                status=RouteStatus.UNREACHABLE,
                failure={"code": f"valhalla_{code}", "message": body["error"]},
            )
        if code == 171:
            return self._make_route(
                origin,
                destination,
                straight_distance,
                graph,
                status=RouteStatus.INVALID_SNAP,
                failure={"code": "valhalla_171", "message": body["error"]},
            )
        raise SourceUnavailableError(
            f"Valhalla route failed with error code {code}: {body['error']}", retryable=False
        )

    @staticmethod
    def _area_from_dict(value: Mapping[str, Any]) -> ReachableArea:
        return ReachableArea(
            status=value["status"],
            origin=value["origin"],
            snapped_origin=value["snapped_origin"],
            snap_distance_m=value["snap_distance_m"],
            contours=tuple(value["contours"]),
            routing=value["routing"],
            assumptions=tuple(value["assumptions"]),
            quality_flags=tuple(value["quality_flags"]),
            failure=value["failure"],
            computed_at=_iso_datetime(value["computed_at"]),
            cache_hit=value.get("cache_hit", False),
        )

    def _make_area(
        self,
        origin: Coordinate,
        graph: GraphInfo,
        *,
        status: RouteStatus,
        snapped_origin: list[float] | None = None,
        snap_distance: float | None = None,
        contours: tuple[dict[str, Any], ...] = (),
        failure: dict[str, str] | None = None,
    ) -> ReachableArea:
        return ReachableArea(
            status=status,
            origin=origin.to_list(),
            snapped_origin=snapped_origin,
            snap_distance_m=snap_distance,
            contours=contours,
            routing=self._routing_metadata(graph),
            assumptions=self.config.assumptions,
            quality_flags=(),
            failure=failure,
            computed_at=_now_utc(self._clock),
        )

    async def reachable_area(
        self, origin: Coordinate | str | Sequence[float], minutes: Sequence[int | float]
    ) -> ReachableArea:
        origin = self._coordinate(origin)
        if isinstance(minutes, (str, bytes)) or not isinstance(minutes, Sequence):
            raise ConnectorError("ROUTING_LIMIT_EXCEEDED", "Minutes must be a sequence of contours")
        if (
            not minutes
            or len(minutes) > self.config.limits["max_contours"]
            or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or not 1 <= value <= self.config.limits["max_isochrone_minutes"]
                for value in minutes
            )
        ):
            raise ConnectorError(
                "ROUTING_LIMIT_EXCEEDED",
                f"Contour minutes must be between 1 and {self.config.limits['max_isochrone_minutes']} "
                f"with at most {self.config.limits['max_contours']} contours",
            )
        ordered = sorted(minutes)
        if len(set(ordered)) != len(ordered):
            raise ConnectorError("ROUTING_LIMIT_EXCEEDED", "Contour minutes must be unique")
        graph = await self.status()
        minutes_key = list(ordered)
        coordinates = [origin.to_list()]
        if self.cache is not None:
            cached = self.cache.get(
                "walking_reachable_area",
                graph.graph_version,
                self.config.profile_hash,
                coordinates,
                minutes_key,
            )
            if cached is not None:
                return self._area_from_dict(cached)
        self.budget.consume()
        request = {
            "locations": [
                {
                    "lon": origin.lon,
                    "lat": origin.lat,
                    **dict(self.config.profile["location_options"]),
                }
            ],
            "costing": self.config.profile["costing"],
            "costing_options": {
                self.config.profile["costing"]: dict(self.config.profile["costing_options"])
            },
            "contours": [{"time": minute} for minute in ordered],
            "polygons": True,
            "denoise": self.config.profile["isochrone"]["denoise"],
            "generalize": self.config.profile["isochrone"]["generalize"],
            "show_locations": True,
        }
        async with self._semaphore:
            response, body = await self._request_json("POST", "/isochrone", json=request)
        if response.status_code == 400:
            result = self._area_error(origin, graph, body)
        elif response.status_code == 200:
            result = self._area_success(origin, graph, ordered, body)
        else:
            raise SourceUnavailableError(
                f"Valhalla isochrone returned HTTP {response.status_code}",
                retryable=response.status_code >= 500,
            )
        if self.cache is not None:
            self.cache.put(
                "walking_reachable_area",
                graph.graph_version,
                self.config.profile_hash,
                coordinates,
                result.to_dict(),
                _utc_iso(result.computed_at),
                minutes_key,
            )
        return result

    def _area_error(self, origin: Coordinate, graph: GraphInfo, body: Any) -> ReachableArea:
        if (
            not isinstance(body, dict)
            or type(body.get("error_code")) is not int
            or not isinstance(body.get("error"), str)
        ):
            raise SourceUnavailableError(
                "Valhalla isochrone error response is malformed", retryable=False
            )
        code = body["error_code"]
        if 150 <= code <= 158:
            raise ConnectorError("ROUTING_LIMIT_EXCEEDED", body["error"])
        if code in {442, 170}:
            status = RouteStatus.UNREACHABLE
            failure_code = f"valhalla_{code}"
        elif code == 171:
            status = RouteStatus.INVALID_SNAP
            failure_code = "valhalla_171"
        else:
            raise SourceUnavailableError(
                f"Valhalla isochrone failed with error code {code}: {body['error']}",
                retryable=False,
            )
        return self._make_area(
            origin,
            graph,
            status=status,
            failure={"code": failure_code, "message": body["error"]},
        )

    def _area_success(
        self, origin: Coordinate, graph: GraphInfo, requested: Sequence[int | float], body: Any
    ) -> ReachableArea:
        if not isinstance(body, dict) or not isinstance(body.get("features"), list):
            raise SourceUnavailableError(
                "Valhalla isochrone response is malformed", retryable=False
            )
        features = body["features"]
        snapped_features = [
            feature
            for feature in features
            if isinstance(feature, dict)
            and isinstance(feature.get("properties"), dict)
            and feature["properties"].get("type") == "snapped"
        ]
        snapped: list[float] | None = None
        if len(snapped_features) == 1:
            geometry = snapped_features[0].get("geometry")
            if (
                isinstance(geometry, dict)
                and geometry.get("type") == "MultiPoint"
                and isinstance(geometry.get("coordinates"), list)
                and geometry["coordinates"]
                and isinstance(geometry["coordinates"][0], list)
                and len(geometry["coordinates"][0]) >= 2
            ):
                snapped = geometry["coordinates"][0][:2]
        if snapped is None:
            return self._make_area(
                origin,
                graph,
                status=RouteStatus.INVALID_SNAP,
                failure={
                    "code": "no_snapped_location",
                    "message": "Valhalla did not snap the origin to the pedestrian network",
                },
            )
        try:
            snapped_coordinate = Coordinate(snapped[0], snapped[1])
            snap_distance = straight_line_distance_m(origin, snapped_coordinate)
        except (ConnectorError, TypeError, ValueError) as exc:
            raise SourceUnavailableError(
                "Valhalla isochrone snapped location is malformed", retryable=False
            ) from exc
        if snap_distance > self.config.limits["max_snap_distance_m"]:
            return self._make_area(
                origin,
                graph,
                status=RouteStatus.INVALID_SNAP,
                snapped_origin=snapped,
                snap_distance=snap_distance,
                failure={
                    "code": "snap_distance_exceeded",
                    "message": "The snapped origin exceeds the configured maximum snap distance",
                },
            )
        contours: dict[float, dict[str, Any]] = {}
        try:
            for feature in features:
                if not isinstance(feature, dict):
                    raise ValueError("invalid contour feature")
                properties = feature.get("properties", {})
                if not isinstance(properties, dict):
                    raise ValueError("invalid contour properties")
                if properties.get("type") == "snapped":
                    continue
                if properties.get("metric") != "time":
                    continue
                contour = properties.get("contour")
                geometry = feature.get("geometry")
                if (
                    isinstance(contour, bool)
                    or not isinstance(contour, (int, float))
                    or not isinstance(geometry, dict)
                    or geometry.get("type") not in {"Polygon", "MultiPolygon"}
                ):
                    raise ValueError("invalid contour feature")
                if contour in contours:
                    raise ValueError("duplicate contour feature")
                parsed_geometry = shape(geometry)
                projected = transform_geometry(_TO_EPSG_25833.transform, parsed_geometry)
                contours[contour] = {
                    "minutes": contour,
                    "geometry": geometry,
                    "area_m2": float(projected.area),
                }
        except Exception as exc:
            raise SourceUnavailableError(
                "Valhalla isochrone contour is malformed", retryable=False
            ) from exc
        if len(contours) != len(requested) or set(contours) != set(requested):
            raise SourceUnavailableError(
                "Valhalla did not return exactly one contour for every requested minute",
                retryable=False,
            )
        ordered_contours = tuple(contours[minute] for minute in sorted(contours))
        return self._make_area(
            origin,
            graph,
            status=RouteStatus.OK,
            snapped_origin=snapped,
            snap_distance=snap_distance,
            contours=ordered_contours,
        )
