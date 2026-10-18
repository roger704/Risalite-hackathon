"""Catchment geometry: 500 m buffer in EPSG:25833, walking isochrone only if Valhalla is set (F4)."""

from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from shapely.geometry import Point
from shapely.geometry.base import BaseGeometry

from app.application.t1.readers import TO_WGS84, metric_geometry, to_metric


@dataclass(frozen=True)
class Catchment:
    geometry: BaseGeometry
    kind: str
    radius_m: int | None
    minutes: float | None = None
    note: str | None = None

    @property
    def area_km2(self) -> float:
        return self.geometry.area / 1_000_000

    def describe(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "type": self.kind,
            "radius_m": self.radius_m,
            "minutes": self.minutes,
            "crs": "EPSG:25833",
            "area_km2": round(self.area_km2, 4),
        }
        if self.note:
            result["note"] = self.note
        return result

    def boundary_wgs84(self) -> list[list[float]]:
        exterior = getattr(self.geometry, "exterior", None)
        if exterior is None:
            return []
        lon, lat = TO_WGS84.transform(*exterior.xy)
        return [[round(a, 6), round(b, 6)] for a, b in zip(lon, lat)]


def buffer_catchment(lon: float, lat: float, radius_m: int) -> Catchment:
    if radius_m < 1:
        raise ValueError("radius_m must be at least 1 m")
    geometry = Point(*to_metric(lon, lat)).buffer(radius_m, quad_segs=64)
    return Catchment(geometry, "circle", radius_m, note="straight-line radius, not walking time")


def valhalla_url() -> str | None:
    value = os.environ.get("VALHALLA_URL", "").strip()
    return value or None


async def _isochrone(lon: float, lat: float, minutes: float, base_url: str) -> dict | None:
    from app.connectors.valhalla import ValhallaWalkingClient
    from app.domain.routing import load_routing_config

    env = {**os.environ, "VALHALLA_BASE_URL": base_url}
    config = load_routing_config(env=env)
    async with ValhallaWalkingClient(config) as client:
        area = await client.reachable_area([lon, lat], [minutes])
    if not area.contours:
        return None
    return area.contours[0]["geometry"]


def _run(coro) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def resolve_catchment(lon: float, lat: float, radius_m: int, minutes: float) -> Catchment:
    fallback = buffer_catchment(lon, lat, radius_m)
    base_url = valhalla_url()
    if base_url is None:
        return fallback
    try:
        geometry = _run(_isochrone(lon, lat, minutes, base_url))
    except Exception as exc:
        return Catchment(
            fallback.geometry,
            "circle",
            radius_m,
            note=f"walking isochrone failed ({type(exc).__name__}); straight-line radius used",
        )
    if geometry is None:
        return Catchment(
            fallback.geometry, "circle", radius_m, note="no isochrone contour; radius used"
        )
    return Catchment(metric_geometry(geometry), "walking_isochrone", None, minutes=minutes)
