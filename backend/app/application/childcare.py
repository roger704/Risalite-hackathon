"""Read-side childcare views: facility locations and per-area counts beside under-6 data.

Every source keeps its own snapshot, date, coverage and attribution. Nothing here
derives available places, vacancies or a shortage indicator.
"""

from __future__ import annotations

from collections import defaultdict
from functools import lru_cache

from app.application.service import DataService
from app.connectors.berlin_wfs_facility import assign_areas
from app.domain import ConnectorError, IncompatibleSnapshotError, record_from_dict

FACILITY_DATASET = "berlin_kitas"
LIMITATIONS = (
    "facility_count counts registry entries of publicly funded facilities in the area. It is "
    "not a count of available places and is not proof of a childcare shortage or surplus.",
    "permitted_places_known_sum adds 'Erlaubte Platzzahl' (licensed maximum) over facilities "
    "that report it; facilities_without_capacity are excluded, never counted as zero. Vacancies "
    "are not provided.",
    "Children under 6 are registered residents on the population reference date; facility data "
    "uses the registry revision date. The dates differ and are reported separately.",
    "Children attend facilities across area borders; per-area counts are not catchment demand.",
)


def _all_records(service: DataService, dataset_id: str, snapshot_id: str, **filters):
    cursor = None
    while True:
        page = service.records(dataset_id, snapshot_id, limit=1000, cursor=cursor, **filters)
        yield from (record_from_dict(record) for record in page["records"])
        cursor = page["next_cursor"]
        if cursor is None:
            return


def _source(manifest: dict) -> dict:
    return {
        key: manifest.get(key)
        for key in (
            "dataset_id",
            "snapshot_id",
            "source_edition",
            "reference_period",
            "boundary_edition",
            "retrieved_at",
            "landing_url",
            "license_id",
            "license_url",
            "attribution",
            "coverage",
        )
    } | {"warnings": list(manifest.get("quality_report", {}).get("warnings", ()))}


@lru_cache(maxsize=8)
def _facility_view(service: DataService, snapshot_id: str, dataset_id: str):
    manifest = service.manifest(dataset_id, snapshot_id)
    parents = manifest.get("parent_snapshot_ids") or []
    if len(parents) != 1:
        raise ConnectorError(
            "SOURCE_UNAVAILABLE", "Facility snapshot has no pinned parent LOR snapshot"
        )
    coverage = manifest.get("coverage") or {}
    parent_dataset = coverage.get("parent_dataset")
    hierarchy = (coverage.get("area_join") or {}).get("hierarchy")
    if not parent_dataset or not hierarchy:
        raise ConnectorError(
            "SOURCE_UNAVAILABLE", "Facility snapshot does not record its pinned area join"
        )
    lor_manifest = service.manifest(parent_dataset, parents[0])
    facilities = tuple(_all_records(service, dataset_id, snapshot_id))
    areas = tuple(
        record
        for record in _all_records(service, parent_dataset, parents[0])
        if getattr(record, "hierarchy", None) == hierarchy
    )
    assigned, _ = assign_areas(facilities, areas, hierarchy)
    return manifest, lor_manifest, hierarchy, facilities, areas, assigned


def childcare_facilities(
    service: DataService, snapshot_id: str, dataset_id: str = FACILITY_DATASET
) -> dict:
    """GeoJSON FeatureCollection of facilities with their pinned-LOR area assignment."""
    manifest, lor_manifest, hierarchy, facilities, _, assigned = _facility_view(
        service, snapshot_id, dataset_id
    )
    features = []
    for record in sorted(facilities, key=lambda item: item.record_id):
        properties = {
            key: getattr(record, key)
            for key in (
                "record_id",
                "provider_facility_id",
                "name",
                "facility_type",
                "original_category",
                "canonical_category",
                "capacity",
                "capacity_definition",
                "capacity_reference_date",
                "vacancies",
                "vacancies_reference_date",
            )
        }
        properties.update(
            area_code=assigned[record.record_id],
            hierarchy=hierarchy,
            attributes=dict(record.attributes),
            quality_flags=list(record.quality_flags),
        )
        features.append({"type": "Feature", "geometry": record.geometry, "properties": properties})
    return {
        "type": "FeatureCollection",
        "schema_version": "1.0",
        "sources": {"facilities": _source(manifest), "boundaries": _source(lor_manifest)},
        "limitations": list(LIMITATIONS),
        "features": features,
    }


def childcare_areas(
    service: DataService,
    snapshot_id: str,
    population_snapshot_id: str | None = None,
    dataset_id: str = FACILITY_DATASET,
    population_dataset_id: str = "berlin_population",
) -> dict:
    """Per-area facility counts and, optionally, children under 6 from a pinned snapshot."""
    manifest, lor_manifest, hierarchy, facilities, areas, assigned = _facility_view(
        service, snapshot_id, dataset_id
    )
    by_area = defaultdict(list)
    for record in facilities:
        if assigned[record.record_id] is not None:
            by_area[assigned[record.record_id]].append(record)
    children = {}
    population_source = None
    if population_snapshot_id is not None:
        population_manifest = service.manifest(population_dataset_id, population_snapshot_id)
        if list(population_manifest.get("parent_snapshot_ids") or []) != [
            lor_manifest["snapshot_id"]
        ]:
            raise IncompatibleSnapshotError(
                "Population and facility snapshots must pin the same LOR snapshot"
            )
        population_source = _source(population_manifest)
        for record in _all_records(
            service,
            population_dataset_id,
            population_snapshot_id,
            metric_code="population_age_band",
        ):
            if (
                record.hierarchy == hierarchy
                and record.age_min == 0
                and record.age_max_exclusive == 6
            ):
                children[record.area_code] = {
                    "value": record.value,
                    "value_status": getattr(record.value_status, "value", record.value_status),
                    "unit": record.unit,
                    "reference_period": record.reference_period,
                }
    rows = []
    for area in sorted(areas, key=lambda item: item.area_code):
        members = by_area.get(area.area_code, [])
        capacities = [record.capacity for record in members if record.capacity is not None]
        rows.append(
            {
                "area_code": area.area_code,
                "hierarchy": hierarchy,
                "facility_count": len(members),
                "facilities_with_capacity": len(capacities),
                "facilities_without_capacity": len(members) - len(capacities),
                "permitted_places_known_sum": sum(capacities),
                "children_under_6": children.get(area.area_code)
                if population_snapshot_id is not None
                else None,
            }
        )
    unassigned = sum(code is None for code in assigned.values())
    return {
        "schema_version": "1.0",
        "hierarchy": hierarchy,
        "sources": {
            "facilities": _source(manifest),
            "boundaries": _source(lor_manifest),
            "children_under_6": population_source,
        },
        "facility_count": len(facilities),
        "unassigned_facility_count": unassigned,
        "limitations": list(LIMITATIONS),
        "areas": rows,
    }
