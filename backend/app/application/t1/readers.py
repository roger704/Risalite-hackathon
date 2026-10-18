"""Data readers for the T1 point path: real snapshot readers plus an in-memory variant.

All geometry arguments are EPSG:25833; points are WGS84 lon/lat.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any, Protocol

import numpy as np
import pyarrow.parquet as pq
import shapely
from pyproj import Transformer
from shapely.geometry import Point, shape
from shapely.geometry.base import BaseGeometry
from shapely.strtree import STRtree

from app.application.address_search import AddressIndex
from app.domain import (
    Area,
    AreaObservation,
    GridObservation,
    NotFoundError,
    Place,
    StopService,
    TransitStop,
    record_from_dict,
)

TO_METRIC = Transformer.from_crs("EPSG:4326", "EPSG:25833", always_xy=True)
TO_WGS84 = Transformer.from_crs("EPSG:25833", "EPSG:4326", always_xy=True)
TRANSIT_REFERENCE_BUFFER_M = 400.0
YOUNG_ADULT_BANDS = ((18, 27), (27, 45))

DATASETS: dict[str, str] = {
    "addresses": "berlin_addresses",
    "boundary": "berlin_lor",
    "names": "berlin_lor_names",
    "population": "berlin_population",
    "census": "zensus_grid",
    "places": "osm_pois",
    "transit": "vbb_gtfs",
}

Windows = Mapping[str, tuple[str, str]]


@dataclass(frozen=True)
class Poi:
    record_id: str
    category: str
    lon: float
    lat: float
    name: str | None = None


@dataclass
class ReferencePlr:
    plr_id: str
    residents: float
    area_km2: float
    counts: dict[str, int] = field(default_factory=dict)
    young_adult_share: float | None = None
    transit: dict[str, float] | None = None
    centre_distance_m: float | None = None


class PointReaders(Protocol):
    def address_index(self) -> AddressIndex | None: ...

    def plr_at(self, lon: float, lat: float) -> dict[str, Any] | None: ...

    def plr_profile(self, plr_id: str) -> dict[str, Any]: ...

    def catchment_residents(self, catchment: BaseGeometry) -> dict[str, Any] | None: ...

    def pois(self, categories: Collection[str], catchment: BaseGeometry) -> list[Poi]: ...

    def transit_departures(
        self, catchment: BaseGeometry, windows: Windows
    ) -> dict[str, float] | None: ...

    def centre_distance_m(self, lon: float, lat: float) -> float | None: ...

    def berlin_reference(self, windows: Windows) -> list[ReferencePlr]: ...

    def source(self, role: str) -> dict[str, str] | None: ...


def to_metric(lon: float, lat: float) -> tuple[float, float]:
    x, y = TO_METRIC.transform(lon, lat)
    return float(x), float(y)


def metric_geometry(geometry: Mapping[str, Any]) -> BaseGeometry:
    return shapely.transform(shape(geometry), TO_METRIC.transform, interleaved=False)


def _xy(points: Iterable[tuple[float, float]]) -> tuple[np.ndarray, np.ndarray]:
    values = np.array(list(points), dtype=float).reshape(-1, 2)
    if not len(values):
        return np.empty(0), np.empty(0)
    x, y = TO_METRIC.transform(values[:, 0], values[:, 1])
    return np.asarray(x), np.asarray(y)


def _inside(x: np.ndarray, y: np.ndarray, geometry: BaseGeometry) -> np.ndarray:
    if not len(x):
        return np.empty(0, dtype=int)
    minx, miny, maxx, maxy = geometry.bounds
    candidates = np.flatnonzero((x >= minx) & (x <= maxx) & (y >= miny) & (y <= maxy))
    shapely.prepare(geometry)
    return candidates[shapely.intersects_xy(geometry, x[candidates], y[candidates])]


def _window_key(windows: Windows, start: str, end: str) -> str | None:
    return next((name for name, span in windows.items() if tuple(span) == (start, end)), None)


class SnapshotReaders:
    """Reads the active published snapshots of a `DataService` (no provider calls)."""

    def __init__(self, service: Any):
        self.service = service

    def _manifest(self, role: str):
        dataset_id = DATASETS.get(role)
        if dataset_id is None or dataset_id not in self.service.configs:
            return None
        try:
            manifest = self.service.catalog.get(dataset_id)
        except NotFoundError:
            return None
        return manifest if self.service._snapshot_available(manifest) else None

    def _records(self, role: str) -> list[Any]:
        manifest = self._manifest(role)
        if manifest is None:
            return []
        path = self.service._snapshot_path(manifest)
        values = pq.read_table(path, columns=["record_json"]).column("record_json").to_pylist()
        return [record_from_dict(json.loads(value)) for value in values]

    def source(self, role: str) -> dict[str, str] | None:
        manifest = self._manifest(role)
        if manifest is None:
            return None
        return {
            "dataset_id": manifest.dataset_id,
            "snapshot_id": manifest.snapshot_id,
            "source_url": manifest.landing_url,
            "reference_period": manifest.reference_period or manifest.source_edition or "",
        }

    @cached_property
    def _address_index(self) -> AddressIndex | None:
        manifest = self._manifest("addresses")
        if manifest is None:
            return None
        return AddressIndex.from_parquet(self.service._snapshot_path(manifest))

    def address_index(self) -> AddressIndex | None:
        return self._address_index

    @cached_property
    def _plrs(self) -> dict[str, Any]:
        names = {
            record.area_code: record.name
            for record in self._records("names")
            if isinstance(record, Area)
        }
        areas = sorted(
            (
                record
                for record in self._records("boundary")
                if isinstance(record, Area) and record.hierarchy == "planungsraum"
            ),
            key=lambda record: record.area_code,
        )
        polygons = [metric_geometry(area.geometry) for area in areas]
        return {
            "ids": [area.area_code for area in areas],
            "names": [names.get(area.area_code) or area.name or None for area in areas],
            "polygons": polygons,
            "tree": STRtree(polygons) if polygons else None,
        }

    def plr_at(self, lon: float, lat: float) -> dict[str, Any] | None:
        plrs = self._plrs
        if plrs["tree"] is None:
            return None
        point = Point(*to_metric(lon, lat))
        hits = sorted(int(i) for i in plrs["tree"].query(point, predicate="covered_by"))
        if not hits:
            return None
        return {"plr_id": plrs["ids"][hits[0]], "name": plrs["names"][hits[0]]}

    @cached_property
    def _population(self) -> dict[str, dict[str, float | None]]:
        result: dict[str, dict[str, float | None]] = {}
        for record in self._records("population"):
            if not isinstance(record, AreaObservation) or record.hierarchy != "planungsraum":
                continue
            row = result.setdefault(record.area_code, {"total": None, "young": 0.0})
            if record.metric_code == "population_total":
                row["total"] = record.value
            elif record.metric_code == "population_age_band" and (
                (record.age_min, record.age_max_exclusive) in YOUNG_ADULT_BANDS
            ):
                if record.value is None or row["young"] is None:
                    row["young"] = None
                else:
                    row["young"] = float(row["young"]) + record.value
        return result

    def plr_profile(self, plr_id: str) -> dict[str, Any]:
        row = self._population.get(plr_id)
        if not row or not row["total"] or row["young"] is None:
            return {"residents": row["total"] if row else None, "young_adult_share": None}
        return {"residents": row["total"], "young_adult_share": row["young"] / row["total"]}

    @cached_property
    def _grid(self) -> dict[str, Any]:
        cells = [
            record for record in self._records("census") if isinstance(record, GridObservation)
        ]
        x, y = _xy(shape(cell.geometry).centroid.coords[0] for cell in cells)
        return {"cells": cells, "x": x, "y": y}

    def catchment_residents(self, catchment: BaseGeometry) -> dict[str, Any] | None:
        from app.geo.zensus_catchment import aggregate_grid

        grid = self._grid
        if not grid["cells"]:
            return None
        nearby = _inside(grid["x"], grid["y"], catchment.buffer(150))
        if not len(nearby):
            return {"value": 0.0, "complete": False, "method": "no published census cells"}
        result = aggregate_grid((grid["cells"][int(i)] for i in nearby), catchment)
        return {
            "value": float(result["intersected_estimate"]["value"]),
            "complete": bool(result["bounds"]["complete"]),
            "method": result["intersected_estimate"]["method"],
            "reference_period": result["reference_period"],
            "warnings": result["warnings"],
        }

    @cached_property
    def _places(self) -> dict[str, Any]:
        places = sorted(
            (
                record
                for record in self._records("places")
                if isinstance(record, Place) and record.canonical_category
            ),
            key=lambda record: record.record_id,
        )
        x, y = _xy(tuple(place.geometry["coordinates"][:2]) for place in places)
        categories = np.array([place.canonical_category for place in places], dtype=object)
        return {"records": places, "x": x, "y": y, "categories": categories}

    def pois(self, categories: Collection[str], catchment: BaseGeometry) -> list[Poi]:
        places = self._places
        wanted = set(categories)
        result = []
        for i in _inside(places["x"], places["y"], catchment):
            record = places["records"][int(i)]
            if record.canonical_category in wanted:
                lon, lat = record.geometry["coordinates"][:2]
                result.append(
                    Poi(record.record_id, record.canonical_category, lon, lat, record.name)
                )
        return result

    @cached_property
    def _transit(self) -> dict[str, Any]:
        stops: dict[str, TransitStop] = {}
        services: dict[str, list[StopService]] = {}
        for record in self._records("transit"):
            if isinstance(record, TransitStop):
                stops[record.stop_id] = record
            elif isinstance(record, StopService):
                services.setdefault(record.stop_id, []).append(record)
        located = sorted(key for key in services if key in stops)
        x, y = _xy(tuple(stops[key].geometry["coordinates"][:2]) for key in located)
        return {"ids": located, "services": services, "x": x, "y": y, "available": bool(services)}

    def _departures(self, geometry: BaseGeometry, windows: Windows) -> dict[str, float]:
        transit = self._transit
        totals = {name: 0.0 for name in windows}
        for i in _inside(transit["x"], transit["y"], geometry):
            for service in transit["services"][transit["ids"][int(i)]]:
                name = _window_key(windows, service.window_start, service.window_end)
                if name is not None:
                    totals[name] += service.departures
        return totals

    def transit_departures(
        self, catchment: BaseGeometry, windows: Windows
    ) -> dict[str, float] | None:
        if not self._transit["available"]:
            return None
        return self._departures(catchment, windows)

    def centre_distance_m(self, lon: float, lat: float) -> float | None:
        return None

    def berlin_reference(self, windows: Windows) -> list[ReferencePlr]:
        key = tuple(sorted((name, tuple(span)) for name, span in windows.items()))
        cache = self.__dict__.setdefault("_reference_cache", {})
        if key not in cache:
            cache[key] = self._build_reference(windows)
        return cache[key]

    def _build_reference(self, windows: Windows) -> list[ReferencePlr]:
        plrs = self._plrs
        places = self._places
        transit = self._transit["available"]
        result = []
        for plr_id, polygon in zip(plrs["ids"], plrs["polygons"]):
            profile = self.plr_profile(plr_id)
            if profile["residents"] is None:
                continue
            hits = _inside(places["x"], places["y"], polygon)
            counts: dict[str, int] = {}
            for category in places["categories"][hits]:
                counts[category] = counts.get(category, 0) + 1
            result.append(
                ReferencePlr(
                    plr_id=plr_id,
                    residents=float(profile["residents"]),
                    area_km2=polygon.area / 1_000_000,
                    counts=counts,
                    young_adult_share=profile["young_adult_share"],
                    transit=self._departures(polygon.buffer(TRANSIT_REFERENCE_BUFFER_M), windows)
                    if transit
                    else None,
                )
            )
        return result


@dataclass
class StaticReaders:
    """In-memory readers (tests, fixtures, fake mode).

    `plrs`: [{"plr_id", "name", "geometry" (WGS84 GeoJSON), "young_adult_share"?}];
    `residents`: [(lon, lat, persons)] point weights summed inside the catchment;
    `stations`: [{"lon", "lat", "departures": {window: n}}].
    """

    addresses: list[dict[str, Any]] = field(default_factory=list)
    plrs: list[dict[str, Any]] = field(default_factory=list)
    residents: list[tuple[float, float, float]] | None = field(default_factory=list)
    places: list[Poi] = field(default_factory=list)
    stations: list[dict[str, Any]] | None = None
    reference: list[ReferencePlr] = field(default_factory=list)
    centres: list[tuple[float, float]] | None = None
    sources: dict[str, dict[str, str]] = field(default_factory=dict)

    def address_index(self) -> AddressIndex | None:
        if not self.addresses:
            return None
        return AddressIndex(enumerate(self.addresses))

    def plr_at(self, lon: float, lat: float) -> dict[str, Any] | None:
        point = Point(*to_metric(lon, lat))
        for plr in sorted(self.plrs, key=lambda item: item["plr_id"]):
            if metric_geometry(plr["geometry"]).covers(point):
                return {"plr_id": plr["plr_id"], "name": plr.get("name")}
        return None

    def plr_profile(self, plr_id: str) -> dict[str, Any]:
        plr = next((item for item in self.plrs if item["plr_id"] == plr_id), {})
        return {
            "residents": plr.get("residents"),
            "young_adult_share": plr.get("young_adult_share"),
        }

    def catchment_residents(self, catchment: BaseGeometry) -> dict[str, Any] | None:
        if self.residents is None:
            return None
        x, y = _xy((lon, lat) for lon, lat, _ in self.residents)
        hits = _inside(x, y, catchment)
        return {
            "value": float(sum(self.residents[int(i)][2] for i in hits)),
            "complete": True,
            "method": "static_points",
        }

    def pois(self, categories: Collection[str], catchment: BaseGeometry) -> list[Poi]:
        wanted = [place for place in self.places if place.category in set(categories)]
        x, y = _xy((place.lon, place.lat) for place in wanted)
        return [wanted[int(i)] for i in _inside(x, y, catchment)]

    def transit_departures(
        self, catchment: BaseGeometry, windows: Windows
    ) -> dict[str, float] | None:
        if self.stations is None:
            return None
        x, y = _xy((station["lon"], station["lat"]) for station in self.stations)
        totals = {name: 0.0 for name in windows}
        for i in _inside(x, y, catchment):
            for name, value in self.stations[int(i)].get("departures", {}).items():
                if name in totals:
                    totals[name] += value
        return totals

    def centre_distance_m(self, lon: float, lat: float) -> float | None:
        if not self.centres:
            return None
        x, y = to_metric(lon, lat)
        return min(
            float(np.hypot(cx - x, cy - y)) for cx, cy in (to_metric(*c) for c in self.centres)
        )

    def berlin_reference(self, windows: Windows) -> list[ReferencePlr]:
        return list(self.reference)

    def source(self, role: str) -> dict[str, str] | None:
        return self.sources.get(role)
