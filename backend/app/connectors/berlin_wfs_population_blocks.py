"""Umweltatlas Einwohnerdichte block normalization and LOR assignment."""

from __future__ import annotations

import math
import re
from dataclasses import replace
from numbers import Real

from pyproj import Transformer
from shapely.geometry import mapping, shape
from shapely.ops import transform
from shapely.strtree import STRtree

from app.domain import (
    POPULATION_BLOCK_AGE_BANDS,
    Area,
    ConfigError,
    DatasetConfig,
    IncompatibleSnapshotError,
    NormalizedBatch,
    PopulationBlock,
    QualityReport,
    RawBundle,
    SchemaError,
    ValueStatus,
)


def _finite_number(value, *, minimum: float | None = None, maximum: float | None = None) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, Real)
        and math.isfinite(value)
        and (minimum is None or value >= minimum)
        and (maximum is None or value <= maximum)
    )


def validate_options(options: dict) -> None:
    field_map = options.get("field_map")
    required_fields = (
        "block_id",
        "residents",
        "area_ha",
        "density_per_ha",
        "land_use_type",
        "land_use_type_en",
    )
    if not isinstance(field_map, dict) or any(
        not isinstance(field_map.get(name), str) or not field_map[name] for name in required_fields
    ):
        raise SchemaError("WFS population-block field_map must map all fields to properties")
    age_fields = options.get("age_fields")
    if (
        not isinstance(age_fields, dict)
        or set(age_fields) != set(POPULATION_BLOCK_AGE_BANDS)
        or any(not isinstance(value, str) or not value for value in age_fields.values())
    ):
        raise SchemaError("WFS age_fields must map exactly the population-block age bands")
    if (
        type(options.get("suppression_max_residents")) is not int
        or options["suppression_max_residents"] < 0
    ):
        raise SchemaError("suppression_max_residents must be a nonnegative integer")
    ambiguous = options.get("ambiguous_share_threshold")
    partial = options.get("partial_share_threshold")
    maximum_unassigned = options.get("max_unassigned_share")
    if (
        not _finite_number(ambiguous, minimum=0, maximum=1)
        or not _finite_number(partial, minimum=0, maximum=1)
        or ambiguous == 0
        or ambiguous > partial
    ):
        raise SchemaError("WFS assignment thresholds must satisfy 0 < ambiguous <= partial <= 1")
    if not _finite_number(maximum_unassigned, minimum=0, maximum=1):
        raise SchemaError("max_unassigned_share must be in [0, 1]")


