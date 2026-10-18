"""Berlin address-point WFS normalization and pinned-LOR coverage checks."""

from __future__ import annotations

import math
import re
from collections import Counter
from datetime import date
from pathlib import Path

import numpy as np
import shapely
from pyproj import Transformer
from shapely.geometry import shape
from shapely.ops import transform

from app.connectors.berlin_wfs import BerlinWFSConnector, _options, parse_page
from app.domain import (
    Address,
    Area,
    DatasetConfig,
    IncompatibleSnapshotError,
    NormalizedBatch,
    QualityReport,
    RawBundle,
    SchemaError,
    ValidationError,
)

ADDRESS_FIELDS = (
    "id",
    "street_name",
    "street_code",
    "house_number",
    "house_number_suffix",
    "postcode",
    "district_code",
    "district_name",
    "locality_code",
    "locality_name",
    "area_code",
    "area_name",
    "address_type",
)


def _address_options(config: DatasetConfig) -> dict:
    options = _options(config)
    if options["record_type"] != "address":
        raise SchemaError("Berlin address parser requires record_type address")
    expected = set(options["expected_properties"])
    field_map = options["field_map"]
    if not isinstance(field_map, dict) or set(field_map) != set(ADDRESS_FIELDS):
        raise SchemaError("Berlin address field_map must explicitly map all address fields")
    if any(
        not isinstance(value, str) or not value or value not in expected
        for value in field_map.values()
    ):
        raise SchemaError("Berlin address field_map values must be expected property names")
    if field_map["id"] != options["id_field"]:
        raise SchemaError("Berlin address id_field must match field_map id")
    patterns = options.get("field_patterns")
    if not isinstance(patterns, dict):
        raise SchemaError("Berlin address field_patterns must be an object")
    for prop, pattern in patterns.items():
        if prop not in expected or not isinstance(pattern, str):
            raise SchemaError(
                "Berlin address field_patterns must map expected properties to regexes"
            )
        try:
            re.compile(pattern)
        except re.error as exc:
            raise SchemaError(f"Invalid address field pattern for {prop}") from exc
    address_types = options.get("address_types")
    if (
        not isinstance(address_types, list)
        or not address_types
        or any(not isinstance(value, str) or not value.strip() for value in address_types)
    ):
        raise SchemaError("Berlin address address_types must be a nonempty list of strings")
    attributes = options["attribute_fields"]
    if (
        not isinstance(attributes, list)
        or any(not isinstance(value, str) or value not in expected for value in attributes)
        or len(attributes) != len(set(attributes))
    ):
        raise SchemaError("Berlin address attribute_fields must be expected property names")
    if not isinstance(options["hierarchy"], str) or not options["hierarchy"].strip():
        raise SchemaError("Berlin address hierarchy must be a nonempty string")
    limits = options.get("parent_check", {})
    if not isinstance(limits, dict):
        raise SchemaError("Berlin address parent_check must be an object")
    for name, default in (
        ("max_unknown_area_codes", 0),
        ("max_outside_supplied_area_count", 0),
    ):
        value = limits.get(name, default)
        if type(value) is not int or value < 0:
            raise SchemaError(f"Berlin address {name} must be a nonnegative integer")
    tolerance = limits.get("boundary_tolerance_m", 1.0)
    if (
        isinstance(tolerance, bool)
        or not isinstance(tolerance, (int, float))
        or not math.isfinite(tolerance)
        or tolerance < 0
    ):
        raise SchemaError("Berlin address boundary_tolerance_m must be nonnegative")
    return options


