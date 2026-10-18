"""Road-segment normalization for reviewed Berlin WFS layers."""

from __future__ import annotations

import math
import re
from collections import Counter
from datetime import datetime

from pyproj import Transformer
from shapely.geometry import mapping, shape
from shapely.ops import transform

from app.domain import (
    ConfigError,
    DatasetConfig,
    NormalizedBatch,
    QualityReport,
    RoadTrafficSegment,
    SchemaError,
)


def validate_road_segment_options(options: dict) -> None:
    field_map = options.get("field_map")
    if not isinstance(field_map, dict) or any(
        not isinstance(field_map.get(key), str) or not field_map[key]
        for key in ("name", "motor_vehicles_per_day", "length_m")
    ):
        raise SchemaError(
            "Road WFS field_map must map name, motor_vehicles_per_day, and length_m to properties"
        )
    volume_fields = options.get("volume_fields")
    if not isinstance(volume_fields, dict) or any(
        not isinstance(property_name, str)
        or not property_name
        or not isinstance(class_name, str)
        or not class_name
        for property_name, class_name in volume_fields.items()
    ):
        raise SchemaError("Road WFS volume_fields must map property names to class keys")
    if len(set(volume_fields.values())) != len(volume_fields):
        raise SchemaError("Road WFS volume_fields class keys must be unique")
    attributes = options.get("attribute_fields")
    if not isinstance(attributes, list) or any(
        not isinstance(value, str) or not value for value in attributes
    ):
        raise SchemaError("Road WFS attribute_fields must be a list of property names")
    if len(set(attributes)) != len(attributes):
        raise SchemaError("Road WFS attribute_fields must not contain duplicates")
    geometry_types = options.get("geometry_types", ["LineString", "MultiLineString"])
    if (
        not isinstance(geometry_types, list)
        or not geometry_types
        or any(
            not isinstance(value, str) or value not in {"LineString", "MultiLineString"}
            for value in geometry_types
        )
    ):
        raise SchemaError("Road WFS geometry_types must list LineString and/or MultiLineString")


def _bbox(options: dict, name: str) -> tuple[float, float, float, float]:
    value = options.get(name)
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 4
        or any(
            isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item)
            for item in value
        )
        or value[0] > value[2]
        or value[1] > value[3]
    ):
        raise SchemaError(f"WFS parser requires a valid {name}")
    return tuple(value)


def _nonnegative_integer(value, field_name: str, source_id: str) -> int:
    if type(value) is not int or value < 0:
        raise SchemaError(f"WFS {field_name} must be a nonnegative integer for ID {source_id}")
    return value


def _source_warning(counts: Counter[str]) -> str:
    map_count = counts.get("Verkehrsmengenkarte 2019", 0) + counts.get(
        "Verkehrsmengenkarte 2019 (Korrektur)", 0
    )
    visum_count = sum(
        count for name, count in counts.items() if name.casefold().startswith("visum")
    )
    plausibility_count = counts.get("Plausibilitätsprüfung VMZ", 0)
    traffic_count = 0
    traffic_dates = []
    other = []
    for name, count in sorted(counts.items()):
        if name in {"Verkehrsmengenkarte 2019", "Verkehrsmengenkarte 2019 (Korrektur)"}:
            continue
        if name.casefold().startswith("visum") or name == "Plausibilitätsprüfung VMZ":
            continue
        match = re.fullmatch(r"Zählung\s+(\d{8})", name)
        if name.startswith("Zählung "):
            traffic_count += count
            if match:
                try:
                    traffic_dates.append(
                        datetime.strptime(match.group(1), "%Y%m%d").date().isoformat()
                    )
                except ValueError:
                    pass
        else:
            other.append(f"{name} {count}")
    parts = [
        f"Verkehrsmengenkarte 2019 (incl. Korrektur) {map_count}",
        f"VISUM model {visum_count}",
        f"VMZ plausibility check {plausibility_count}",
        f"traffic counts {traffic_count}",
    ]
    if traffic_dates:
        parts[-1] += f" (dates {min(traffic_dates)}..{max(traffic_dates)})"
    parts.extend(other)
    return f"DTV source (herkunft): {', '.join(parts)}"