def normalize_population_blocks(
    features: list[dict], raw: RawBundle, config: DatasetConfig, options: dict
) -> NormalizedBatch:
    validate_options(options)
    if config.reference_period is None:
        raise ConfigError("Population-block configuration requires reference_period")
    source_bounds = options["source_bbox"]
    allowed_bounds = options.get("allowed_bbox", [13.0, 52.3, 13.9, 52.8])
    geometry_types = options.get("geometry_types", ["Polygon", "MultiPolygon"])
    field_map = options["field_map"]
    age_fields = options["age_fields"]
    required_properties = set(options["expected_properties"])
    required_properties.update(field_map.values())
    required_properties.update(age_fields.values())
    required_properties.add(options["id_field"])
    records = []
    seen_ids = set()
    extra_properties = set()
    flag_counts: dict[str, int] = {}
    n_suppressed = 0
    n_observed = 0
    observed_residents = 0
    try:
        transformer = Transformer.from_crs(options["expected_crs"], "EPSG:4326", always_xy=True)
        for feature in features:
            if not isinstance(feature, dict) or not isinstance(feature.get("properties"), dict):
                raise SchemaError("WFS feature properties must be an object")
            properties = feature["properties"]
            missing = sorted(required_properties - properties.keys())
            if missing:
                raise SchemaError(f"WFS feature is missing expected properties: {missing}")
            extra_properties.update(properties.keys() - set(options["expected_properties"]))
            block_id = properties[options["id_field"]]
            if not isinstance(block_id, str) or not block_id:
                raise SchemaError("WFS ID must be a nonempty string")
            if options.get("id_pattern") and not re.fullmatch(options["id_pattern"], block_id):
                raise SchemaError(f"WFS ID does not match id_pattern: {block_id!r}")
            if block_id in seen_ids:
                raise SchemaError(f"Duplicate WFS ID: {block_id}")
            seen_ids.add(block_id)

            geometry_data = feature.get("geometry")
            if not isinstance(geometry_data, dict):
                raise SchemaError(f"Invalid or missing WFS geometry: {block_id}")
            try:
                geometry = shape(geometry_data)
            except Exception as exc:
                raise SchemaError(f"Invalid WFS geometry: {block_id}") from exc
            if geometry.is_empty or geometry.geom_type not in geometry_types:
                raise SchemaError(f"Unsupported WFS geometry type: {block_id}")
            minx, miny, maxx, maxy = geometry.bounds
            if not (
                source_bounds[0] <= minx <= maxx <= source_bounds[2]
                and source_bounds[1] <= miny <= maxy <= source_bounds[3]
            ):
                raise SchemaError(f"WFS geometry outside source_bbox: {block_id}")
            geometry = transform(transformer.transform, geometry)
            if geometry.is_empty or not geometry.is_valid:
                raise SchemaError(f"Invalid WFS polygon after reprojection: {block_id}")
            west, south, east, north = geometry.bounds
            if not (
                allowed_bounds[0] <= west <= east <= allowed_bounds[2]
                and allowed_bounds[1] <= south <= north <= allowed_bounds[3]
            ):
                raise SchemaError(f"WFS geometry outside allowed_bbox: {block_id}")

            residents = properties[field_map["residents"]]
            if type(residents) is not int or residents < 0:
                raise SchemaError(f"Invalid resident count for block {block_id}")
            area_ha = properties[field_map["area_ha"]]
            if not _finite_number(area_ha, minimum=0) or area_ha == 0:
                raise SchemaError(f"Invalid area_ha for block {block_id}")
            density = properties[field_map["density_per_ha"]]
            if not _finite_number(density, minimum=0):
                raise SchemaError(f"Invalid density_per_ha for block {block_id}")

            flags = []
            age_values = {}
            source_ages = {band: properties[prop] for band, prop in age_fields.items()}
            for value in source_ages.values():
                if value is not None and (type(value) is not int or value < 0):
                    raise SchemaError(f"Invalid age-band count for block {block_id}")
            suppression_limit = options["suppression_max_residents"]
            if residents == 0:
                if density != 0:
                    raise SchemaError(f"Suppressed block {block_id} must publish zero density")
                status = ValueStatus.SUPPRESSED
                stored_residents = None
                stored_density = None
                n_suppressed += 1
                flags.append("residents_suppressed_0_to_2")
            elif residents <= suppression_limit:
                raise SchemaError(
                    f"Published resident count for block {block_id} is within the suppression range"
                )
            else:
                status = ValueStatus.OBSERVED
                stored_residents = residents
                stored_density = float(density)
                n_observed += 1
                observed_residents += residents
                if abs(round(residents / area_ha, 2) - density) > 0.011:
                    flags.append("density_inconsistent")
                if all(type(source_ages[band]) is int for band in POPULATION_BLOCK_AGE_BANDS):
                    age_values = source_ages
                    if sum(age_values.values()) != residents:
                        flags.append("age_bands_sum_mismatch")
                else:
                    flags.append("age_bands_missing")
            for flag in flags:
                flag_counts[flag] = flag_counts.get(flag, 0) + 1

            land_use_values = {}
            for key in ("land_use_type", "land_use_type_en"):
                value = properties[field_map[key]]
                if value is not None and not isinstance(value, str):
                    raise SchemaError(f"{key} must be a string or null for block {block_id}")
                land_use_values[key] = value.strip() or None if isinstance(value, str) else None
            records.append(
                PopulationBlock(
                    source_id=config.dataset_id,
                    snapshot_id=raw.snapshot_id,
                    source_record_id=block_id,
                    record_id=f"population_block:{block_id}",
                    geometry=mapping(geometry),
                    block_id=block_id,
                    area_ha=float(area_ha),
                    residents=stored_residents,
                    residents_status=status,
                    density_per_ha=stored_density,
                    reference_period=config.reference_period,
                    residents_by_age=age_values,
                    land_use_type=land_use_values["land_use_type"],
                    land_use_type_en=land_use_values["land_use_type_en"],
                    quality_flags=tuple(flags),
                )
            )
    except SchemaError:
        raise
    except Exception as exc:
        raise SchemaError(f"Unable to normalize population-block WFS features: {exc}") from exc

    warnings = list(options.get("limitations", ()))
    warnings.append(
        f"{n_suppressed} blocks publish 0 residents (= 0–2, suppressed): stored as missing, not 0"
    )
    warnings.append(
        f"{n_observed} blocks with published residents; sum {observed_residents} is a lower bound "
        f"(true total ≤ {observed_residents} + "
        f"{options['suppression_max_residents']}·{n_suppressed})"
    )
    for flag, count in sorted(flag_counts.items()):
        warnings.append(f"{count} blocks flagged {flag}")
    if extra_properties:
        warnings.append(f"WFS returned extra properties: {', '.join(sorted(extra_properties))}")
    return NormalizedBatch(
        records=tuple(records), input_rows=len(features), warnings=tuple(warnings)
    )