class BerlinAddressConnector(BerlinWFSConnector):
    adapter_version = "1.0.0"

    def normalize(self, raw: RawBundle, config: DatasetConfig) -> NormalizedBatch:
        options = _address_options(config)
        if len(raw.assets) > options["max_pages"]:
            raise SchemaError("Berlin address WFS exceeded max_pages")
        expected_properties = set(options["expected_properties"])
        field_map = options["field_map"]
        field_patterns = {
            name: re.compile(pattern) for name, pattern in options["field_patterns"].items()
        }
        id_pattern = re.compile(options["id_pattern"])
        source_bbox = options["source_bbox"]
        allowed_bbox = options.get("allowed_bbox", [13.0, 52.3, 13.9, 52.8])
        transform_point = Transformer.from_crs(
            options["expected_crs"], "EPSG:4326", always_xy=True
        ).transform
        records: list[Address] = []
        seen: set[str] = set()
        extra_properties: set[str] = set()
        type_counts: Counter[str] = Counter()
        quality_counts: Counter[tuple[str, str]] = Counter()
        missing_house_number = 0
        missing_area_code = 0
        max_address_date = None
        max_street_date = None
        number_matched = None
        received = 0

        for asset in raw.assets:
            collection = parse_page(Path(asset.path).read_bytes(), options)
            matched = collection["numberMatched"]
            if number_matched is None:
                number_matched = matched
            elif number_matched != matched:
                raise SchemaError(
                    "Berlin address WFS numberMatched changed across pages: "
                    f"{number_matched} then {matched}"
                )
            features = collection["features"]
            for feature in features:
                properties = feature.get("properties") if isinstance(feature, dict) else None
                if not isinstance(properties, dict):
                    raise SchemaError(
                        "Berlin address feature has invalid properties for adressid <missing>"
                    )
                absent = expected_properties - properties.keys()
                if absent:
                    raise SchemaError(
                        "Berlin address feature is missing expected properties "
                        f"{sorted(absent)} for adressid {properties.get('adressid', '<missing>')}"
                    )
                extra_properties.update(properties.keys() - expected_properties)
                adressid = properties.get(options["id_field"])
                label = adressid if isinstance(adressid, str) else "<missing>"
                normalized: dict[str, str | int | None] = {}
                for prop in expected_properties:
                    value = properties[prop]
                    if prop == "hnr":
                        if value is not None and (type(value) is not int or value < 0):
                            raise SchemaError(f"Invalid hnr type for adressid {label}")
                        normalized[prop] = value
                        continue
                    if value is not None and not isinstance(value, str):
                        raise SchemaError(f"Invalid {prop} type for adressid {label}")
                    text = value.strip() if isinstance(value, str) else None
                    normalized[prop] = text or None
                    if text and prop in field_patterns and not field_patterns[prop].fullmatch(text):
                        raise SchemaError(f"Invalid {prop} pattern for adressid {label}")
                    if text and prop in {"adr_datum", "str_datum"}:
                        try:
                            date.fromisoformat(text)
                        except ValueError as exc:
                            raise SchemaError(
                                f"Invalid {prop} calendar date for adressid {label}"
                            ) from exc

                adressid = normalized[options["id_field"]]
                if not isinstance(adressid, str) or not id_pattern.fullmatch(adressid):
                    raise SchemaError(f"Invalid adressid {label}")
                if adressid in seen:
                    raise SchemaError(f"Duplicate WFS ID: {adressid}")
                seen.add(adressid)
                street_name = normalized[field_map["street_name"]]
                address_type = normalized[field_map["address_type"]]
                if street_name is None or address_type is None:
                    raise SchemaError(f"Missing required address field for adressid {adressid}")
                if address_type not in options["address_types"]:
                    raise SchemaError(
                        f"Unexpected address type for adressid {adressid}: {address_type}"
                    )

                feature_geometry = feature.get("geometry")
                if (
                    not isinstance(feature_geometry, dict)
                    or feature_geometry.get("type") != "Point"
                ):
                    raise SchemaError(f"Invalid Point geometry for adressid {adressid}")
                coordinates = feature_geometry.get("coordinates")
                if (
                    not isinstance(coordinates, (list, tuple))
                    or len(coordinates) != 2
                    or any(
                        isinstance(value, bool)
                        or not isinstance(value, (int, float))
                        or not math.isfinite(value)
                        for value in coordinates
                    )
                ):
                    raise SchemaError(f"Invalid Point coordinates for adressid {adressid}")
                x, y = coordinates
                if not (
                    source_bbox[0] <= x <= source_bbox[2] and source_bbox[1] <= y <= source_bbox[3]
                ):
                    raise SchemaError(f"Point outside source_bbox for adressid {adressid}")
                longitude, latitude = transform_point(x, y)
                if not (
                    allowed_bbox[0] <= longitude <= allowed_bbox[2]
                    and allowed_bbox[1] <= latitude <= allowed_bbox[3]
                ):
                    raise SchemaError(f"Point outside allowed_bbox for adressid {adressid}")
                geometry = {"type": "Point", "coordinates": [longitude, latitude]}

                mapped = {key: normalized[prop] for key, prop in field_map.items()}
                flags = []
                for key, value in mapped.items():
                    if key == "id" or value is not None:
                        continue
                    if key == "house_number":
                        flags.append("house_number_missing")
                        missing_house_number += 1
                    elif key == "area_code":
                        flags.append("area_code_missing")
                        missing_area_code += 1
                    else:
                        flags.append(f"field_missing:{key}")
                attributes = {}
                for prop in options["attribute_fields"]:
                    value = normalized[prop]
                    if value is None:
                        flags.append(f"attribute_missing:{prop}")
                    else:
                        attributes[prop] = value
                try:
                    record = Address(
                        source_id=config.dataset_id,
                        snapshot_id=raw.snapshot_id,
                        source_record_id=adressid,
                        record_id=f"address:{adressid}",
                        geometry=geometry,
                        street_name=street_name,
                        address_type=address_type,
                        street_code=mapped["street_code"],
                        house_number=mapped["house_number"],
                        house_number_suffix=mapped["house_number_suffix"],
                        postcode=mapped["postcode"],
                        district_code=mapped["district_code"],
                        district_name=mapped["district_name"],
                        locality_code=mapped["locality_code"],
                        locality_name=mapped["locality_name"],
                        area_code=mapped["area_code"],
                        area_name=mapped["area_name"],
                        attributes=attributes,
                        quality_flags=tuple(flags),
                    )
                except ValidationError as exc:
                    raise SchemaError(
                        f"Invalid normalized address for adressid {adressid}: {exc}"
                    ) from exc
                records.append(record)
                received += 1
                type_counts[address_type] += 1
                quality_counts[(address_type, attributes.get("qualitaet", "missing"))] += 1
                for prop, target in (
                    ("adr_datum", "address"),
                    ("str_datum", "street"),
                ):
                    value = normalized[prop]
                    if value is not None and (
                        target == "address"
                        and (max_address_date is None or value > max_address_date)
                    ):
                        max_address_date = value
                    if value is not None and (
                        target == "street" and (max_street_date is None or value > max_street_date)
                    ):
                        max_street_date = value
            del features
            del collection

        if number_matched is None or received != number_matched:
            raise SchemaError(
                f"Berlin address count mismatch: numberMatched {number_matched}, received {received}"
            )
        expected_count = options.get("expected_count")
        count_range = options.get("expected_count_range")
        if expected_count is not None and received != expected_count:
            raise SchemaError(
                f"Berlin address count differs from expected_count {expected_count}: {received}"
            )
        if count_range is not None and not count_range[0] <= received <= count_range[1]:
            raise SchemaError(
                f"Berlin address count {received} outside expected_count_range {count_range}"
            )
        warnings = list(options.get("limitations", ()))
        warnings.extend(
            f"Address type counts: {key}={count}" for key, count in sorted(type_counts.items())
        )
        warnings.extend(
            f"Address type/qualitaet counts: {typ}/{quality}={count}"
            for (typ, quality), count in sorted(quality_counts.items())
        )
        warnings.extend(
            (
                f"Maximum adr_datum observed: {max_address_date or 'none'}",
                f"Maximum str_datum observed: {max_street_date or 'none'}",
                f"Addresses without house number: {missing_house_number}",
                f"Addresses without area code: {missing_area_code}",
            )
        )
        if extra_properties:
            warnings.append(f"WFS returned extra properties: {', '.join(sorted(extra_properties))}")
        return NormalizedBatch(
            records=tuple(records),
            input_rows=received,
            warnings=tuple(warnings),
        )

    def validate(self, batch: NormalizedBatch) -> QualityReport:
        errors = []
        if not batch.records:
            errors.append("Berlin address snapshot is empty")
        if batch.rejected_rows or batch.input_rows != len(batch.records):
            errors.append("Berlin address snapshot contains rejected or missing rows")
        if any(not isinstance(record, Address) for record in batch.records):
            errors.append("Berlin address snapshot contains non-address records")
        record_ids = [record.record_id for record in batch.records]
        if len(record_ids) != len(set(record_ids)):
            errors.append("Berlin address snapshot contains duplicate record IDs")
        return QualityReport(
            valid=not errors,
            input_rows=batch.input_rows,
            output_rows=len(batch.records),
            rejected_rows=batch.rejected_rows,
            warnings=batch.warnings,
            errors=tuple(errors),
        )

    def check_parent(self, config: DatasetConfig, records, parent_records) -> dict:
        options = _address_options(config)
        limits = options.get("parent_check", {})
        hierarchy = options["hierarchy"]
        if config.boundary_edition != "2021":
            raise IncompatibleSnapshotError("Berlin addresses require LOR boundary edition 2021")
        if any(not isinstance(record, Address) for record in records):
            raise IncompatibleSnapshotError(
                "Berlin address child snapshot contains non-address rows"
            )
        if any(not isinstance(record, Area) for record in parent_records):
            raise IncompatibleSnapshotError("Pinned LOR snapshot contains non-area rows")
        mismatched_areas = [
            area
            for area in parent_records
            if area.hierarchy == hierarchy and area.boundary_edition != config.boundary_edition
        ]
        if mismatched_areas:
            raise IncompatibleSnapshotError(
                "Berlin address and pinned LOR boundary editions differ"
            )
        areas = {
            area.area_code: area
            for area in parent_records
            if area.hierarchy == hierarchy and area.boundary_edition == config.boundary_edition
        }
        if not areas:
            raise IncompatibleSnapshotError(
                f"Pinned LOR snapshot has no {hierarchy} areas for boundary edition "
                f"{config.boundary_edition}"
            )

        coded = [record for record in records if record.area_code is not None]
        unknown_codes = sorted(
            {record.area_code for record in coded if record.area_code not in areas}
        )
        if len(unknown_codes) > limits.get("max_unknown_area_codes", 0):
            raise IncompatibleSnapshotError(
                f"Berlin addresses have {len(unknown_codes)} area codes absent from the pinned "
                f"LOR snapshot (maximum {limits.get('max_unknown_area_codes', 0)}): "
                f"{unknown_codes[:10]}"
            )

        tolerance = limits.get("boundary_tolerance_m", 1.0)
        codes = sorted(areas)
        polygons = [shape(areas[code].geometry) for code in codes]
        transformer = Transformer.from_crs("EPSG:4326", "EPSG:25833", always_xy=True)
        projected_polygons = [transform(transformer.transform, polygon) for polygon in polygons]
        code_positions = {code: index for index, code in enumerate(codes)}
        matched = [record for record in coded if record.area_code in areas]
        outside_ids: list[str] = []
        if matched:
            coordinates = np.asarray([record.geometry["coordinates"] for record in matched])
            x, y = transformer.transform(coordinates[:, 0], coordinates[:, 1])
            supplied = np.asarray(
                [projected_polygons[code_positions[record.area_code]] for record in matched],
                dtype=object,
            )
            inside = shapely.dwithin(supplied, shapely.points(x, y), tolerance)
            outside_ids = sorted(
                record.record_id for record, is_inside in zip(matched, inside) if not is_inside
            )
        if len(outside_ids) > limits.get("max_outside_supplied_area_count", 0):
            raise IncompatibleSnapshotError(
                f"{len(outside_ids)} Berlin address points are outside their supplied LOR areas "
                f"(maximum {limits.get('max_outside_supplied_area_count', 0)})"
            )
        addressed_codes = {record.area_code for record in coded}
        return {
            "address_count": len(records),
            "parent_area_count": len(areas),
            "area_code_missing_count": len(records) - len(coded),
            "unknown_area_codes": unknown_codes[:50],
            "areas_without_address_count": len(set(areas) - addressed_codes),
            "outside_supplied_area_count": len(outside_ids),
            "outside_supplied_area_record_ids": outside_ids,
            "boundary_tolerance_m": tolerance,
            "join_method": (
                "supplied plr_nr matched to pinned LOR snapshot; point-in-polygon in EPSG:25833; "
                "supplied codes reported, never reassigned"
            ),
        }