def normalize_road_segments(
    features: list[dict],
    options: dict,
    config: DatasetConfig,
    raw,
) -> NormalizedBatch:
    if not config.reference_period:
        raise ConfigError("Road WFS requires reference_period")
    try:
        source_bounds = _bbox(options, "source_bbox")
        allowed_bounds = _bbox(options, "allowed_bbox")
        geometry_types = options.get("geometry_types", ["LineString", "MultiLineString"])
        field_map = options["field_map"]
        required_properties = set(options["expected_properties"])
        required_properties.update(
            (
                options["id_field"],
                field_map["name"],
                field_map["motor_vehicles_per_day"],
                field_map["length_m"],
                *options["volume_fields"],
                *options["attribute_fields"],
            )
        )
        transformer = Transformer.from_crs(options["expected_crs"], "EPSG:4326", always_xy=True)
    except ConfigError:
        raise
    except Exception as exc:
        raise SchemaError(f"Invalid road WFS parser options: {exc}") from exc

    records = []
    seen_ids = set()
    extra_properties = set()
    source_counts: Counter[str] = Counter()
    missing_dtv = 0
    network_length_m = 0.0
    try:
        for feature in features:
            if not isinstance(feature, dict) or not isinstance(feature.get("properties"), dict):
                raise SchemaError("WFS feature properties must be an object")
            properties = feature["properties"]
            missing = sorted(required_properties - properties.keys())
            if missing:
                raise SchemaError(f"WFS feature is missing expected properties: {missing}")
            extra_properties.update(properties.keys() - set(options["expected_properties"]))
            source_id = properties[options["id_field"]]
            if not isinstance(source_id, str) or not source_id.strip():
                raise SchemaError("WFS ID must be a nonempty string")
            if options.get("id_pattern") and not re.fullmatch(options["id_pattern"], source_id):
                raise SchemaError(f"WFS ID does not match id_pattern: {source_id!r}")
            if source_id in seen_ids:
                raise SchemaError(f"Duplicate WFS ID: {source_id}")
            seen_ids.add(source_id)

            geometry_data = feature.get("geometry")
            if not isinstance(geometry_data, dict):
                raise SchemaError(f"Invalid or missing WFS geometry: {source_id}")
            try:
                source_geometry = shape(geometry_data)
            except Exception as exc:
                raise SchemaError(f"Invalid WFS geometry: {source_id}") from exc
            if (
                source_geometry.is_empty
                or source_geometry.geom_type not in geometry_types
                or not source_geometry.is_valid
            ):
                raise SchemaError(f"Unsupported WFS line geometry: {source_id}")
            minx, miny, maxx, maxy = source_geometry.bounds
            if not all(math.isfinite(value) for value in (minx, miny, maxx, maxy)) or not (
                source_bounds[0] <= minx <= maxx <= source_bounds[2]
                and source_bounds[1] <= miny <= maxy <= source_bounds[3]
            ):
                raise SchemaError(f"WFS geometry outside source_bbox: {source_id}")
            network_length_m += source_geometry.length
            geometry = transform(transformer.transform, source_geometry)
            if geometry.is_empty or not geometry.is_valid:
                raise SchemaError(f"Invalid WFS line after reprojection: {source_id}")
            west, south, east, north = geometry.bounds
            if not (
                allowed_bounds[0] <= west <= east <= allowed_bounds[2]
                and allowed_bounds[1] <= south <= north <= allowed_bounds[3]
            ):
                raise SchemaError(f"WFS geometry outside allowed_bbox: {source_id}")

            quality_flags = []
            name_value = properties[field_map["name"]]
            if name_value is None or isinstance(name_value, str) and not name_value.strip():
                name = None
                quality_flags.append("name_missing")
            elif isinstance(name_value, str):
                name = name_value
            else:
                raise SchemaError(f"WFS name must be a string or null for ID {source_id}")

            length_value = properties[field_map["length_m"]]
            if length_value is None:
                source_length_m = None
                quality_flags.append("length_missing")
            else:
                source_length_m = _nonnegative_integer(length_value, "length_m", source_id)

            dtv_value = properties[field_map["motor_vehicles_per_day"]]
            if dtv_value is None:
                dtv = None
                missing_dtv += 1
                quality_flags.append("volume_missing:dtv")
            else:
                dtv = _nonnegative_integer(dtv_value, "dtv", source_id)

            class_volumes = {}
            all_classes_present = True
            for property_name, class_name in options["volume_fields"].items():
                value = properties[property_name]
                if value is None:
                    all_classes_present = False
                    quality_flags.append(f"volume_missing:{property_name}")
                else:
                    class_volumes[class_name] = _nonnegative_integer(
                        value, property_name, source_id
                    )
            if dtv is not None and all_classes_present and sum(class_volumes.values()) != dtv:
                quality_flags.append("dtv_class_sum_mismatch")

            attributes = {}
            for key in options["attribute_fields"]:
                value = properties[key]
                if value is None or isinstance(value, str) and not value.strip():
                    quality_flags.append(f"attribute_missing:{key}")
                elif isinstance(value, str):
                    attributes[key] = value
                else:
                    raise SchemaError(f"WFS attribute {key} must be a string or null")
            if isinstance(attributes.get("herkunft"), str):
                source_counts[attributes["herkunft"]] += 1

            records.append(
                RoadTrafficSegment(
                    source_id=config.dataset_id,
                    snapshot_id=raw.snapshot_id,
                    source_record_id=source_id,
                    record_id=f"road_segment:{source_id}",
                    geometry=mapping(geometry),
                    reference_period=config.reference_period,
                    dtv_motor_vehicles_per_day=dtv,
                    vehicle_class_volumes_per_day=class_volumes,
                    name=name,
                    source_length_m=source_length_m,
                    attributes=attributes,
                    quality_flags=tuple(quality_flags),
                )
            )
    except SchemaError:
        raise
    except Exception as exc:
        raise SchemaError(f"Unable to normalize WFS road segments: {exc}") from exc

    warnings = list(options.get("limitations", ()))
    if extra_properties:
        warnings.append(f"WFS returned extra properties: {', '.join(sorted(extra_properties))}")
    warnings.append(f"Segments with missing DTV: {missing_dtv}")
    warnings.append(_source_warning(source_counts))
    warnings.append(f"Total network length: {network_length_m / 1000:.1f} km")
    return NormalizedBatch(
        records=tuple(records), input_rows=len(features), warnings=tuple(warnings)
    )


def validate_road_segments(batch: NormalizedBatch) -> QualityReport:
    errors = []
    if not batch.records:
        errors.append("WFS snapshot is empty")
    if batch.rejected_rows or batch.input_rows != len(batch.records):
        errors.append("WFS snapshot contains rejected or missing rows")
    record_ids = set()
    for record in batch.records:
        if not isinstance(record, RoadTrafficSegment):
            errors.append("WFS snapshot contains a non-road-traffic record")
            continue
        if record.record_id in record_ids:
            errors.append(f"Duplicate WFS road record ID: {record.record_id}")
        record_ids.add(record.record_id)
        try:
            geometry = shape(record.geometry)
        except Exception:
            errors.append(f"Invalid WFS line geometry: {record.record_id}")
            continue
        if (
            geometry.is_empty
            or not geometry.is_valid
            or geometry.geom_type not in {"LineString", "MultiLineString"}
        ):
            errors.append(f"Invalid WFS line geometry: {record.record_id}")
    return QualityReport(
        valid=not errors,
        input_rows=batch.input_rows,
        output_rows=len(batch.records),
        rejected_rows=batch.rejected_rows,
        warnings=batch.warnings,
        errors=tuple(errors),
    )