def validate_population_blocks(batch: NormalizedBatch) -> QualityReport:
    errors = []
    if not batch.records:
        errors.append("WFS population-block snapshot is empty")
    if batch.rejected_rows or batch.input_rows != len(batch.records):
        errors.append("WFS population-block snapshot contains rejected or missing rows")
    seen = set()
    for record in batch.records:
        if not isinstance(record, PopulationBlock):
            errors.append("WFS population-block snapshot contains a non-population-block record")
            continue
        if record.block_id in seen:
            errors.append(f"Duplicate population block ID: {record.block_id}")
        seen.add(record.block_id)
        try:
            geometry = shape(record.geometry)
        except Exception:
            errors.append(f"Invalid population-block polygon: {record.block_id}")
            continue
        if (
            geometry.is_empty
            or not geometry.is_valid
            or geometry.geom_type not in {"Polygon", "MultiPolygon"}
        ):
            errors.append(f"Invalid population-block polygon: {record.block_id}")
    return QualityReport(
        valid=not errors,
        input_rows=batch.input_rows,
        output_rows=len(batch.records),
        rejected_rows=batch.rejected_rows,
        warnings=batch.warnings,
        errors=tuple(errors),
    )


def assign_parent(
    config: DatasetConfig, batch: NormalizedBatch, parent_records: tuple
) -> NormalizedBatch:
    options = config.parser_options
    hierarchy = options["hierarchy"]
    parents = sorted(
        (
            record
            for record in parent_records
            if isinstance(record, Area)
            and record.hierarchy == hierarchy
            and record.boundary_edition == config.boundary_edition
        ),
        key=lambda record: record.area_code,
    )
    if not parents:
        raise IncompatibleSnapshotError("Pinned parent has no compatible Planungsraum areas")
    transformer = Transformer.from_crs("EPSG:4326", "EPSG:25833", always_xy=True)
    projected_parents = [
        transform(transformer.transform, shape(record.geometry)) for record in parents
    ]
    tree = STRtree(projected_parents)
    parent_flags = {"plr_unassigned", "plr_assignment_ambiguous", "plr_assignment_partial"}
    assigned = []
    for record in batch.records:
        if not isinstance(record, PopulationBlock):
            assigned.append(record)
            continue
        block = transform(transformer.transform, shape(record.geometry))
        candidate_indices = tree.query(block, predicate="intersects")
        overlaps = []
        for index in candidate_indices:
            parent_geometry = projected_parents[int(index)]
            overlap_area = block.intersection(parent_geometry).area
            if overlap_area > 0:
                parent = parents[int(index)]
                overlaps.append((overlap_area, parent.area_code, parent))
        flags = [flag for flag in record.quality_flags if flag not in parent_flags]
        if not overlaps or block.area <= 0:
            flags.append("plr_unassigned")
            assigned.append(
                replace(
                    record,
                    area_code=None,
                    hierarchy=None,
                    boundary_edition=None,
                    area_overlap_share=None,
                    quality_flags=tuple(flags),
                )
            )
            continue
        largest_overlap_area = max(item[0] for item in overlaps)
        tied_overlaps = [
            item
            for item in overlaps
            if math.isclose(item[0], largest_overlap_area, rel_tol=1e-10, abs_tol=1e-9)
        ]
        _, _, parent = min(tied_overlaps, key=lambda item: item[1])
        share = min(1.0, largest_overlap_area / block.area)
        rounded_share = round(share, 4)
        if rounded_share == 0:
            rounded_share = share
        flags.append(
            "plr_assignment_ambiguous"
            if share < options["ambiguous_share_threshold"]
            else "plr_assignment_partial"
            if share < options["partial_share_threshold"]
            else ""
        )
        flags = [flag for flag in flags if flag]
        assigned.append(
            replace(
                record,
                area_code=parent.area_code,
                hierarchy=parent.hierarchy,
                boundary_edition=parent.boundary_edition,
                area_overlap_share=rounded_share,
                quality_flags=tuple(flags),
            )
        )
    return replace(batch, records=tuple(assigned))


