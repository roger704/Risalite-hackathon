"""Point-facility normalization for reviewed gdi.berlin WFS layers.

Used by ``berlin_wfs`` when ``record_type`` is ``facility``. Paging, raw storage and
page validation stay in the shared adapter; this module owns the facility mapping,
quality checks and the EPSG:25833 point-in-area assignment to a pinned LOR snapshot.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

from pyproj import Transformer
from shapely import STRtree, points
from shapely.geometry import shape
from shapely.ops import transform

from app.domain import (
    Area,
    DatasetConfig,
    Facility,
    IncompatibleSnapshotError,
    NormalizedBatch,
    QualityReport,
    RawBundle,
    SchemaError,
)

METRIC_CRS = "EPSG:25833"
EDITION_METADATA_NAME = "edition-metadata.json"


def _ratio(name: str, value) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 <= value <= 1
    ):
        raise SchemaError(f"WFS facility {name} must be a ratio between 0 and 1")
    return float(value)


def check_facility_options(options: dict) -> None:
    """Validate the facility-specific part of a reviewed WFS layer mapping."""
    field_map = options["field_map"]
    if not isinstance(field_map, dict) or any(
        not isinstance(field_map.get(key), str) or not field_map[key]
        for key in ("name", "category")
    ):
        raise SchemaError("WFS facility field_map must map name and category to properties")
    if "capacity" in field_map and (
        not isinstance(field_map["capacity"], str) or not field_map["capacity"]
    ):
        raise SchemaError("WFS facility field_map capacity must be a property name")
    for name in ("facility_type", "record_id_prefix", "category_mapping_version"):
        if not isinstance(options.get(name), str) or not options[name].strip():
            raise SchemaError(f"WFS facility requires a nonempty {name}")
    mapping = options.get("category_mapping")
    if not isinstance(mapping, dict) or any(
        not isinstance(key, str) or not isinstance(value, str) or not value
        for key, value in mapping.items()
    ):
        raise SchemaError("WFS facility category_mapping must map source labels to categories")
    if "capacity" in field_map:
        for name in ("capacity_definition", "capacity_reference_date"):
            if not isinstance(options.get(name), str) or not options[name].strip():
                raise SchemaError(f"WFS facility capacity mapping requires {name}")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", options["capacity_reference_date"]):
            raise SchemaError("WFS facility capacity_reference_date must be an ISO date")
    if options.get("geometry_types", ["Point"]) != ["Point"]:
        raise SchemaError("WFS facility geometry_types must be ['Point']")
    metadata_url = options.get("edition_metadata_url")
    if metadata_url is not None and (
        not isinstance(metadata_url, str) or urlsplit(metadata_url).scheme != "https"
    ):
        raise SchemaError("WFS facility edition_metadata_url must be an https URL")
    district_field = options.get("district_field")
    if district_field is not None and district_field not in options["expected_properties"]:
        raise SchemaError("WFS facility district_field must be an expected property")
    for name in ("max_unassigned_ratio", "max_district_mismatch_ratio"):
        _ratio(name, options.get(name, 0.01))


def _optional_text(value, key: str, source_id: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SchemaError(f"WFS facility property {key} must be a string or null: {source_id}")
    return value.strip() or None


def _capacity(value, key: str, source_id: str) -> int | None:
    text = _optional_text(value, key, source_id)
    if text is None:
        return None
    if not re.fullmatch(r"\d+", text):
        raise SchemaError(f"WFS facility capacity {key} is not a whole number: {source_id}")
    return int(text)


def verify_edition(raw: RawBundle, config: DatasetConfig, options: dict) -> str | None:
    """Check the provider's CKAN revision date against the reviewed source_edition.

    The WFS is a live layer without a per-response edition, so the snapshot stores the
    provider metadata next to the pages and refuses to publish when the revision moved.
    """
    if not options.get("edition_metadata_url"):
        return None
    reference_type = options.get("edition_reference_type", "revision")
    assets = [asset for asset in raw.assets if asset.path.endswith(EDITION_METADATA_NAME)]
    if len(assets) != 1:
        raise SchemaError("WFS facility source edition could not be verified: no provider metadata")
    try:
        result = json.loads(Path(assets[0].path).read_bytes())["result"]
        extras = {item["key"]: item["value"] for item in result["extras"]}
        references = json.loads(extras["dataset-reference-date"])
        dates = [item["value"] for item in references if item.get("type") == reference_type]
    except (KeyError, TypeError, ValueError) as exc:
        raise SchemaError("WFS facility provider metadata has no dataset reference dates") from exc
    if len(dates) != 1:
        raise SchemaError(f"WFS facility provider metadata has no unique {reference_type} date")
    if dates[0] != config.source_edition or (
        options.get("capacity_reference_date") not in (None, dates[0])
    ):
        raise SchemaError(
            f"Provider {reference_type} date {dates[0]} differs from the reviewed source_edition "
            f"{config.source_edition}; review the mapping and dates before publishing"
        )
    return (
        f"Source edition {dates[0]} verified against provider metadata ({reference_type} date; "
        f"metadata_modified {result.get('metadata_modified', 'unknown')})."
    )


def normalize_facilities(
    features: list, raw: RawBundle, config: DatasetConfig, options: dict, source_bounds, allowed
) -> NormalizedBatch:
    edition_note = verify_edition(raw, config, options)
    field_map = options["field_map"]
    capacity_field = field_map.get("capacity")
    required = set(options["expected_properties"])
    required.update((options["id_field"], *field_map.values(), *options["attribute_fields"]))
    transformer = Transformer.from_crs(options["expected_crs"], "EPSG:4326", always_xy=True)
    records, seen, extra = [], set(), set()
    categories, unmapped = Counter(), Counter()
    blanks = Counter()
    positions = Counter()
    for feature in features:
        if not isinstance(feature, dict) or not isinstance(feature.get("properties"), dict):
            raise SchemaError("WFS feature properties must be an object")
        properties = feature["properties"]
        missing = sorted(required - properties.keys())
        if missing:
            raise SchemaError(f"WFS feature is missing expected properties: {missing}")
        extra.update(properties.keys() - set(options["expected_properties"]))
        source_id = properties[options["id_field"]]
        if not isinstance(source_id, str) or not source_id:
            raise SchemaError("WFS ID must be a nonempty string")
        if options.get("id_pattern") and not re.fullmatch(options["id_pattern"], source_id):
            raise SchemaError(f"WFS ID does not match id_pattern: {source_id!r}")
        if source_id in seen:
            raise SchemaError(f"Duplicate WFS ID: {source_id}")
        seen.add(source_id)

        geometry_data = feature.get("geometry")
        if not isinstance(geometry_data, dict) or geometry_data.get("type") != "Point":
            raise SchemaError(f"WFS facility geometry must be a Point: {source_id}")
        try:
            x, y = (float(value) for value in geometry_data["coordinates"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SchemaError(f"Invalid WFS facility coordinates: {source_id}") from exc
        if not (
            math.isfinite(x)
            and math.isfinite(y)
            and source_bounds[0] <= x <= source_bounds[2]
            and source_bounds[1] <= y <= source_bounds[3]
        ):
            raise SchemaError(f"WFS geometry outside source_bbox: {source_id}")
        longitude, latitude = transformer.transform(x, y)
        if not (allowed[0] <= longitude <= allowed[2] and allowed[1] <= latitude <= allowed[3]):
            raise SchemaError(f"WFS geometry outside allowed_bbox: {source_id}")
        positions[(x, y)] += 1

        category = _optional_text(properties[field_map["category"]], "category", source_id)
        if category is None:
            raise SchemaError(f"WFS facility category is missing: {source_id}")
        categories[category] += 1
        canonical = options["category_mapping"].get(category)
        flags = []
        if canonical is None:
            unmapped[category] += 1
            flags.append("category_unmapped")

        capacity = None
        if capacity_field:
            capacity = _capacity(properties[capacity_field], capacity_field, source_id)
            if capacity is None:
                flags.append("capacity_missing")

        attributes = {}
        for key in options["attribute_fields"]:
            value = _optional_text(properties[key], key, source_id)
            if value is None:
                blanks[key] += 1
            else:
                attributes[key] = value
        name = _optional_text(properties[field_map["name"]], "name", source_id)
        if name is None:
            flags.append("name_missing")
        records.append(
            Facility(
                source_id=config.dataset_id,
                snapshot_id=raw.snapshot_id,
                source_record_id=source_id,
                record_id=f"{options['record_id_prefix']}:{source_id}",
                geometry={
                    "type": "Point",
                    "coordinates": [round(longitude, 7), round(latitude, 7)],
                },
                facility_type=options["facility_type"],
                provider_facility_id=source_id,
                original_category=category,
                category_mapping_version=options["category_mapping_version"],
                canonical_category=canonical,
                name=name,
                capacity=capacity,
                capacity_definition=options["capacity_definition"]
                if capacity is not None
                else None,
                capacity_reference_date=options["capacity_reference_date"]
                if capacity is not None
                else None,
                attributes=attributes,
                quality_flags=tuple(flags),
            )
        )

    total = len(records)
    warnings = list(options.get("limitations", ()))
    if edition_note:
        warnings.append(edition_note)
    warnings.append(
        "Facility categories: "
        + ", ".join(f"{label}={count}" for label, count in sorted(categories.items()))
    )
    if unmapped:
        warnings.append(
            "Unreviewed facility categories kept with canonical_category=null: "
            + ", ".join(f"{label}={count}" for label, count in sorted(unmapped.items()))
        )
    if capacity_field:
        known = [record.capacity for record in records if record.capacity is not None]
        warnings.append(
            f"Capacity ({options['capacity_definition']}, {capacity_field}, reference date "
            f"{options['capacity_reference_date']}): present for {len(known)}/{total} "
            f"facilities, missing for {total - len(known)}, known sum {sum(known)}. "
            "Vacancies are not provided by this source and remain null."
        )
    if blanks:
        warnings.append(
            f"Blank or null source attributes (of {total} facilities): "
            + ", ".join(f"{key}={count}" for key, count in sorted(blanks.items()))
        )
    shared = sum(count for count in positions.values() if count > 1)
    if shared:
        warnings.append(
            f"{shared} facilities share an identical point with another facility "
            "(e.g. several facilities at one address); they remain separate records."
        )
    if extra:
        warnings.append(f"WFS returned extra properties: {', '.join(sorted(extra))}")
    return NormalizedBatch(
        records=tuple(records), input_rows=len(features), warnings=tuple(warnings)
    )


def validate_facilities(batch: NormalizedBatch) -> QualityReport:
    errors = []
    if not batch.records:
        errors.append("WFS snapshot is empty")
    if batch.rejected_rows or batch.input_rows != len(batch.records):
        errors.append("WFS snapshot contains rejected or missing rows")
    provider_ids = set()
    for record in batch.records:
        if not isinstance(record, Facility):
            errors.append("WFS facility snapshot contains a non-facility record")
            continue
        if record.provider_facility_id in provider_ids:
            errors.append(f"Duplicate facility ID: {record.provider_facility_id}")
        provider_ids.add(record.provider_facility_id)
        try:
            geometry = shape(record.geometry)
        except Exception:
            errors.append(f"Invalid facility point: {record.provider_facility_id}")
            continue
        if geometry.is_empty or geometry.geom_type != "Point":
            errors.append(f"Invalid facility point: {record.provider_facility_id}")
    return QualityReport(
        valid=not errors,
        input_rows=batch.input_rows,
        output_rows=len(batch.records),
        rejected_rows=batch.rejected_rows,
        warnings=batch.warnings,
        errors=tuple(errors),
    )


def assign_areas(
    facilities, areas, hierarchy: str = "planungsraum"
) -> tuple[dict[str, str | None], int]:
    """Assign each facility to the area polygon covering it, measured in EPSG:25833.

    Returns ``{record_id: area_code or None}`` and the number of points on a shared
    boundary, which go deterministically to the lowest area code.
    """
    to_metric = Transformer.from_crs("EPSG:4326", METRIC_CRS, always_xy=True)
    polygons = [
        (record.area_code, transform(to_metric.transform, shape(record.geometry)))
        for record in areas
        if isinstance(record, Area) and record.hierarchy == hierarchy
    ]
    facilities = [record for record in facilities if isinstance(record, Facility)]
    result: dict[str, str | None] = {record.record_id: None for record in facilities}
    if not facilities or not polygons:
        return result, 0
    xs, ys = to_metric.transform(
        [record.geometry["coordinates"][0] for record in facilities],
        [record.geometry["coordinates"][1] for record in facilities],
    )
    tree = STRtree([geometry for _, geometry in polygons])
    point_index, area_index = tree.query(points(xs, ys), predicate="covered_by")
    matches: dict[int, list[str]] = {}
    for point, area in zip(point_index.tolist(), area_index.tolist()):
        matches.setdefault(point, []).append(polygons[area][0])
    ties = 0
    for point, codes in matches.items():
        ties += len(codes) > 1
        result[facilities[point].record_id] = min(codes)
    return result, ties


def check_facility_parent(
    config: DatasetConfig, options: dict, records: tuple, parent_records: tuple
) -> dict:
    hierarchy = options["hierarchy"]
    parent_areas = [
        record
        for record in parent_records
        if isinstance(record, Area) and record.hierarchy == hierarchy
    ]
    if not parent_areas:
        raise IncompatibleSnapshotError(f"Parent snapshot has no {hierarchy} areas")
    facilities = [record for record in records if isinstance(record, Facility)]
    assigned, ties = assign_areas(facilities, parent_areas, hierarchy)
    unassigned = sorted(record_id for record_id, code in assigned.items() if code is None)
    district_field = options.get("district_field")
    mismatches = []
    if district_field:
        for record in facilities:
            code, district = assigned[record.record_id], record.attributes.get(district_field)
            if code is not None and district is not None and code[:2] != district:
                mismatches.append(record.provider_facility_id)
    total = len(facilities)
    max_unassigned = _ratio("max_unassigned_ratio", options.get("max_unassigned_ratio", 0.01))
    max_mismatch = _ratio(
        "max_district_mismatch_ratio", options.get("max_district_mismatch_ratio", 0.01)
    )
    if total and (
        len(unassigned) / total > max_unassigned or len(mismatches) / total > max_mismatch
    ):
        raise IncompatibleSnapshotError(
            f"Facility/area assignment failed: {len(unassigned)} of {total} facilities outside "
            f"every {hierarchy}, {len(mismatches)} district mismatches; "
            f"examples unassigned={unassigned[:5]}, mismatched={sorted(mismatches)[:5]}"
        )
    counts = Counter(code for code in assigned.values() if code is not None)
    return {
        "parent_dataset": options.get("parent_dataset", "berlin_lor"),
        "area_join": {
            "hierarchy": hierarchy,
            "crs": METRIC_CRS,
            "method": "point covered_by area polygon; shared-boundary points to the lowest code",
            "parent_area_count": len(parent_areas),
            "assigned_facility_count": total - len(unassigned),
            "unassigned_facility_count": len(unassigned),
            "unassigned_examples": unassigned[:10],
            "boundary_tie_count": ties,
            "areas_with_facilities": len(counts),
            "areas_without_facilities": len(parent_areas) - len(counts),
            "district_field": district_field,
            "district_mismatch_count": len(mismatches),
            "district_mismatch_examples": sorted(mismatches)[:10],
        },
        "limitations": [
            "Area assignment is a point-in-polygon join against the pinned parent LOR snapshot; "
            "records keep area_code null and read paths recompute the same deterministic join."
        ],
    }
