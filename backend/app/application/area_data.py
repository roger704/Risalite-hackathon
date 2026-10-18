"""Data-stage port for area runs (Deep_Research_Spec §5.2, §9.4).

``AreaDataPort`` turns a stable area id into PLR cells with raw layer values for a scoring
template. Every layer value is recorded as a ``dataset`` ledger entry and its id is stored in
``CellInput.layer_evidence``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import shapely
from pyproj import Transformer
from shapely.geometry import shape

from app.domain import NotFoundError
from app.evidence.ledger import EvidenceLedger
from app.scoring.v2 import CellInput, Template

_TO_METRIC = Transformer.from_crs("EPSG:4326", "EPSG:25833", always_xy=True)
_TO_METRIC_FN = _TO_METRIC.transform

BEZIRKE = {
    "01": "Mitte",
    "02": "Friedrichshain-Kreuzberg",
    "03": "Pankow",
    "04": "Charlottenburg-Wilmersdorf",
    "05": "Spandau",
    "06": "Steglitz-Zehlendorf",
    "07": "Tempelhof-Schöneberg",
    "08": "Neukölln",
    "09": "Treptow-Köpenick",
    "10": "Marzahn-Hellersdorf",
    "11": "Lichtenberg",
    "12": "Reinickendorf",
}
LEVEL_ORDER = {"plr": 0, "bezirksregion": 1, "ortsteil": 2, "bezirk": 3}
_DIRECTION_SUFFIX = re.compile(
    r"\s+(nord|süd|ost|west|nordost|nordwest|südost|südwest|mitte|zentrum)$", re.IGNORECASE
)
MIN_SATURATION_DENOMINATOR = 2000
TRANSIT_WINDOWS = (("07:00", "10:00"), ("16:00", "19:00"))
FOOTFALL_CATEGORIES = ("bakery", "restaurant", "fast_food")
LANDING = "https://daten.berlin.de"
_REFERENCE_CACHE: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
_REFERENCE_CACHE_LOCK = threading.RLock()


@dataclass(frozen=True)
class AreaUnit:
    area_id: str
    level: str
    name: str
    bezirk: str | None
    plr_ids: tuple[str, ...]

    def candidate(self) -> dict[str, Any]:
        return {"area_id": self.area_id, "level": self.level, "name": self.name, "bezirk": self.bezirk}

    def resolution(self) -> dict[str, Any]:
        return {
            "area_id": self.area_id,
            "name": self.name,
            "level": self.level,
            "bezirk": self.bezirk,
            "plr_ids": list(self.plr_ids),
            "plr_count": len(self.plr_ids),
        }


class AreaDataPort(Protocol):
    def search_areas(self, query: str) -> list[dict[str, Any]]: ...

    def units(self) -> list[AreaUnit]: ...

    def resolve(self, area_id: str) -> dict[str, Any]: ...

    def cells(
        self, area_id: str, template: Template, *, ledger: EvidenceLedger | None = None
    ) -> list[CellInput]: ...

    def berlin_reference(self, template: Template) -> list[CellInput]: ...

    def confidence_reasons(self, area_id: str) -> dict[str, list[str]]: ...

    def unmapped_layers(self, template: Template) -> list[str]: ...

    def crosscheck(self, area_id: str) -> dict[str, tuple[int, int]]: ...

    def geojson(self, area_id: str) -> dict[str, Any] | None: ...


def units_from_lor(names: dict[str, dict[str, str]]) -> list[AreaUnit]:
    """Bezirk, Ortsteil (Prognoseraum names without direction suffix), Bezirksregion, PLR."""
    bezirke: dict[str, list[str]] = {}
    ortsteile: dict[tuple[str, str], list[str]] = {}
    ortsteil_key: dict[tuple[str, str], str] = {}
    regions: dict[str, tuple[str, list[str]]] = {}
    plrs: list[AreaUnit] = []
    for plr_id in sorted(names):
        attrs = names[plr_id]
        bez = plr_id[:2]
        bezirk = BEZIRKE.get(bez, attrs.get("bez") or bez)
        bezirke.setdefault(bez, []).append(plr_id)
        pgr_name = (attrs.get("pgr_name") or "").strip()
        if pgr_name:
            ortsteil = _DIRECTION_SUFFIX.sub("", pgr_name).strip()
            key = (bez, ortsteil)
            ortsteile.setdefault(key, []).append(plr_id)
            ortsteil_key.setdefault(key, attrs.get("pgr_id") or plr_id[:4])
        bzr_id = attrs.get("bzr_id") or plr_id[:6]
        regions.setdefault(bzr_id, ((attrs.get("bzr_name") or bzr_id).strip(), []))[1].append(
            plr_id
        )
        plrs.append(AreaUnit(f"plr:{plr_id}", "plr", attrs.get("name", plr_id), bezirk, (plr_id,)))
    units = [
        AreaUnit(f"bezirk:{bez}", "bezirk", BEZIRKE.get(bez, bez), BEZIRKE.get(bez, bez), tuple(ids))
        for bez, ids in sorted(bezirke.items())
    ]
    units += [
        AreaUnit(f"ortsteil:{ortsteil_key[key]}", "ortsteil", key[1], BEZIRKE.get(key[0]), tuple(ids))
        for key, ids in sorted(ortsteile.items(), key=lambda item: ortsteil_key[item[0]])
    ]
    units += [
        AreaUnit(f"bezirksregion:{bzr}", "bezirksregion", name, BEZIRKE.get(bzr[:2]), tuple(ids))
        for bzr, (name, ids) in sorted(regions.items())
    ]
    return units + plrs


def search_units(units: Iterable[AreaUnit], query: str) -> list[dict[str, Any]]:
    value = query.strip().casefold()
    if not value:
        return []
    hits = [unit for unit in units if value in unit.name.casefold() or value == unit.area_id]
    hits.sort(key=lambda u: (not u.name.casefold().startswith(value), -LEVEL_ORDER[u.level], u.area_id))
    return [unit.candidate() for unit in hits[:50]]


def _layer_entry(
    ledger: EvidenceLedger | None,
    *,
    cell_id: str,
    cell_name: str,
    layer: str,
    value: float | None,
    dataset_id: str,
    snapshot_id: str,
    source_url: str,
    unit: str | None,
    method: str,
) -> str | None:
    if ledger is None:
        return None
    shown = "missing" if value is None else f"{value:g}"
    entry = ledger.add_dataset_value(
        dataset_id=dataset_id,
        snapshot_id=snapshot_id,
        layer_id=layer,
        source_url=source_url,
        value=value,
        query={"area_code": cell_id, "layer_id": layer, "method": method},
        quote=f"{layer} for PLR {cell_id} ({cell_name}): {shown}{' ' + unit if unit else ''}",
        title=f"{layer} · PLR {cell_id} {cell_name}",
    )
    return entry["evidence_id"]


# --- Fixture / offline port ----------------------------------------------------------


@dataclass
class FixtureAreaData:
    """In-memory port: ``units`` plus per-PLR rows ``{id, name, parent_name, residents, raw}``."""

    area_units: list[AreaUnit]
    plrs: dict[str, dict[str, Any]]
    geometry: dict[str, Any] | None = None
    dataset_id: str = "fixture"
    snapshot_id: str = "fixture"
    notes: dict[str, list[str]] = field(default_factory=dict)

    def units(self) -> list[AreaUnit]:
        return list(self.area_units)

    def _unit(self, area_id: str) -> AreaUnit:
        for unit in self.area_units:
            if unit.area_id == area_id:
                return unit
        raise KeyError(area_id)

    def search_areas(self, query: str) -> list[dict[str, Any]]:
        return search_units(self.area_units, query)

    def resolve(self, area_id: str) -> dict[str, Any]:
        return self._unit(area_id).resolution()

    def _cell(self, row: dict[str, Any], template: Template, ledger: EvidenceLedger | None) -> CellInput:
        raw = {layer.id: _number(row.get("raw", {}).get(layer.id)) for layer in template.layers}
        evidence = {}
        for layer in template.layers:
            eid = _layer_entry(
                ledger,
                cell_id=row["id"],
                cell_name=row.get("name", row["id"]),
                layer=layer.id,
                value=raw[layer.id],
                dataset_id=self.dataset_id,
                snapshot_id=self.snapshot_id,
                source_url=row.get("source_url", LANDING),
                unit=layer.unit,
                method="fixture",
            )
            if eid:
                evidence[layer.id] = eid
        return CellInput(
            cell_id=row["id"],
            name=row.get("name", row["id"]),
            residents=_number(row.get("residents")),
            raw=raw,
            parent_name=row.get("parent_name"),
            incomplete=bool(row.get("incomplete", False)),
            gap_low=_number(row.get("gap_low")),
            gap_high=_number(row.get("gap_high")),
            layer_evidence=evidence,
        )

    def cells(
        self, area_id: str, template: Template, *, ledger: EvidenceLedger | None = None
    ) -> list[CellInput]:
        unit = self._unit(area_id)
        return [self._cell(self.plrs[p], template, ledger) for p in unit.plr_ids if p in self.plrs]

    def berlin_reference(self, template: Template) -> list[CellInput]:
        return [self._cell(row, template, None) for row in self.plrs.values()]

    def confidence_reasons(self, area_id: str) -> dict[str, list[str]]:
        unit = self._unit(area_id)
        return {p: list(self.notes[p]) for p in unit.plr_ids if p in self.notes}

    def crosscheck(self, area_id: str) -> dict[str, tuple[int, int]]:
        """PLR → (OSM, IHK) café counts for ``score_area(crosscheck=…)``; never summed."""
        unit = self._unit(area_id)
        return {
            p: tuple(self.plrs[p]["crosscheck"])
            for p in unit.plr_ids
            if p in self.plrs and self.plrs[p].get("crosscheck")
        }

    def unmapped_layers(self, template: Template) -> list[str]:
        return [
            layer.id
            for layer in template.layers
            if not any(_number(row.get("raw", {}).get(layer.id)) is not None for row in self.plrs.values())
        ]

    def geojson(self, area_id: str) -> dict[str, Any] | None:
        if self.geometry is None:
            return None
        ids = set(self._unit(area_id).plr_ids)
        features = [
            f for f in self.geometry.get("features", []) if str(f.get("properties", {}).get("id", f.get("id"))) in ids
        ]
        return {"type": "FeatureCollection", "features": features}

    @classmethod
    def from_frontend_fixtures(cls, fixture_dir: Path) -> FixtureAreaData:
        """Synthetic Kreuzberg demo cells mapped to ``coffee_v2`` layer ids."""
        cells = json.loads((fixture_dir / "cells.json").read_text(encoding="utf-8"))
        geometry_path = fixture_dir / "cells.geojson"
        geometry = json.loads(geometry_path.read_text(encoding="utf-8")) if geometry_path.exists() else None
        residents = 5000.0
        plrs = {}
        for cell in cells["cells"]:
            raw = cell.get("raw_values", {})
            density = _number(raw.get("demand_density"))
            cafes = _number(raw.get("competition_osm"))
            plrs[cell["id"]] = {
                "id": cell["id"],
                "name": cell["name"],
                "parent_name": cell.get("parent_name"),
                "residents": residents,
                "raw": {
                    "residents_block_density": None if density is None else density * 100,
                    "young_adult_share": _number(raw.get("young_share")),
                    "transit": _number(raw.get("transit")),
                    "cafe_saturation": None if cafes is None else cafes / residents * 10000,
                    "cafe_crosscheck": _number(raw.get("competition_ihk")),
                },
            }
            ihk = _number(raw.get("competition_ihk"))
            if cafes is not None and ihk is not None:
                plrs[cell["id"]]["crosscheck"] = (int(cafes), int(ihk))
        ids = tuple(plrs)
        units = [
            AreaUnit("bezirk:02", "bezirk", BEZIRKE["02"], BEZIRKE["02"], ids),
            AreaUnit("ortsteil:0202", "ortsteil", "Kreuzberg", BEZIRKE["02"], ids),
        ]
        return cls(units, plrs, geometry, dataset_id="frontend_fixture", snapshot_id="synthetic")


# --- Area-summary port ----------------------------------------------------------------


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _value(field_: Any) -> float | None:
    return _number(field_.get("value")) if isinstance(field_, dict) else None


def _sources(field_: Any) -> list[dict[str, str]]:
    return list(field_.get("sources") or []) if isinstance(field_, dict) else []


def _young_share(residents: dict[str, Any]) -> tuple[float | None, list[dict[str, str]]]:
    total = _value(residents.get("total"))
    bands = [
        b
        for b in residents.get("age_bands") or []
        if b.get("age_min") is not None
        and b.get("age_max_exclusive") is not None
        and b["age_min"] >= 18
        and b["age_max_exclusive"] <= 45
    ]
    covered = sorted((b["age_min"], b["age_max_exclusive"]) for b in bands)
    contiguous = bool(covered) and covered[0][0] == 18 and covered[-1][1] == 45 and all(
        a[1] == b[0] for a, b in zip(covered, covered[1:])
    )
    counts = [_value(b.get("count")) for b in bands]
    if not total or not contiguous or any(c is None for c in counts):
        return None, []
    return round(sum(counts) / total, 4), _sources(bands[0].get("count"))


def _osm_count(summary: dict[str, Any]) -> tuple[float | None, list[dict[str, str]], float | None]:
    """(OSM count, sources, IHK count); never combined (§1 rule 3)."""
    osm = ihk = None
    sources: list[dict[str, str]] = []
    for entry in (summary.get("competitors") or {}).get("sources") or []:
        count = _value(entry.get("count")) if isinstance(entry.get("count"), dict) else _number(entry.get("count"))
        if entry.get("inventory") == "mapped_places":
            osm = count
            sources = _sources(entry.get("count")) or (
                [{"dataset_id": entry.get("dataset_id"), "snapshot_id": entry.get("snapshot_id", "")}]
            )
        elif entry.get("inventory") == "registered_businesses":
            ihk = count
    return osm, sources, ihk


def _transit_departures(transit: dict[str, Any]) -> tuple[float | None, list[dict[str, str]]]:
    windows = transit.get("windows") or []
    picked = [
        w for w in windows if (str(w.get("window_start"))[:5], str(w.get("window_end"))[:5]) in TRANSIT_WINDOWS
    ]
    values = [_value(w.get("scheduled_departures")) for w in picked]
    if not picked or any(v is None for v in values):
        return None, []
    return float(sum(values)), _sources(picked[0].get("scheduled_departures"))


SummaryFn = Callable[..., dict[str, Any]]


class SummaryAreaData:
    """Port over ``AreaSummaryService.summary(plr_code, category=...)`` per PLR.

    Block density and centre proximity use their published snapshots when available; residents
    per PLR area remains the flagged density fallback.
    """

    MAPPED = frozenset(
        {
            "residents_block_density",
            "young_adult_share",
            "footfall_proxy",
            "transit",
            "cafe_saturation",
            "cafe_crosscheck",
        }
    )

    def __init__(
        self,
        summary: SummaryFn,
        names: dict[str, dict[str, str]],
        *,
        source_urls: dict[str, str] | None = None,
        block_densities: dict[str, float] | None = None,
        block_snapshot_id: str | None = None,
        center_geometries: Iterable[dict[str, Any]] = (),
        center_snapshot_id: str | None = None,
        snapshot_ids: dict[str, str | None] | None = None,
        reference_cache_dir: Path | None = None,
    ):
        self._summary = summary
        self._names = names
        self._units = units_from_lor(names)
        self._by_id = {unit.area_id: unit for unit in self._units}
        self._source_urls = source_urls or {}
        self._block_densities = block_densities or {}
        self._block_snapshot_id = block_snapshot_id
        self._center_geometries = [
            shapely.transform(shape(geometry), _TO_METRIC_FN, interleaved=False)
            for geometry in center_geometries
        ]
        self._center_snapshot_id = center_snapshot_id
        self._snapshot_ids = dict(snapshot_ids or {})
        self._reference_cache_dir = reference_cache_dir
        self._rows: dict[str, dict[str, Any]] = {}
        self._notes: dict[str, list[str]] = {}
        self._reference: dict[str, list[CellInput]] = {}
        self._geometry: dict[str, Any] = {}

    @classmethod
    def from_data_service(cls, data_service: Any) -> SummaryAreaData:
        from app.application.area_names import area_names
        from app.application.area_summary import AreaSummaryService
        from app.application.population_blocks import block_aggregates

        manifest = data_service.catalog.get("berlin_lor_names", None)
        names = area_names(data_service, manifest.snapshot_id)
        urls = {
            dataset_id: getattr(config, "landing_url", LANDING)
            for dataset_id, config in data_service.configs.items()
        }

        def published_snapshot(dataset_id: str):
            try:
                candidate = data_service.catalog.get(dataset_id)
            except NotFoundError:
                return None
            return candidate if data_service._snapshot_available(candidate) else None

        dataset_ids = (
            "berlin_lor",
            "berlin_lor_names",
            "berlin_population",
            "osm_pois",
            "vbb_gtfs",
            "ihk_businesses",
            "berlin_population_density_blocks",
            "berlin_centers",
        )
        snapshots = {dataset_id: published_snapshot(dataset_id) for dataset_id in dataset_ids}
        snapshot_ids = {
            dataset_id: manifest.snapshot_id if manifest else None
            for dataset_id, manifest in snapshots.items()
        }

        blocks = snapshots["berlin_population_density_blocks"]
        block_densities = {}
        if blocks:
            aggregates = block_aggregates(data_service, blocks.snapshot_id)
            block_densities = {
                area_code: round(value.net_density_per_ha * 100, 3)
                for area_code, value in aggregates.areas.items()
                if value.net_density_per_ha is not None
            }

        centers = snapshots["berlin_centers"]
        center_geometries = []
        if centers:
            center_geometries = [
                record.geometry
                for record in data_service._load_records(centers)
                if hasattr(record, "geometry")
            ]

        return cls(
            AreaSummaryService(data_service).summary,
            names,
            source_urls=urls,
            block_densities=block_densities,
            block_snapshot_id=blocks.snapshot_id if blocks else None,
            center_geometries=center_geometries,
            center_snapshot_id=centers.snapshot_id if centers else None,
            snapshot_ids=snapshot_ids,
            reference_cache_dir=data_service.root / "cache" / "berlin_reference",
        )

    def units(self) -> list[AreaUnit]:
        return list(self._units)

    def search_areas(self, query: str) -> list[dict[str, Any]]:
        return search_units(self._units, query)

    def resolve(self, area_id: str) -> dict[str, Any]:
        return self._by_id[area_id].resolution()

    def unmapped_layers(self, template: Template) -> list[str]:
        return [
            layer.id
            for layer in template.layers
            if layer.id not in self.MAPPED
            and not (layer.id == "centre_proximity" and self._center_geometries)
        ]

    def _centre_proximity(self, boundary: dict[str, Any] | None) -> float | None:
        if not boundary or not self._center_geometries:
            return None
        point = shapely.transform(
            shape(boundary).representative_point(), _TO_METRIC_FN, interleaved=False
        )
        return round(min(point.distance(center) for center in self._center_geometries), 1)

    def _row(self, plr: str) -> dict[str, Any]:
        if plr in self._rows:
            return self._rows[plr]
        base = self._summary(plr, category="coffee_shop", include_geometry=True)
        area = base.get("area") or {}
        residents = base.get("residents") or {}
        total = _value(residents.get("total"))
        area_km2 = _value(area.get("area_km2"))
        if area.get("boundary"):
            self._geometry[plr] = area["boundary"]
        notes: list[str] = []
        values: dict[str, tuple[float | None, list[dict[str, str]], str | None]] = {}

        density = self._block_densities.get(plr)
        density_sources = []
        if density is not None:
            density_sources = [
                {
                    "dataset_id": "berlin_population_density_blocks",
                    "snapshot_id": self._block_snapshot_id or "",
                }
            ]
        else:
            density = _value(residents.get("density"))
            density_sources = _sources(residents.get("density"))
        if density is not None and plr not in self._block_densities:
            notes.append("fallback layer used: residents_block_density = residents / PLR area")
        values["residents_block_density"] = (density, density_sources, "persons/km2")
        values["young_adult_share"] = (*_young_share(residents), "ratio")

        osm, osm_sources, ihk = _osm_count(base)
        saturation = (
            None if osm is None or total is None else round(osm / max(total, MIN_SATURATION_DENOMINATOR) * 10000, 3)
        )
        values["cafe_saturation"] = (saturation, osm_sources, "cafés per 10,000 residents")
        values["cafe_crosscheck"] = (ihk, [], "businesses")
        # OSM vs IHK deviation is judged by score_area(crosscheck=…) via crosscheck().

        footfall: list[float] = []
        footfall_sources: list[dict[str, str]] = []
        for category in FOOTFALL_CATEGORIES:
            count, sources, _ = _osm_count(self._summary(plr, category=category, include_geometry=False))
            if count is None:
                footfall = []
                break
            footfall.append(count)
            footfall_sources = footfall_sources or sources
        values["footfall_proxy"] = (
            round(sum(footfall) / area_km2, 3) if footfall and area_km2 else None,
            footfall_sources,
            "POIs/km2",
        )
        values["transit"] = (*_transit_departures(base.get("transit") or {}), "departures")
        center_distance = self._centre_proximity(area.get("boundary"))
        values["centre_proximity"] = (
            center_distance,
            (
                [
                    {
                        "dataset_id": "berlin_centers",
                        "snapshot_id": self._center_snapshot_id or "",
                    }
                ]
                if center_distance is not None
                else []
            ),
            "meters",
        )
        name = _value_text(area.get("name")) or self._names.get(plr, {}).get("name", plr)
        row = {"id": plr, "name": name, "residents": total, "values": values}
        if osm is not None and ihk is not None:
            row["crosscheck"] = (int(osm), int(ihk))
        self._rows[plr] = row
        self._notes[plr] = notes
        return row

    def _cell(self, plr: str, template: Template, ledger: EvidenceLedger | None) -> CellInput:
        row = self._row(plr)
        raw: dict[str, float | None] = {}
        evidence: dict[str, str] = {}
        for layer in template.layers:
            value, sources, unit = row["values"].get(layer.id, (None, [], layer.unit))
            raw[layer.id] = value
            source = sources[0] if sources else {}
            dataset_id = source.get("dataset_id") or "area_summary"
            eid = _layer_entry(
                ledger,
                cell_id=plr,
                cell_name=row["name"],
                layer=layer.id,
                value=value,
                dataset_id=dataset_id,
                snapshot_id=source.get("snapshot_id") or "unknown",
                source_url=self._source_urls.get(dataset_id, LANDING),
                unit=unit,
                method="area_summary_v1",
            )
            if eid:
                evidence[layer.id] = eid
        return CellInput(
            cell_id=plr,
            name=row["name"],
            residents=row["residents"],
            raw=raw,
            parent_name=self._names.get(plr, {}).get("bzr_name"),
            layer_evidence=evidence,
        )

    def cells(
        self, area_id: str, template: Template, *, ledger: EvidenceLedger | None = None
    ) -> list[CellInput]:
        return [self._cell(plr, template, ledger) for plr in self._by_id[area_id].plr_ids]

    def berlin_reference(self, template: Template) -> list[CellInput]:
        if not any(self._snapshot_ids.values()):
            if template.id not in self._reference:
                self._reference[template.id] = [
                    self._cell(plr, template, None) for plr in sorted(self._names)
                ]
            return self._reference[template.id]

        cache_input = {
            "template_id": template.id,
            "snapshot_ids": dict(sorted(self._snapshot_ids.items())),
        }
        cache_key = hashlib.sha256(
            json.dumps(cache_input, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if cache_key in self._reference:
            return self._reference[cache_key]

        with _REFERENCE_CACHE_LOCK:
            cached = _REFERENCE_CACHE.get(cache_key)
            if cached is not None:
                _REFERENCE_CACHE.move_to_end(cache_key)
            if cached is None and self._reference_cache_dir is not None:
                cache_path = self._reference_cache_dir / f"{cache_key}.json"
                try:
                    disk_cache = json.loads(cache_path.read_text(encoding="utf-8"))
                    if disk_cache.get("key") == cache_input and isinstance(
                        disk_cache.get("cells"), list
                    ):
                        cached = disk_cache["cells"]
                except (OSError, ValueError, AttributeError):
                    pass

            if cached is not None:
                _REFERENCE_CACHE[cache_key] = cached
                _REFERENCE_CACHE.move_to_end(cache_key)
                while len(_REFERENCE_CACHE) > 8:
                    _REFERENCE_CACHE.popitem(last=False)

            if cached is None:
                cells = [self._cell(plr, template, None) for plr in sorted(self._names)]
                cached = [
                    {
                        "cell_id": cell.cell_id,
                        "name": cell.name,
                        "residents": cell.residents,
                        "raw": cell.raw,
                        "parent_name": cell.parent_name,
                        "incomplete": cell.incomplete,
                        "gap_low": cell.gap_low,
                        "gap_high": cell.gap_high,
                        "fallback_layers": cell.fallback_layers,
                    }
                    for cell in cells
                ]
                if self._reference_cache_dir is not None:
                    self._reference_cache_dir.mkdir(parents=True, exist_ok=True)
                    cache_path = self._reference_cache_dir / f"{cache_key}.json"
                    temp_path = cache_path.with_name(f"{cache_path.name}.{os.getpid()}.tmp")
                    temp_path.write_text(
                        json.dumps({"key": cache_input, "cells": cached}, ensure_ascii=False),
                        encoding="utf-8",
                    )
                    temp_path.replace(cache_path)
                _REFERENCE_CACHE[cache_key] = cached
                while len(_REFERENCE_CACHE) > 8:
                    _REFERENCE_CACHE.popitem(last=False)

        self._reference[cache_key] = [
            CellInput(
                cell_id=cell["cell_id"],
                name=cell["name"],
                residents=cell["residents"],
                raw=dict(cell["raw"]),
                parent_name=cell.get("parent_name"),
                incomplete=cell.get("incomplete", False),
                gap_low=cell.get("gap_low"),
                gap_high=cell.get("gap_high"),
                fallback_layers=list(cell.get("fallback_layers") or []),
            )
            for cell in cached
        ]
        return self._reference[cache_key]

    def confidence_reasons(self, area_id: str) -> dict[str, list[str]]:
        return {p: list(self._notes.get(p, [])) for p in self._by_id[area_id].plr_ids if self._notes.get(p)}

    def crosscheck(self, area_id: str) -> dict[str, tuple[int, int]]:
        """PLR → (OSM, IHK) café counts (rows are loaded by ``cells`` first)."""
        rows = {p: self._row(p) for p in self._by_id[area_id].plr_ids}
        return {p: tuple(row["crosscheck"]) for p, row in rows.items() if row.get("crosscheck")}

    def geojson(self, area_id: str) -> dict[str, Any] | None:
        features = [
            {"type": "Feature", "id": plr, "properties": {"id": plr, "name": self._names.get(plr, {}).get("name")}, "geometry": self._geometry[plr]}
            for plr in self._by_id[area_id].plr_ids
            if plr in self._geometry
        ]
        return {"type": "FeatureCollection", "features": features} if features else None


def _value_text(field_: Any) -> str | None:
    if isinstance(field_, dict) and isinstance(field_.get("value"), str):
        return field_["value"]
    return None
