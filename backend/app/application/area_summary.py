"""Frontend-ready PLR summary assembled from pinned, compatible local snapshots.

Every value carries its source snapshot(s), unit and status; derived values also carry a
calculation id and version. Spatial joins and distances are computed in EPSG:25833.
Nothing here ranks areas or estimates footfall, rent, revenue, vacancy or confidence.
"""

import json
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow.parquet as pq
import shapely
from pyproj import Transformer
from shapely.geometry import shape

from app.domain import (
    Business,
    ConnectorError,
    IncompatibleSnapshotError,
    NotFoundError,
    Place,
    StopService,
    TransitStop,
    manifest_to_dict,
    record_from_dict,
)
from app.infrastructure.storage import read_record_page

SUMMARY_SCHEMA_VERSION = "1.0"
CALCULATION_VERSION = "area_summary_v1"
METRIC_CRS = "EPSG:25833"
CATEGORY_DIR = Path(__file__).resolve().parents[2] / "config" / "categories"
MAX_LISTED_ITEMS = 200
ESTIMATE_FLAG = "frequency_headway_estimate"

# role -> (default dataset id, request parameter, requires pinning to the LOR snapshot)
ROLES: dict[str, tuple[str, str, bool]] = {
    "boundary": ("berlin_lor", "lor_snapshot_id", False),
    "names": ("berlin_lor_names", "names_snapshot_id", True),
    "population": ("berlin_population", "population_snapshot_id", True),
    "places": ("osm_pois", "osm_snapshot_id", False),
    "businesses": ("ihk_businesses", "ihk_snapshot_id", True),
    "transit": ("vbb_gtfs", "gtfs_snapshot_id", False),
}
SNAPSHOT_PARAMETERS = tuple(parameter for _, parameter, _ in ROLES.values())

# Later connectors are added here once they publish snapshots; they never block a summary.
NOT_INTEGRATED = (
    "retail_centers",
    "planning_context",
    "households",
    "population_forecast",
    "routed_walking_access",
    "measured_daytime_population",
)
NOT_PROVIDED = ("ranking", "score", "footfall", "rent", "revenue", "vacancy", "confidence")
SUMMARY_LIMITATIONS = (
    "OSM places and IHK businesses are separate inventories; report their counts side by side "
    "and never add them.",
    "An empty or zero count only describes the pinned extraction, not the real world.",
    "Scheduled departures measure timetable supply, not passengers or footfall.",
    "Straight-line distances are not walking times; no routed distance is computed.",
    "IHK employee size classes are bounds of registered businesses, not headcounts or "
    "measured daytime population.",
    "Registered residents are counted at their principal residence; they are not daytime "
    "population or customer demand.",
    "This endpoint produces no ranking, score, footfall, rent, revenue, vacancy or confidence "
    "figure.",
)

_TO_METRIC = Transformer.from_crs("EPSG:4326", METRIC_CRS, always_xy=True)
_TO_WGS84 = Transformer.from_crs(METRIC_CRS, "EPSG:4326", always_xy=True)


def _calculation(calculation_id: str, method: str) -> dict[str, str]:
    return {"id": calculation_id, "version": CALCULATION_VERSION, "method": method}


