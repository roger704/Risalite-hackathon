"""Bounded ALKIS parcel acquisition and normalization using the reviewed Berlin WFS."""

from __future__ import annotations

import math
import re
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from pyproj import Transformer
from shapely import make_valid
from shapely.geometry import MultiPolygon, box, mapping, shape
from shapely.ops import transform
from shapely.strtree import STRtree

from app.connectors.base import FileConnector
from app.connectors.berlin_wfs import BerlinWFSConnector, parse_page
from app.domain import (
    Area,
    ConfigError,
    DatasetConfig,
    FetchRequest,
    NormalizedBatch,
    Parcel,
    QualityReport,
    RawBundle,
    SchemaError,
)


def _bbox(value, name: str) -> tuple[float, float, float, float]:
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 4
        or any(
            isinstance(part, bool) or not isinstance(part, (int, float)) or not math.isfinite(part)
            for part in value
        )
        or value[0] > value[2]
        or value[1] > value[3]
    ):
        raise ConfigError(f"ALKIS {name} must be four finite ordered coordinates")
    return tuple(value)


def _validated_options(config: DatasetConfig) -> dict:
    options = config.parser_options
    for key in (
        "typename",
        "page_size",
        "max_pages",
        "max_total_bytes",
        "expected_crs",
        "extent_bbox",
        "source_bbox",
        "expected_properties",
        "id_field",
        "id_pattern",
        "parcel_id_pattern",
        "expected_count_range",
    ):
        if key not in options:
            raise ConfigError(f"ALKIS parser requires explicit {key}")
    extent = _bbox(options["extent_bbox"], "extent_bbox")
    query = parse_qs(urlsplit(config.resource_url).query, keep_blank_values=True)
    bbox_values = query.get("BBOX", [])
    if len(bbox_values) != 1:
        raise ConfigError("ALKIS resource_url must contain one reviewed BBOX")
    bbox_parts = bbox_values[0].split(",")
    try:
        resource_extent = tuple(float(value) for value in bbox_parts[:4])
    except (TypeError, ValueError) as exc:
        raise ConfigError("ALKIS resource_url BBOX is invalid") from exc
    if len(bbox_parts) != 5 or resource_extent != extent:
        raise ConfigError("ALKIS extent_bbox must match the resource_url BBOX")
    if bbox_parts[4] not in {"urn:ogc:def:crs:EPSG::25833", "EPSG:25833"}:
        raise ConfigError("ALKIS resource_url BBOX must use EPSG:25833")
    if query.get("TYPENAMES") != [options["typename"]]:
        raise ConfigError("ALKIS typename must match resource_url TYPENAMES")
    for key in ("page_size", "max_pages", "max_total_bytes"):
        if type(options[key]) is not int or options[key] <= 0:
            raise ConfigError(f"ALKIS {key} must be a positive integer")
    _bbox(options["source_bbox"], "source_bbox")
    _bbox(options.get("allowed_bbox", [13.0, 52.3, 13.9, 52.8]), "allowed_bbox")
    expected = options["expected_count_range"]
    if (
        not isinstance(expected, (list, tuple))
        or len(expected) != 2
        or any(type(value) is not int or value < 0 for value in expected)
        or expected[0] > expected[1]
    ):
        raise ConfigError("ALKIS expected_count_range must be an ordered nonnegative range")
    if not isinstance(options["expected_properties"], list) or any(
        not isinstance(name, str) for name in options["expected_properties"]
    ):
        raise ConfigError("ALKIS expected_properties must be a list of strings")
    for key in ("id_pattern", "parcel_id_pattern"):
        try:
            re.compile(options[key])
        except (TypeError, re.error) as exc:
            raise ConfigError(f"ALKIS {key} must be a valid regular expression") from exc
    if options["expected_crs"] != "EPSG:25833":
        raise ConfigError("ALKIS expected_crs must be EPSG:25833")
    return options


def _polygon_parts(geometry):
    if geometry.geom_type == "Polygon":
        return [geometry]
    if geometry.geom_type == "MultiPolygon":
        return list(geometry.geoms)
    if geometry.geom_type == "GeometryCollection":
        return [part for item in geometry.geoms for part in _polygon_parts(item)]
    return []