def check_parent(config: DatasetConfig, records: tuple, parent_records: tuple) -> dict:
    options = config.parser_options
    if any(not isinstance(record, PopulationBlock) for record in records):
        raise IncompatibleSnapshotError("Population-block snapshot contains a non-block record")
    if not any(record.hierarchy is not None for record in records):
        raise IncompatibleSnapshotError("Population blocks have not been assigned to a parent")
    parents = [
        record
        for record in parent_records
        if isinstance(record, Area)
        and record.hierarchy == options["hierarchy"]
        and record.boundary_edition == config.boundary_edition
    ]
    assigned = [record for record in records if record.hierarchy is not None]
    unassigned = [record for record in records if record.hierarchy is None]
    if records and len(unassigned) / len(records) > options["max_unassigned_share"]:
        raise IncompatibleSnapshotError(
            f"Unassigned population blocks exceed max_unassigned_share: "
            f"{len(unassigned)}/{len(records)}"
        )
    ambiguous = [record for record in records if "plr_assignment_ambiguous" in record.quality_flags]
    partial = [record for record in records if "plr_assignment_partial" in record.quality_flags]
    area_codes = {record.area_code for record in assigned}
    parent_codes = {record.area_code for record in parents}
    areas_without_blocks = sorted(parent_codes - area_codes)
    return {
        "parent_dataset": options.get("parent_dataset", "berlin_lor"),
        "parent_area_count": len(parent_codes),
        "assignment": {
            "crs": "EPSG:25833",
            "method": "whole block assigned to the planungsraum with the largest overlap area",
            "ambiguous_share_threshold": options["ambiguous_share_threshold"],
            "partial_share_threshold": options["partial_share_threshold"],
            "assigned": len(assigned),
            "unassigned": len(unassigned),
            "ambiguous": len(ambiguous),
            "partial": len(partial),
            "unassigned_examples": sorted(record.block_id for record in unassigned)[:10],
            "ambiguous_examples": [
                {
                    "block_id": record.block_id,
                    "area_code": record.area_code,
                    "share": record.area_overlap_share,
                }
                for record in sorted(ambiguous, key=lambda item: item.block_id)[:10]
            ],
        },
        "areas_with_blocks": len(area_codes),
        "areas_without_blocks": len(areas_without_blocks),
        "areas_without_blocks_examples": areas_without_blocks[:10],
        "limitations": [
            "Block geometry (ISU5, Raumbezug Umweltatlas 2022) and LOR geometry are not reconciled; partial overlaps are reported, not split."
        ],
    }