def _field(
    value: Any,
    status: str,
    unit: str | None,
    sources: list[dict[str, str]],
    *,
    reference_period: str | None = None,
    calculation: dict[str, str] | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    if status not in {"observed", "estimated", "derived"} and value is not None:
        raise ValueError("Only observed, estimated and derived fields carry values")
    result = {
        "value": value,
        "status": status,
        "unit": unit,
        "sources": sources,
        "reference_period": reference_period,
        "calculation": calculation,
    }
    if note:
        result["note"] = note
    return result


@dataclass(frozen=True)
class _Resolved:
    role: str
    dataset_id: str
    status: str
    pinned_by: str | None = None
    manifest: Any = None
    reason: str | None = None

    @property
    def usable(self) -> bool:
        return self.status == "used"

    @property
    def source(self) -> dict[str, str]:
        return {"dataset_id": self.dataset_id, "snapshot_id": self.manifest.snapshot_id}

    def describe(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "role": self.role,
            "dataset_id": self.dataset_id,
            "status": self.status,
            "pinned_by": self.pinned_by,
            "snapshot_id": self.manifest.snapshot_id if self.manifest else None,
        }
        if self.reason:
            result["reason"] = self.reason
        if self.manifest is not None:
            data = manifest_to_dict(self.manifest)
            result.update(
                {
                    key: data[key]
                    for key in (
                        "source_edition",
                        "reference_period",
                        "valid_from",
                        "valid_to",
                        "boundary_edition",
                        "retrieved_at",
                        "adapter_version",
                        "landing_url",
                        "resource_url",
                        "license_id",
                        "license_url",
                        "attribution",
                        "parent_snapshot_ids",
                    )
                }
            )
            result["quality_warnings"] = data["quality_report"]["warnings"]
        return result


class AreaSummaryService:
    """Reads only published local snapshots. Per-snapshot point indexes are cached because
    snapshots are immutable."""

    def __init__(self, data_service, cache_size: int = 6):
        self.data = data_service
        self._cache: OrderedDict[tuple[str, str], Any] = OrderedDict()
        self._cache_size = cache_size
        self._lock = threading.Lock()
        self._building: dict[tuple[str, str], threading.Lock] = {}
        self._mappings: dict[tuple[str, str], tuple[str | None, frozenset[str] | None]] = {}

    # Snapshot resolution -------------------------------------------------------------

    def _resolve(self, role: str, requested: str | None) -> _Resolved:
        dataset_id, parameter, _ = ROLES[role]
        pinned_by = "request" if requested else "active_snapshot"
        if dataset_id not in self.data.configs:
            if requested:
                raise NotFoundError(f"Unknown dataset: {dataset_id}", "UNKNOWN_DATASET")
            return _Resolved(role, dataset_id, "not_registered")
        try:
            manifest = self.data.catalog.get(dataset_id, requested)
        except NotFoundError as exc:
            if requested:
                raise NotFoundError(
                    f"Unknown {parameter} for {dataset_id}", "SNAPSHOT_NOT_FOUND"
                ) from exc
            return _Resolved(role, dataset_id, "missing", reason="No snapshot is published")
        if not self.data._snapshot_available(manifest):
            if requested:
                raise ConnectorError(
                    "DATA_UNAVAILABLE", f"Pinned {dataset_id} snapshot is not usable", True
                )
            return _Resolved(
                role,
                dataset_id,
                "unavailable",
                pinned_by,
                manifest,
                "Snapshot is invalid or its normalized asset is missing",
            )
        return _Resolved(role, dataset_id, "used", pinned_by, manifest)

    def _check_parent(self, resolved: _Resolved, boundary: _Resolved) -> _Resolved:
        manifest = resolved.manifest
        if boundary.manifest.snapshot_id in manifest.parent_snapshot_ids and (
            manifest.boundary_edition == boundary.manifest.boundary_edition
        ):
            return resolved
        reason = (
            f"{resolved.dataset_id} snapshot {manifest.snapshot_id} is pinned to "
            f"{list(manifest.parent_snapshot_ids) or 'no'} LOR snapshot(s), not "
            f"{boundary.manifest.snapshot_id}"
        )
        if resolved.pinned_by == "request":
            raise IncompatibleSnapshotError(reason)
        return _Resolved(
            resolved.role, resolved.dataset_id, "incompatible", resolved.pinned_by, manifest, reason
        )

    # Cached snapshot readers ---------------------------------------------------------

    def _cached(self, manifest, builder):
        key = (manifest.dataset_id, manifest.snapshot_id)
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
            build_lock = self._building.setdefault(key, threading.Lock())
        with build_lock:
            with self._lock:
                if key in self._cache:
                    self._cache.move_to_end(key)
                    return self._cache[key]
            # On failure the key's lock is kept so queued waiters and new callers share it.
            value = builder(self.data._snapshot_path(manifest))
            with self._lock:
                self._cache[key] = value
                while len(self._cache) > self._cache_size:
                    self._cache.popitem(last=False)
                self._building.pop(key, None)
        return value

    def _snapshot_mapping(self, resolved: _Resolved):
        """Category mapping recorded on the snapshot's records, not the current config."""
        current = _category_mapping(self.data.configs.get(resolved.dataset_id))
        if not resolved.usable:
            return current
        key = (resolved.dataset_id, resolved.manifest.snapshot_id)
        with self._lock:
            if key in self._mappings:
                return self._mappings[key]
        version = None
        parquet = pq.ParquetFile(self.data._snapshot_path(resolved.manifest))
        for batch in parquet.iter_batches(batch_size=1, columns=["record_json"]):
            rows = batch.column(0).to_pylist()
            if rows:
                version = json.loads(rows[0]).get("category_mapping_version")
                break
        mapping = current if version is None else _load_mapping(version)
        with self._lock:
            self._mappings[key] = mapping
        return mapping

    def _area_records(self, manifest, area_code: str):
        path = self.data._snapshot_path(manifest)
        records, offset = [], 0
        while True:
            total, page = read_record_page(path, area_code=area_code, offset=offset, limit=1000)
            records.extend(page)
            offset += len(page)
            if offset >= total or not page:
                return records

    @staticmethod
    def _decode_all(path: Path):
        values = pq.read_table(path, columns=["record_json"]).column("record_json").to_pylist()
        return [record_from_dict(json.loads(value)) for value in values]

    @classmethod
    def _place_index(cls, path: Path):
        places = sorted(
            (record for record in cls._decode_all(path) if isinstance(record, Place)),
            key=lambda record: record.record_id,
        )
        return _points(places)

    @classmethod
    def _transit_index(cls, path: Path):
        stops, services = {}, {}
        for record in cls._decode_all(path):
            if isinstance(record, TransitStop):
                stops[record.stop_id] = record
            elif isinstance(record, StopService):
                services.setdefault(record.stop_id, []).append(record)
        located = sorted(
            (stops[key] for key in services if key in stops), key=lambda item: item.stop_id
        )
        return {
            "stops": _points(sorted(stops.values(), key=lambda item: item.stop_id)),
            "stations": _points(located),
            "services": services,
            "unlocated_station_keys": len(set(services) - set(stops)),
        }

    # Summary -------------------------------------------------------------------------

    def summary(
        self,
        area_code: str,
        *,
        category: str = "coffee_shop",
        include_geometry: bool = True,
        snapshot_ids: dict[str, str | None] | None = None,
    ) -> dict[str, Any]:
        snapshot_ids = snapshot_ids or {}
        unknown = set(snapshot_ids) - set(SNAPSHOT_PARAMETERS)
        if unknown:
            raise ConnectorError("UNSUPPORTED_FILTER", "Unknown snapshot parameters")
        requested = {role: snapshot_ids.get(parameter) for role, (_, parameter, _) in ROLES.items()}
        configured = {
            category
            for role in ("places", "businesses")
            for category in _category_mapping(self.data.configs.get(ROLES[role][0]))[1] or ()
        }

        boundary = self._resolve("boundary", requested["boundary"])
        if not boundary.usable:
            raise ConnectorError(
                "DATA_UNAVAILABLE",
                "A usable berlin_lor boundary snapshot is required for area summaries",
                True,
            )
        resolved = {"boundary": boundary}
        for role, (_, _, needs_parent) in ROLES.items():
            if role == "boundary":
                continue
            item = self._resolve(role, requested[role])
            resolved[role] = (
                self._check_parent(item, boundary) if needs_parent and item.usable else item
            )
        mappings = {
            role: self._snapshot_mapping(resolved[role]) for role in ("places", "businesses")
        }
        if category not in configured and not any(
            categories is None or category in categories for _, categories in mappings.values()
        ):
            raise ConnectorError(
                "UNSUPPORTED_CATEGORY", "category is not defined by any configured mapping"
            )

        areas = [
            record
            for record in self._area_records(boundary.manifest, area_code)
            if record.record_type == "area"
        ]
        if not areas:
            raise NotFoundError(
                f"Area {area_code} is not in the pinned boundary snapshot", "AREA_NOT_FOUND"
            )
        area = areas[0]
        businesses = resolved["businesses"]
        business_records = (
            sorted(
                (
                    r
                    for r in self._area_records(businesses.manifest, area_code)
                    if isinstance(r, Business)
                ),
                key=lambda r: r.record_id,
            )
            if businesses.usable
            else []
        )
        polygon = shapely.transform(shape(area.geometry), _TO_METRIC.transform, interleaved=False)
        warnings = [
            f"{item.dataset_id}: {item.status}" + (f" ({item.reason})" if item.reason else "")
            for item in resolved.values()
            if not item.usable
        ]
        return {
            "schema_version": SUMMARY_SCHEMA_VERSION,
            "summary_type": "area_summary",
            "calculation_version": CALCULATION_VERSION,
            "metric_crs": METRIC_CRS,
            "geometry_crs": "EPSG:4326",
            "parameters": {"area_code": area_code, "category": category},
            "area": self._area(area, polygon, boundary, resolved["names"], include_geometry),
            "residents": self._residents(area_code, polygon, boundary, resolved["population"]),
            "competitors": self._competitors(
                polygon, category, resolved, mappings, business_records
            ),
            "workplaces": self._workplaces(businesses, business_records),
            "transit": self._transit(polygon, resolved["transit"]),
            "snapshots": [item.describe() for item in resolved.values()],
            "not_integrated": list(NOT_INTEGRATED),
            "not_provided": list(NOT_PROVIDED),
            "limitations": list(SUMMARY_LIMITATIONS),
            "warnings": warnings,
        }

    def _area(self, area, polygon, boundary, names, include_geometry):
        name = attributes = None
        if names.usable:
            matches = self._area_records(names.manifest, area.area_code)
            if matches:
                name, attributes = matches[0].name, dict(sorted(matches[0].attributes.items()))
        name_status = "observed" if name else "missing"
        return {
            "area_code": area.area_code,
            "hierarchy": area.hierarchy,
            "boundary_edition": area.boundary_edition,
            "name": _field(
                name,
                name_status,
                None,
                [names.source] if names.manifest is not None else [],
                reference_period=names.manifest.source_edition if names.manifest else None,
                note=None if name else "Official LOR name is unavailable; do not invent one.",
            ),
            "official_attributes": attributes,
            "area_km2": _field(
                round(polygon.area / 1_000_000, 4),
                "derived",
                "km2",
                [boundary.source],
                reference_period=boundary.manifest.source_edition,
                calculation=_calculation("planar_area", f"Polygon area in {METRIC_CRS}"),
            ),
            "boundary": area.geometry if include_geometry else None,
            "boundary_source": boundary.source,
        }

    def _residents(self, area_code, polygon, boundary, population):
        base = {
            "status": population.status,
            "measures": "registered residents (principal residence)",
        }
        if not population.usable:
            return {**base, "reference_date": None, "total": None, "age_bands": [], "density": None}
        source = [population.source]
        observations = [
            record
            for record in self._area_records(population.manifest, area_code)
            if record.record_type == "area_observation"
        ]
        total = next((r for r in observations if r.metric_code == "population_total"), None)
        if total is None:
            return {
                **base,
                "status": "missing",
                "reference_date": population.manifest.reference_period,
                "total": _field(None, "missing", "persons", source, note="Area not in snapshot"),
                "age_bands": [],
                "density": None,
            }
        status = str(total.value_status.value)
        total_field = _field(
            total.value, status, total.unit, source, reference_period=total.reference_period
        )
        bands = []
        for band in sorted(
            (r for r in observations if r.metric_code == "population_age_band"),
            key=lambda r: r.age_min or 0,
        ):
            band_status = str(band.value_status.value)
            count = _field(
                band.value, band_status, band.unit, source, reference_period=band.reference_period
            )
            share_calc = _calculation("age_band_share", "age band count / population_total")
            if band.value is None or total.value is None:
                missing = band_status if band.value is None else status
                share = _field(None, missing, "ratio", source, calculation=share_calc)
            elif total.value == 0:
                share = _field(
                    None,
                    "not_applicable",
                    "ratio",
                    source,
                    calculation=share_calc,
                    note="population_total is zero",
                )
            else:
                share = _field(
                    round(band.value / total.value, 4),
                    "derived",
                    "ratio",
                    source,
                    reference_period=band.reference_period,
                    calculation=share_calc,
                )
            bands.append(
                {
                    "age_min": band.age_min,
                    "age_max_exclusive": band.age_max_exclusive,
                    "count": count,
                    "share": share,
                }
            )
        density_calc = _calculation(
            "residents_per_km2", f"population_total / planar area in {METRIC_CRS}"
        )
        density_sources = [*source, boundary.source]
        density = (
            _field(
                round(total.value / (polygon.area / 1_000_000), 1),
                "derived",
                "persons/km2",
                density_sources,
                reference_period=total.reference_period,
                calculation=density_calc,
            )
            if total.value is not None and polygon.area > 0
            else _field(None, status, "persons/km2", density_sources, calculation=density_calc)
        )
        incomplete = status != "observed" or any(b["count"]["status"] != "observed" for b in bands)
        return {
            **base,
            "status": "partial" if incomplete else "available",
            "reference_date": total.reference_period,
            "total": total_field,
            "age_bands": bands,
            "density": density,
        }

    def _competitors(self, polygon, category, resolved, mappings, business_records):
        sources = []
        places = resolved["places"]
        version, categories = mappings["places"]
        entry = {
            "dataset_id": places.dataset_id,
            "inventory": "mapped_places",
            "status": places.status,
        }
        if places.usable and categories is not None and category not in categories:
            entry["status"] = "not_applicable"
            entry["reason"] = f"category is not defined by mapping {version}"
        elif places.usable:
            index = self._cached(places.manifest, self._place_index)
            selected = [
                index["records"][i]
                for i in _inside(index, polygon)
                if index["records"][i].canonical_category == category
            ]
            entry.update(
                _count_entry(
                    selected,
                    places.source,
                    places.manifest.source_edition,
                    _calculation(
                        "point_in_polygon_count",
                        f"Places whose representative point lies inside the area in {METRIC_CRS}",
                    ),
                    version,
                )
            )
            entry["items"] = [
                {"record_id": r.record_id, "name": r.name, "coordinates": r.geometry["coordinates"]}
                for r in selected[:MAX_LISTED_ITEMS]
            ]
            entry["items_truncated"] = len(selected) > MAX_LISTED_ITEMS
        sources.append(entry)

        businesses = resolved["businesses"]
        version, categories = mappings["businesses"]
        entry = {
            "dataset_id": businesses.dataset_id,
            "inventory": "registered_businesses",
            "status": businesses.status,
        }
        if businesses.usable and categories is not None and category not in categories:
            entry["status"] = "not_applicable"
            entry["reason"] = f"category is not defined by mapping {version}"
        elif businesses.usable:
            selected = [r for r in business_records if r.canonical_category == category]
            entry.update(
                _count_entry(
                    selected,
                    businesses.source,
                    businesses.manifest.source_edition,
                    _calculation(
                        "supplied_area_code_count",
                        "Businesses whose IHK-supplied planungsraum_id equals area_code",
                    ),
                    version,
                )
            )
        sources.append(entry)
        return {
            "category": category,
            "combination": "not_combined",
            "note": "Source-specific counts; do not add them as unique businesses.",
            "sources": sources,
        }

    def _workplaces(self, businesses, records):
        daytime = _field(
            None,
            "not_available",
            "persons",
            [],
            note="No measured daytime-population source is integrated.",
        )
        base = {
            "status": businesses.status,
            "dataset_id": businesses.dataset_id,
            "measured_daytime_population": daytime,
        }
        if not businesses.usable:
            return {**base, "registered_businesses": None, "employee_size_classes": []}
        classes: dict[tuple, int] = {}
        for record in records:
            key = (record.employees_min, record.employees_max, record.employees_label)
            classes[key] = classes.get(key, 0) + 1
        source = [businesses.source]
        edition = businesses.manifest.source_edition
        return {
            **base,
            "registered_businesses": _field(
                len(records),
                "derived",
                "businesses",
                source,
                reference_period=edition,
                calculation=_calculation(
                    "supplied_area_code_count",
                    "IHK registrations whose supplied planungsraum_id equals area_code",
                ),
            ),
            "employee_size_classes": [
                {
                    "label": label,
                    "employees_min": low,
                    "employees_max": high,
                    "business_count": _field(
                        count,
                        "derived",
                        "businesses",
                        source,
                        reference_period=edition,
                        calculation=_calculation(
                            "employee_size_class_count",
                            "IHK registrations in the area grouped by employee size-class label",
                        ),
                    ),
                    "note": "Size-class bounds, not headcounts.",
                }
                for (low, high, label), count in sorted(
                    classes.items(),
                    key=lambda item: (item[0][0] is None, item[0][0] or 0, str(item[0][2])),
                )
            ],
        }

    def _transit(self, polygon, transit):
        routed = _field(
            None,
            "not_available",
            "m",
            [],
            note="No routing source is integrated; straight-line distance is not walking time.",
        )
        base = {
            "status": transit.status,
            "dataset_id": transit.dataset_id,
            "routed_distance": routed,
        }
        if not transit.usable:
            return {
                **base,
                "windows": [],
                "stops_in_area": None,
                "stations": [],
                "nearest_station": None,
            }
        index = self._cached(transit.manifest, self._transit_index)
        source = [transit.source]
        period = transit.manifest.reference_period
        stop_counts: dict[int, int] = {}
        for i in _inside(index["stops"], polygon):
            kind = index["stops"]["records"][i].location_type
            stop_counts[kind] = stop_counts.get(kind, 0) + 1
        inside = [index["stations"]["records"][i] for i in _inside(index["stations"], polygon)]
        windows: dict[tuple, dict] = {}
        stations = []
        for station in inside:
            services = sorted(
                index["services"][station.stop_id],
                key=lambda s: (s.service_date, s.window_start, s.window_end),
            )
            stations.append(
                {
                    "stop_id": station.stop_id,
                    "name": station.name,
                    "coordinates": station.geometry["coordinates"],
                    "departures": [
                        {
                            "service_date": s.service_date,
                            "window_start": s.window_start,
                            "window_end": s.window_end,
                            "departures": s.departures,
                            "route_types": list(s.route_types),
                            "quality_flags": list(s.quality_flags),
                        }
                        for s in services
                    ],
                }
            )
            for s in services:
                window = windows.setdefault(
                    (s.service_date, s.window_start, s.window_end, s.timezone),
                    {"stations": 0, "departures": 0, "route_types": set(), "estimated": 0},
                )
                if ESTIMATE_FLAG in s.quality_flags:
                    window["estimated"] += 1
                window["stations"] += 1 if s.departures else 0
                window["departures"] += s.departures
                window["route_types"].update(s.route_types)
        all_windows = sorted(
            {
                (s.service_date, s.window_start, s.window_end, s.timezone)
                for services in index["services"].values()
                for s in services
            }
        )
        summary = []
        for key in all_windows:
            window = windows.get(
                key, {"stations": 0, "departures": 0, "route_types": set(), "estimated": 0}
            )
            summary.append(
                {
                    "service_date": key[0],
                    "window_start": key[1],
                    "window_end": key[2],
                    "timezone": key[3],
                    "stations_with_departures": _field(
                        window["stations"],
                        "derived",
                        "stations",
                        source,
                        reference_period=period,
                        calculation=_calculation(
                            "stations_with_departures",
                            f"StopService stations inside the area in {METRIC_CRS} with ≥1 departure",
                        ),
                    ),
                    "scheduled_departures": _field(
                        window["departures"],
                        "derived",
                        "departures",
                        source,
                        reference_period=period,
                        calculation=_calculation(
                            "station_departure_sum",
                            "Sum of per-station departures; a trip serving several stations in "
                            "the area is counted at each station",
                        ),
                        note="Includes departures estimated from GTFS frequencies.txt headways."
                        if window["estimated"]
                        else None,
                    ),
                    "includes_frequency_estimates": bool(window["estimated"]),
                    "estimated_station_windows": window["estimated"],
                    "route_types": sorted(window["route_types"]),
                }
            )
        stations.sort(
            key=lambda item: (-sum(d["departures"] for d in item["departures"]), item["stop_id"])
        )
        return {
            **base,
            "measures": "scheduled timetable departures, not passengers or footfall",
            "windows": summary,
            "stops_in_area": {
                str(kind): _field(
                    count,
                    "derived",
                    "stops",
                    source,
                    reference_period=period,
                    calculation=_calculation(
                        "stops_point_in_polygon_count",
                        f"GTFS stops by location_type whose point lies inside the area in {METRIC_CRS}",
                    ),
                )
                for kind, count in sorted(stop_counts.items())
            },
            "stations": stations[:MAX_LISTED_ITEMS],
            "stations_truncated": len(stations) > MAX_LISTED_ITEMS,
            "nearest_station": _nearest(index["stations"], polygon, source, period),
        }


def _points(records) -> dict[str, Any]:
    if not records:
        return {"records": [], "x": np.empty(0), "y": np.empty(0)}
    lon, lat = np.array([record.geometry["coordinates"] for record in records], dtype=float).T
    x, y = _TO_METRIC.transform(lon, lat)
    return {"records": records, "x": np.asarray(x), "y": np.asarray(y)}


def _inside(index, polygon) -> list[int]:
    if not len(index["records"]):
        return []
    minx, miny, maxx, maxy = polygon.bounds
    x, y = index["x"], index["y"]
    candidates = np.flatnonzero((x >= minx) & (x <= maxx) & (y >= miny) & (y <= maxy))
    shapely.prepare(polygon)
    hits = shapely.contains_xy(polygon, x[candidates], y[candidates])
    return candidates[hits].tolist()


def _nearest(index, polygon, source, period):
    if not len(index["records"]):
        return None
    point = polygon.representative_point()
    distances = np.hypot(index["x"] - point.x, index["y"] - point.y)
    position = int(np.argmin(distances))
    station = index["records"][position]
    lon, lat = _TO_WGS84.transform(point.x, point.y)
    return {
        "stop_id": station.stop_id,
        "name": station.name,
        "coordinates": station.geometry["coordinates"],
        "from": {
            "type": "area_representative_point",
            "coordinates": [round(lon, 6), round(lat, 6)],
        },
        "distance": _field(
            round(float(distances[position]), 1),
            "derived",
            "m",
            source,
            reference_period=period,
            calculation=_calculation(
                "straight_line_distance",
                f"Euclidean distance in {METRIC_CRS} from the area's representative point to the "
                "nearest station with scheduled-service records",
            ),
        ),
        "distance_type": "straight_line",
    }


def _count_entry(selected, source, edition, calculation, mapping_version):
    return {
        "count": _field(
            len(selected),
            "derived",
            "records",
            [source],
            reference_period=edition,
            calculation=calculation,
            note="Zero means none in this extraction, not none in reality."
            if not selected
            else None,
        ),
        "category_mapping_version": mapping_version,
        "record_ids": [record.record_id for record in selected[:MAX_LISTED_ITEMS]],
        "record_ids_truncated": len(selected) > MAX_LISTED_ITEMS,
    }


def _category_mapping(config) -> tuple[str | None, frozenset[str] | None]:
    name = config.parser_options.get("category_mapping") if config else None
    if not name:
        return None, frozenset()
    return _load_mapping(name)


def _load_mapping(name: str) -> tuple[str, frozenset[str] | None]:
    """Categories of a versioned mapping file; None when that version is not installed."""
    try:
        data = json.loads((CATEGORY_DIR / f"{name}.json").read_text())
    except (OSError, ValueError):
        return name, None
    entries = data.get("categories") or data.get("rules") or []
    return data.get("mapping_version", name), frozenset(
        item["canonical_category"] for item in entries if "canonical_category" in item
    )