def _date_value(value, name: str, source_id: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise SchemaError(f"ALKIS {name} must be an ISO date or null: {source_id}")
    try:
        from datetime import date

        date.fromisoformat(value)
    except ValueError as exc:
        raise SchemaError(f"ALKIS {name} must be a valid ISO date: {source_id}") from exc
    return value


def _optional_string(value, name: str, source_id: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise SchemaError(f"ALKIS {name} must be a string or null: {source_id}")
    return value


class BerlinALKISParcelsConnector(FileConnector):
    adapter_version = "1.0.0"

    async def fetch(self, request: FetchRequest) -> RawBundle:
        config = request.config
        options = _validated_options(config)
        wfs_options = {
            "record_type": "area",
            "field_map": {"area_code": "uuid", "name": "fsko"},
            "attribute_fields": [],
            "hierarchy": "flurstueck",
            "typename": options["typename"],
            "page_size": options["page_size"],
            "max_pages": options["max_pages"],
            "max_total_bytes": options["max_total_bytes"],
            "expected_crs": options["expected_crs"],
            "source_bbox": options["source_bbox"],
            "allowed_bbox": options.get("allowed_bbox", [13.0, 52.3, 13.9, 52.8]),
            "expected_properties": options["expected_properties"],
            "id_field": options["id_field"],
            "id_pattern": options["id_pattern"],
            "expected_count_range": options["expected_count_range"],
        }
        return await BerlinWFSConnector(self.fetcher).fetch(
            replace(
                request,
                config=replace(config, parser_options=wfs_options),
            )
        )

    def normalize(self, raw: RawBundle, config: DatasetConfig) -> NormalizedBatch:
        options = _validated_options(config)
        if not raw.assets:
            raise SchemaError("ALKIS requires at least one provider WFS page")
        pages = []
        matched = None
        expected_start = 0
        for asset in raw.assets:
            query = parse_qs(urlsplit(asset.resource_url).query, keep_blank_values=True)
            if (
                query.get("TYPENAMES") != [options["typename"]]
                or len(query.get("STARTINDEX", [])) != 1
                or len(query.get("COUNT", [])) != 1
            ):
                raise SchemaError(
                    "berlin_alkis_parcels does not support --file; ingest from provider"
                )
            try:
                start = int(query["STARTINDEX"][0])
                count = int(query["COUNT"][0])
            except (KeyError, TypeError, ValueError) as exc:
                raise SchemaError("ALKIS provider page has invalid paging parameters") from exc
            if start != expected_start or count != options["page_size"]:
                raise SchemaError("ALKIS provider page paging sequence is inconsistent")
            try:
                with Path(asset.path).open("rb") as source:
                    collection = parse_page(source.read(), options)
            except SchemaError:
                raise
            except OSError as exc:
                raise SchemaError("ALKIS provider page cannot be read") from exc
            page_matched = collection["numberMatched"]
            if matched is None:
                matched = page_matched
            elif page_matched != matched:
                raise SchemaError("ALKIS numberMatched changed between pages")
            features = collection["features"]
            if len(features) > options["page_size"]:
                raise SchemaError("ALKIS page exceeded configured page_size")
            pages.extend(features)
            expected_start += len(features)
        if len(pages) != matched:
            raise SchemaError(
                f"ALKIS count mismatch: numberMatched {matched}, received {len(pages)}"
            )
        if not options["expected_count_range"][0] <= matched <= options["expected_count_range"][1]:
            raise SchemaError(
                f"ALKIS expected feature count in {options['expected_count_range']}, received {matched}"
            )

        required = set(options["expected_properties"])
        source_bounds = _bbox(options["source_bbox"], "source_bbox")
        allowed_bounds = _bbox(
            options.get("allowed_bbox", [13.0, 52.3, 13.9, 52.8]), "allowed_bbox"
        )
        transformer = Transformer.from_crs("EPSG:25833", "EPSG:4326", always_xy=True)
        seen_uuids = set()
        seen_parcel_ids = set()
        extra_properties = set()
        records = []
        missing_area_count = 0
        made_valid_count = 0
        ratio_outlier_count = 0

        for feature in pages:
            if not isinstance(feature, dict) or not isinstance(feature.get("properties"), dict):
                raise SchemaError("ALKIS feature properties must be an object")
            properties = feature["properties"]
            missing = sorted(required - properties.keys())
            if missing:
                raise SchemaError(f"ALKIS feature is missing expected properties: {missing}")
            extra_properties.update(properties.keys() - required)

            source_id = properties["uuid"]
            if not isinstance(source_id, str) or not re.fullmatch(options["id_pattern"], source_id):
                raise SchemaError(f"Invalid ALKIS UUID: {source_id!r}")
            parcel_id = properties["fsko"]
            if not isinstance(parcel_id, str) or not re.fullmatch(
                options["parcel_id_pattern"], parcel_id
            ):
                raise SchemaError(f"Invalid ALKIS parcel identifier: {parcel_id!r}")
            if source_id in seen_uuids:
                raise SchemaError(f"Duplicate ALKIS uuid: {source_id}")
            if parcel_id in seen_parcel_ids:
                raise SchemaError(f"Duplicate ALKIS fsko: {parcel_id}")
            seen_uuids.add(source_id)
            seen_parcel_ids.add(parcel_id)

            for field in ("gmk", "zae"):
                value = properties[field]
                if not isinstance(value, str) or not value:
                    raise SchemaError(f"ALKIS {field} must be a nonempty string: {source_id}")
            geometry_data = feature.get("geometry")
            if not isinstance(geometry_data, dict):
                raise SchemaError(f"Invalid or missing ALKIS geometry: {source_id}")
            try:
                source_geometry = shape(geometry_data)
            except Exception as exc:
                raise SchemaError(f"Invalid ALKIS geometry: {source_id}") from exc
            if (
                source_geometry.is_empty
                or source_geometry.geom_type not in {"Polygon", "MultiPolygon"}
                or not source_geometry.is_valid
            ):
                raise SchemaError(f"Invalid ALKIS polygon: {source_id}")
            minx, miny, maxx, maxy = source_geometry.bounds
            if not (
                source_bounds[0] <= minx <= maxx <= source_bounds[2]
                and source_bounds[1] <= miny <= maxy <= source_bounds[3]
            ):
                raise SchemaError(f"ALKIS geometry outside source_bbox: {source_id}")

            source_area = round(source_geometry.area, 2)
            geometry = transform(transformer.transform, source_geometry)
            quality_flags = []
            if not geometry.is_valid:
                repaired = make_valid(geometry)
                polygons = _polygon_parts(repaired)
                if not polygons:
                    raise SchemaError(f"ALKIS geometry became non-polygonal: {source_id}")
                geometry = MultiPolygon(polygons)
                made_valid_count += 1
                quality_flags.append("geometry_made_valid_after_reprojection")
            elif geometry.geom_type == "Polygon":
                geometry = MultiPolygon([geometry])
            if geometry.is_empty or not geometry.is_valid or geometry.geom_type != "MultiPolygon":
                raise SchemaError(f"Invalid ALKIS polygon after reprojection: {source_id}")
            west, south, east, north = geometry.bounds
            if not (
                allowed_bounds[0] <= west <= east <= allowed_bounds[2]
                and allowed_bounds[1] <= south <= north <= allowed_bounds[3]
            ):
                raise SchemaError(f"ALKIS geometry outside allowed_bbox: {source_id}")

            official_area = None
            raw_official = properties["afl"]
            if raw_official is None:
                quality_flags.append("official_area_missing")
                missing_area_count += 1
            elif raw_official == 0 and not isinstance(raw_official, bool):
                quality_flags.append("official_area_zero")
                missing_area_count += 1
            elif (
                isinstance(raw_official, bool)
                or not isinstance(raw_official, (int, float))
                or not math.isfinite(raw_official)
                or raw_official < 0
            ):
                if raw_official == "":
                    quality_flags.append("official_area_missing")
                else:
                    raw_text = repr(raw_official)[:80]
                    quality_flags.append(f"official_area_invalid:{raw_text}")
                missing_area_count += 1
            else:
                official_area = float(raw_official)
            if official_area is not None and source_area > 0:
                ratio = official_area / source_area
                if ratio < 0.9 or ratio > 1.1:
                    quality_flags.append("official_vs_geometry_area_ratio_outside_0.9_1.1")
                    ratio_outlier_count += 1

            denominator = _optional_string(properties["nen"], "nen", source_id)
            records.append(
                Parcel(
                    source_id=config.dataset_id,
                    snapshot_id=raw.snapshot_id,
                    source_record_id=source_id,
                    record_id=f"flurstueck:{source_id}",
                    geometry=mapping(geometry),
                    parcel_id=parcel_id,
                    cadastral_district_code=properties["gmk"],
                    cadastral_district_name=_optional_string(
                        properties["namgmk"], "namgmk", source_id
                    ),
                    cadastral_section=_optional_string(properties["fln"], "fln", source_id),
                    parcel_numerator=properties["zae"],
                    parcel_denominator=denominator,
                    municipality_code=_optional_string(properties["gdz"], "gdz", source_id),
                    municipality_name=_optional_string(properties["namgem"], "namgem", source_id),
                    responsible_office=_optional_string(properties["dst"], "dst", source_id),
                    official_area_m2=official_area,
                    geometry_area_m2=source_area,
                    created_date=_date_value(properties["zde"], "zde", source_id),
                    lifespan_start=_date_value(properties["beg"], "beg", source_id),
                    quality_flags=tuple(quality_flags),
                )
            )

        warnings = list(options.get("limitations", ()))
        warnings.extend(
            [
                f"Official area missing or invalid for {missing_area_count} parcels.",
                f"Geometry repaired after reprojection for {made_valid_count} parcels.",
                f"Official/geometry area ratio outside 0.9–1.1 for {ratio_outlier_count} parcels.",
                "Parcels intersecting extent_bbox EPSG:25833 "
                f"{list(options['extent_bbox'])}; edge parcels are whole geometries and extend beyond it.",
            ]
        )
        if extra_properties:
            warnings.append(
                f"ALKIS returned extra properties: {', '.join(sorted(extra_properties))}"
            )
        return NormalizedBatch(
            records=tuple(records), input_rows=len(pages), warnings=tuple(warnings)
        )

    def validate(self, batch: NormalizedBatch) -> QualityReport:
        errors = []
        if not batch.records:
            errors.append("ALKIS snapshot is empty")
        if batch.rejected_rows or batch.input_rows != len(batch.records):
            errors.append("ALKIS snapshot contains rejected or missing rows")
        record_ids = set()
        parcel_ids = set()
        for record in batch.records:
            if not isinstance(record, Parcel):
                errors.append("ALKIS snapshot contains a non-parcel record")
                continue
            if record.record_id in record_ids:
                errors.append(f"Duplicate ALKIS record_id: {record.record_id}")
            if record.parcel_id in parcel_ids:
                errors.append(f"Duplicate ALKIS parcel_id: {record.parcel_id}")
            record_ids.add(record.record_id)
            parcel_ids.add(record.parcel_id)
            try:
                geometry = shape(record.geometry)
            except Exception:
                errors.append(f"Invalid ALKIS geometry: {record.record_id}")
                continue
            if (
                geometry.is_empty
                or not geometry.is_valid
                or geometry.geom_type not in {"Polygon", "MultiPolygon"}
            ):
                errors.append(f"Invalid ALKIS polygon: {record.record_id}")
        return QualityReport(
            valid=not errors,
            input_rows=batch.input_rows,
            output_rows=len(batch.records),
            rejected_rows=batch.rejected_rows,
            warnings=batch.warnings,
            errors=tuple(errors),
        )

    def check_parent(self, config: DatasetConfig, records: tuple, parent_records: tuple) -> dict:
        options = _validated_options(config)
        if any(not isinstance(record, Parcel) for record in records) or any(
            not isinstance(record, Area) or record.hierarchy != "planungsraum"
            for record in parent_records
        ):
            raise SchemaError("ALKIS parent coverage requires parcels and LOR Areas")
        transformer = Transformer.from_crs("EPSG:4326", "EPSG:25833", always_xy=True)
        lor_geometries = [
            transform(transformer.transform, shape(record.geometry)) for record in parent_records
        ]
        tree = STRtree(lor_geometries)
        extent = box(*_bbox(options["extent_bbox"], "extent_bbox"))
        intersecting_codes = sorted(
            record.area_code
            for record, geometry in zip(parent_records, lor_geometries, strict=True)
            if geometry.intersects(extent)
        )
        outside = 0
        for record in records:
            geometry = transform(transformer.transform, shape(record.geometry))
            point = geometry.representative_point()
            if not any(lor_geometries[index].covers(point) for index in tree.query(point)):
                outside += 1
        return {
            "parent_dataset": options.get("parent_dataset", "berlin_lor"),
            "lor_snapshot": parent_records[0].snapshot_id if parent_records else None,
            "extent_bbox_epsg25833": list(options["extent_bbox"]),
            "extent_complete": True,
            "number_matched": len(records),
            "plr_codes_intersecting_extent": intersecting_codes,
            "parcels_outside_lor": outside,
            "area_method": "Parcel representative point coverage in EPSG:25833 against pinned LOR PLR polygons.",
        }
