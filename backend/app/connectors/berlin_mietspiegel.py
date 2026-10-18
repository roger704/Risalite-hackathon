"""Address-level Wohnlage classifications for the Berlin Mietspiegel 2026."""

from __future__ import annotations

import math
import re
from collections import Counter
from pathlib import Path

from pyproj import Transformer
from shapely.geometry import shape
from shapely.ops import transform
from shapely.strtree import STRtree

from app.connectors.berlin_wfs import BerlinWFSConnector, _options, parse_page
from app.domain import (
    Area,
    ConfigError,
    DatasetConfig,
    IncompatibleSnapshotError,
    NormalizedBatch,
    QualityReport,
    RawBundle,
    ResidentialLocationClass,
    SchemaError,
)

_PROPERTY_NAMES = {
    "schluessel",
    "bezname",
    "plz",
    "strasse",
    "hnr",
    "wol",
    "stadtteil",
    "plr_name",
}


class BerlinWohnlageConnector(BerlinWFSConnector):
    adapter_version = "1.0.0"

    def normalize(self, raw: RawBundle, config: DatasetConfig) -> NormalizedBatch:
        options = _options(config)
        if options["record_type"] != "residential_location_class":
            raise SchemaError("Wohnlage parser requires residential_location_class records")
        expected = set(options["expected_properties"])
        if expected != _PROPERTY_NAMES:
            raise SchemaError("Wohnlage expected_properties must match the reviewed WFS schema")
        typename = options["typename"]
        local_name = typename.split(":", 1)[-1]
        edition_year = options.get("edition_year")
        if type(edition_year) is not int or local_name != f"wohnlagenadr{edition_year}":
            raise ConfigError("Wohnlage typename does not match the configured edition_year")
        if config.source_edition != options.get("valid_from"):
            raise ConfigError("Wohnlage source_edition must match the configured validity start")
        classes = options.get("classes")
        if (
            not isinstance(classes, list)
            or not classes
            or any(not isinstance(value, str) or not value for value in classes)
        ):
            raise SchemaError("Wohnlage classes must be a nonempty list of strings")
        max_unclassified_share = options.get("max_unclassified_share")
        if (
            isinstance(max_unclassified_share, bool)
            or not isinstance(max_unclassified_share, (int, float))
            or not math.isfinite(max_unclassified_share)
            or not 0 <= max_unclassified_share <= 1
        ):
            raise SchemaError("max_unclassified_share must be between zero and one")
        if not raw.assets or len(raw.assets) > options["max_pages"]:
            raise SchemaError("Wohnlage WFS returned an invalid number of pages")

        source_bounds = options["source_bbox"]
        allowed_bounds = options.get("allowed_bbox", [13.0, 52.3, 13.9, 52.8])
        transformer = Transformer.from_crs(
            options["expected_crs"], "EPSG:4326", always_xy=True
        ).transform
        records: list[ResidentialLocationClass] = []
        seen: set[str] = set()
        number_matched: int | None = None
        page_lengths: list[int] = []
        received = 0
        id_pattern = re.compile(r"\d{8}[A-Z]{0,2}")
        house_pattern = re.compile(r"\d{3}[A-Z]{0,2}")

        for asset in raw.assets:
            collection = parse_page(Path(asset.path).read_bytes(), options)
            if number_matched is None:
                number_matched = collection["numberMatched"]
            elif collection["numberMatched"] != number_matched:
                raise SchemaError("Wohnlage WFS numberMatched changed across pages")
            features = collection["features"]
            page_lengths.append(len(features))
            received += len(features)
            for feature in features:
                if not isinstance(feature, dict):
                    raise SchemaError("Wohnlage WFS feature must be an object")
                properties = feature.get("properties")
                if not isinstance(properties, dict):
                    raise SchemaError("Wohnlage WFS feature properties must be an object")
                property_names = set(properties)
                if property_names != expected:
                    missing = sorted(expected - property_names)
                    unexpected = sorted(property_names - expected)
                    raise SchemaError(
                        f"Wohnlage WFS property schema changed: missing={missing}, "
                        f"unexpected={unexpected}"
                    )
                if any(
                    value is not None and not isinstance(value, str)
                    for value in properties.values()
                ):
                    raise SchemaError("Wohnlage WFS properties must contain only strings or null")
                values = {
                    name: value.strip() or None if isinstance(value, str) else None
                    for name, value in properties.items()
                }
                key = values["schluessel"]
                house_label = values["hnr"]
                if (
                    key is None
                    or not id_pattern.fullmatch(key)
                    or house_label is None
                    or not house_pattern.fullmatch(house_label)
                    or key[5:] != house_label
                ):
                    raise SchemaError("Wohnlage key/house-number identity does not match")
                if key in seen:
                    raise SchemaError(f"Duplicate Wohnlage address key: {key}")
                seen.add(key)
                feature_id = feature.get("id")
                expected_feature_id = f"{local_name}.{key}"
                if feature_id is not None and feature_id != expected_feature_id:
                    raise SchemaError(f"Wohnlage feature id does not match key: {feature_id}")

                geometry_value = feature.get("geometry")
                if not isinstance(geometry_value, dict) or geometry_value.get("type") != "Point":
                    raise SchemaError(f"Wohnlage geometry must be a Point: {key}")
                try:
                    source_geometry = shape(geometry_value)
                except Exception as exc:
                    raise SchemaError(f"Wohnlage point geometry is invalid: {key}") from exc
                if source_geometry.is_empty or source_geometry.geom_type != "Point":
                    raise SchemaError(f"Wohnlage point geometry is invalid: {key}")
                x, y = source_geometry.x, source_geometry.y
                if not (
                    source_bounds[0] <= x <= source_bounds[2]
                    and source_bounds[1] <= y <= source_bounds[3]
                ):
                    raise SchemaError(f"Wohnlage point outside source_bbox: {key}")
                geometry = transform(transformer, source_geometry)
                longitude, latitude = geometry.x, geometry.y
                if not (
                    allowed_bounds[0] <= longitude <= allowed_bounds[2]
                    and allowed_bounds[1] <= latitude <= allowed_bounds[3]
                ):
                    raise SchemaError(f"Wohnlage point outside allowed_bbox: {key}")

                raw_label = values["wol"]
                if raw_label in classes:
                    class_status = "assigned"
                    wohnlage_class = raw_label
                elif raw_label is None:
                    class_status = "unassigned"
                    wohnlage_class = None
                else:
                    class_status = "unmapped"
                    wohnlage_class = None
                flags = [f"field_missing:{name}" for name, value in values.items() if value is None]
                if class_status == "unmapped":
                    flags.append("class_label_unmapped")
                street_code = key[:5]
                number = int(house_label[:3])
                suffix = house_label[3:] or None
                attributes = {
                    name: value
                    for name, value in values.items()
                    if name not in {"wol", "schluessel", "hnr"} and value is not None
                }
                records.append(
                    ResidentialLocationClass(
                        source_id=config.dataset_id,
                        snapshot_id=raw.snapshot_id,
                        source_record_id=key,
                        record_id=f"wohnlage:{edition_year}:{key}",
                        geometry={
                            "type": "Point",
                            "coordinates": [longitude, latitude],
                        },
                        address_key=key,
                        street_code=street_code,
                        house_number=number,
                        house_number_suffix=suffix,
                        house_number_label=house_label,
                        street_name=values["strasse"],
                        postcode=values["plz"],
                        district_name=values["bezname"],
                        planning_area_name=values["plr_name"],
                        east_west_marker=values["stadtteil"],
                        wohnlage_class=wohnlage_class,
                        class_status=class_status,
                        source_label=raw_label,
                        edition=options["edition"],
                        valid_from=options["valid_from"],
                        valid_to=options["valid_to"],
                        attributes=attributes,
                        quality_flags=tuple(flags),
                    )
                )

        self._validation = {
            "number_matched": number_matched,
            "expected_count_range": options["expected_count_range"],
            "page_size": options["page_size"],
            "page_lengths": page_lengths,
            "max_unclassified_share": float(max_unclassified_share),
            "edition": options["edition"],
        }
        return NormalizedBatch(
            records=tuple(records),
            input_rows=received,
            warnings=tuple(options.get("limitations", ())),
        )

    def validate(self, batch: NormalizedBatch) -> QualityReport:
        expectations = getattr(self, "_validation", {})
        errors: list[str] = []
        records = batch.records
        expected_range = expectations.get("expected_count_range", (0, -1))
        number_matched = expectations.get("number_matched")
        if not expected_range[0] <= len(records) <= expected_range[1]:
            errors.append(
                f"Wohnlage count outside configured range: {len(records)} not in "
                f"[{expected_range[0]}, {expected_range[1]}]"
            )
        if number_matched != len(records):
            errors.append(
                f"Wohnlage count mismatch: numberMatched {number_matched}, received {len(records)}"
            )
        if batch.rejected_rows or batch.input_rows != len(records):
            errors.append("Wohnlage snapshot contains rejected or missing rows")
        page_lengths = expectations.get("page_lengths", [])
        page_size = expectations.get("page_size", 0)
        if any(length != page_size for length in page_lengths[:-1]):
            errors.append("Wohnlage WFS contains a short non-final page")
        if not records:
            errors.append("Wohnlage snapshot is empty")

        keys: set[str] = set()
        editions: set[str] = set()
        classes: Counter[str] = Counter()
        markers: Counter[str] = Counter()
        for record in records:
            if not isinstance(record, ResidentialLocationClass):
                errors.append("Wohnlage snapshot contains a non-Wohnlage record")
                continue
            if record.address_key in keys:
                errors.append(f"Duplicate Wohnlage address key: {record.address_key}")
            keys.add(record.address_key)
            editions.add(record.edition)
            classes[
                record.class_status if record.class_status != "assigned" else record.wohnlage_class
            ] += 1
            if record.east_west_marker in {"Ost", "West"}:
                markers[record.east_west_marker] += 1
            try:
                point = shape(record.geometry)
            except Exception:
                errors.append(f"Invalid Wohnlage point: {record.address_key}")
                continue
            if point.is_empty or point.geom_type != "Point":
                errors.append(f"Invalid Wohnlage point: {record.address_key}")
        if len(editions) > 1:
            errors.append("Wohnlage snapshot mixes editions")
        if editions and editions != {expectations.get("edition")}:
            errors.append("Wohnlage record edition does not match the configured edition")

        unclassified = classes["unassigned"] + classes["unmapped"]
        share = unclassified / len(records) if records else 0
        if share > expectations.get("max_unclassified_share", 0):
            errors.append(
                f"Unassigned and unmapped share {share:.6f} exceeds configured maximum "
                f"{expectations.get('max_unclassified_share', 0):.6f}"
            )
        warnings = list(batch.warnings)
        warnings.append(
            "Wohnlage class counts: "
            + ", ".join(
                f"{name}={classes[name]}"
                for name in ("einfach", "mittel", "gut", "unassigned", "unmapped")
            )
        )
        warnings.append(
            f"Wohnlage Ost/West marker counts: Ost={markers['Ost']}, West={markers['West']}"
        )
        return QualityReport(
            valid=not errors,
            input_rows=batch.input_rows,
            output_rows=len(records),
            rejected_rows=batch.rejected_rows,
            warnings=tuple(warnings),
            errors=tuple(errors),
        )

    def check_parent(self, config: DatasetConfig, records: tuple, parent_records: tuple) -> dict:
        options = _options(config)
        parent_options = options.get("parent_check", {})
        expected_boundary = parent_options.get("boundary_edition")
        if any(
            not isinstance(record, Area) or record.boundary_edition != expected_boundary
            for record in parent_records
        ):
            raise IncompatibleSnapshotError("Wohnlage parent must use the configured LOR edition")
        if any(not isinstance(record, ResidentialLocationClass) for record in records):
            raise IncompatibleSnapshotError("Wohnlage parent check received invalid records")
        tolerance = parent_options.get("boundary_tolerance_m", 1.0)
        max_outside_share = parent_options.get("max_outside_share", 0.001)
        if (
            isinstance(tolerance, bool)
            or not isinstance(tolerance, (int, float))
            or not math.isfinite(tolerance)
            or tolerance < 0
        ):
            raise SchemaError("parent_check boundary_tolerance_m must be nonnegative")
        if (
            isinstance(max_outside_share, bool)
            or not isinstance(max_outside_share, (int, float))
            or not math.isfinite(max_outside_share)
            or not 0 <= max_outside_share <= 1
        ):
            raise SchemaError("parent_check max_outside_share must be between zero and one")

        to_metric = Transformer.from_crs("EPSG:4326", "EPSG:25833", always_xy=True).transform
        areas = [record for record in parent_records if isinstance(record, Area)]
        area_geometries = [transform(to_metric, shape(record.geometry)) for record in areas]
        points = [transform(to_metric, shape(record.geometry)) for record in records]
        tree = STRtree(area_geometries)
        matches = tree.query(points, predicate="dwithin", distance=float(tolerance))
        candidates_by_point: list[list[int]] = [[] for _ in points]
        for point_index, area_index in zip(matches[0], matches[1], strict=True):
            candidates_by_point[int(point_index)].append(int(area_index))
        counts = [0] * len(areas)
        outside = 0
        for point_index, candidates in enumerate(candidates_by_point):
            if not candidates:
                outside += 1
                continue
            covering = [
                index for index in candidates if area_geometries[index].covers(points[point_index])
            ]
            chosen = (
                min(covering)
                if covering
                else min(
                    candidates,
                    key=lambda index: (area_geometries[index].distance(points[point_index]), index),
                )
            )
            counts[chosen] += 1
        zero_areas = sorted(
            areas[index].area_code for index, count in enumerate(counts) if count == 0
        )
        outside_share = outside / len(points) if points else 0.0
        warnings = []
        if outside:
            warnings.append(
                f"{outside} Wohnlage points ({outside_share:.6%}) are outside all LOR areas "
                "within the configured tolerance"
            )
        if zero_areas:
            warnings.append(f"{len(zero_areas)} LOR areas have no Wohnlage address points")
        if outside_share > max_outside_share:
            raise IncompatibleSnapshotError(
                f"Wohnlage points outside LOR areas share {outside_share:.6f} exceeds "
                f"{max_outside_share:.6f}"
            )
        return {
            "parent_dataset": options.get("parent_dataset", "berlin_lor"),
            "lor_snapshot_id": None,
            "parent_boundary_edition": expected_boundary,
            "join_method": f"point-in-polygon in EPSG:25833 with {tolerance:g} m tolerance",
            "point_count": len(points),
            "points_outside_all_lor_areas": outside,
            "outside_share": outside_share,
            "lor_areas_without_wohnlage_addresses_count": len(zero_areas),
            "lor_areas_without_wohnlage_addresses": zero_areas,
            "warnings": warnings,
        }
