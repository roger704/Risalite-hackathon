"""Generic WFS 2.0 GeoJSON adapter.

Fetched pages are retained as separate raw assets so each response has its own
hash and is copied in page order to ``raw/<dataset>/<snapshot>/<index>-page-NNNN.json``.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import statistics
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from xml.etree import ElementTree

from pyproj import CRS, Transformer
from shapely.geometry import mapping, shape
from shapely.ops import transform

from app.connectors.base import FileConnector
from app.connectors.berlin_wfs_roads import (
    normalize_road_segments,
    validate_road_segment_options,
    validate_road_segments,
)
from app.domain import (
    Area,
    ConfigError,
    DatasetConfig,
    FetchRequest,
    IncompatibleSnapshotError,
    NormalizedBatch,
    QualityReport,
    RawBundle,
    RoadTrafficSegment,
    SchemaError,
)
from app.infrastructure.http import HTTPFetcher


def _query_values(url: str) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for key, value in parse_qsl(urlsplit(url).query, keep_blank_values=True):
        values.setdefault(key.lower(), []).append(value)
    return values


def _query_params(url: str) -> list[tuple[str, str]]:
    return sorted(
        (key.upper(), value)
        for key, value in parse_qsl(urlsplit(url).query, keep_blank_values=True)
        if key.upper() not in {"COUNT", "STARTINDEX"}
    )


def _single_query(url: str, name: str) -> str | None:
    values = _query_values(url).get(name.lower(), [])
    if len(values) > 1:
        raise SchemaError(f"WFS URL has duplicate {name} parameters")
    return values[0] if values else None


def page_url(resource_url: str, start_index: int, count: int) -> str:
    """Return a WFS GetFeature URL with paging parameters added or replaced."""
    parts = urlsplit(resource_url)
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in {"count", "startindex"}
    ]
    query.extend((("COUNT", str(count)), ("STARTINDEX", str(start_index))))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _exception_report(text: str) -> str:
    snippet = re.sub(r"\s+", " ", text).strip()[:300]
    try:
        root = ElementTree.fromstring(text)
        if root.tag.rsplit("}", 1)[-1] == "ExceptionReport":
            exception_code, exception_text = "unknown", ""
            for element in root.iter():
                name = element.tag.rsplit("}", 1)[-1]
                if name == "Exception":
                    exception_code = element.attrib.get("exceptionCode", "unknown")
                elif name == "ExceptionText":
                    exception_text = " ".join("".join(element.itertext()).split())
            return (
                "WFS returned an OGC ExceptionReport: "
                f"{exception_code}: {exception_text or snippet}"
            )
    except ElementTree.ParseError:
        pass
    if "ExceptionReport" in text:
        code = re.search(r'exceptionCode=["\']([^"\']+)', text, re.IGNORECASE)
        message = re.search(
            r"<(?:\w+:)?ExceptionText[^>]*>(.*?)</(?:\w+:)?ExceptionText>",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        return (
            "WFS returned an OGC ExceptionReport: "
            f"{code.group(1) if code else 'unknown'}: "
            f"{re.sub(r'<[^>]+>', '', message.group(1)).strip() if message else snippet}"
        )
    if snippet.lower().startswith(("<!doctype html", "<html")) or "<html" in snippet.lower():
        return "WFS returned HTML instead of GeoJSON"
    return "WFS returned XML instead of GeoJSON"


def parse_page(data: bytes, options: dict) -> dict:
    """Decode and validate a single GeoJSON FeatureCollection page."""
    expected_crs_name = options.get("expected_crs")
    if not isinstance(expected_crs_name, str) or not expected_crs_name:
        raise SchemaError("WFS parser requires explicit expected_crs")
    try:
        text = data.decode("utf-8-sig").lstrip("\ufeff \t\r\n")
    except (AttributeError, UnicodeDecodeError) as exc:
        raise SchemaError("WFS response is not valid UTF-8 GeoJSON") from exc
    if text.startswith("<"):
        raise SchemaError(_exception_report(text))
    try:
        collection = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise SchemaError("WFS response is not valid JSON") from exc
    if not isinstance(collection, dict) or collection.get("type") != "FeatureCollection":
        raise SchemaError("WFS response is not a GeoJSON FeatureCollection")
    matched = collection.get("numberMatched")
    if type(matched) is not int or matched < 0:
        raise SchemaError("WFS numberMatched must be an integer")
    if not isinstance(collection.get("features"), list):
        raise SchemaError("WFS features must be a list")
    crs_info = collection.get("crs")
    crs_name = (
        crs_info.get("properties", {}).get("name")
        if isinstance(crs_info, dict) and isinstance(crs_info.get("properties"), dict)
        else None
    )
    if not isinstance(crs_name, str) or not crs_name:
        raise SchemaError("WFS GeoJSON CRS name is missing")
    try:
        source_crs = CRS.from_user_input(crs_name)
        expected_crs = CRS.from_user_input(expected_crs_name)
    except Exception as exc:
        raise SchemaError("WFS GeoJSON CRS is invalid") from exc
    if source_crs != expected_crs:
        raise SchemaError(f"WFS CRS differs from expected {expected_crs_name}")
    if not source_crs.axis_info or source_crs.axis_info[0].direction.lower() != "east":
        raise SchemaError("WFS CRS axis order is not easting-first")
    return collection


def _bbox(options: dict, name: str, default: list[float] | None = None) -> tuple[float, ...]:
    value = options.get(name, default)
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


def _significant(value: float) -> float:
    return float(f"{value:.3g}")


def _options(config: DatasetConfig) -> dict:
    options = config.parser_options
    required = (
        "typename",
        "page_size",
        "max_pages",
        "expected_crs",
        "source_bbox",
        "expected_properties",
        "id_field",
        "field_map",
        "attribute_fields",
        "record_type",
    )
    for name in required:
        if name not in options:
            raise SchemaError(f"WFS parser requires explicit {name}")
    url_typename = _single_query(config.resource_url, "TYPENAMES")
    if options["typename"] != url_typename:
        raise ConfigError("WFS parser typename must match resource_url TYPENAMES")
    if type(options["page_size"]) is not int or options["page_size"] <= 0:
        raise SchemaError("WFS page_size must be a positive integer")
    if type(options["max_pages"]) is not int or options["max_pages"] <= 0:
        raise SchemaError("WFS max_pages must be a positive integer")
    max_total_bytes = options.get("max_total_bytes", 50_000_000)
    if type(max_total_bytes) is not int or max_total_bytes <= 0:
        raise SchemaError("WFS max_total_bytes must be a positive integer")
    if not isinstance(options["expected_crs"], str) or not options["expected_crs"]:
        raise SchemaError("WFS expected_crs must be a nonempty string")
    _bbox(options, "source_bbox")
    _bbox(options, "allowed_bbox", [13.0, 52.3, 13.9, 52.8])
    expected_properties = options["expected_properties"]
    if not isinstance(expected_properties, list) or any(
        not isinstance(value, str) for value in expected_properties
    ):
        raise SchemaError("WFS expected_properties must be a list of strings")
    if not isinstance(options["id_field"], str) or not options["id_field"]:
        raise SchemaError("WFS id_field must be a nonempty string")
    if options.get("id_pattern") is not None:
        if not isinstance(options["id_pattern"], str):
            raise SchemaError("WFS id_pattern must be a valid regular expression")
        try:
            re.compile(options["id_pattern"])
        except (TypeError, re.error) as exc:
            raise SchemaError("WFS id_pattern must be a valid regular expression") from exc
    has_count = "expected_count" in options
    has_count_range = "expected_count_range" in options
    if has_count == has_count_range:
        raise SchemaError("WFS requires exactly one of expected_count or expected_count_range")
    expected_count = options.get("expected_count")
    if has_count and (type(expected_count) is not int or expected_count < 0):
        raise SchemaError("WFS expected_count must be a nonnegative integer")
    count_range = options.get("expected_count_range")
    if has_count_range and (
        not isinstance(count_range, (list, tuple))
        or len(count_range) != 2
        or any(type(value) is not int or value < 0 for value in count_range)
        or count_range[0] > count_range[1]
    ):
        raise SchemaError("WFS expected_count_range must contain ordered nonnegative integers")
    if options["record_type"] == "residential_location_class":
        return options
    if options["record_type"] not in {"area", "road_traffic_segment"}:
        raise SchemaError(f"unsupported record_type: {options['record_type']}")
    if options["record_type"] == "area":
        field_map = options["field_map"]
        if not isinstance(field_map, dict) or any(
            not isinstance(field_map.get(key), str) or not field_map[key]
            for key in ("area_code", "name")
        ):
            raise SchemaError("WFS field_map must map area_code and name to properties")
        attributes = options["attribute_fields"]
        if not isinstance(attributes, list) or any(
            not isinstance(value, str) for value in attributes
        ):
            raise SchemaError("WFS attribute_fields must be a list of property names")
        if not isinstance(options.get("hierarchy"), str) or not options["hierarchy"].strip():
            raise SchemaError("WFS hierarchy must be a nonempty string")
        geometry_types = options.get("geometry_types", ["Polygon", "MultiPolygon"])
        if not isinstance(geometry_types, list) or any(
            value not in {"Polygon", "MultiPolygon"} for value in geometry_types
        ):
            raise SchemaError("WFS geometry_types must list Polygon and/or MultiPolygon")
    else:
        validate_road_segment_options(options)
    return options


class BerlinWFSConnector(FileConnector):
    """Reviewed WFS 2.0 GeoJSON paging and polygon normalization."""

    adapter_version = "1.1.0"

    async def fetch(self, request: FetchRequest) -> RawBundle:
        if request.config.parser_options.get("record_type") == "development_plan":
            from app.connectors.berlin_wfs_plans import fetch_development_plans

            return await fetch_development_plans(self, request)
        config = request.config
        if not config.enabled or config.access_validation == "blocked":
            raise ConfigError("Dataset is disabled or blocked")
        options = _options(config)
        page_size = options["page_size"]
        max_pages = options["max_pages"]
        max_total_bytes = options.get("max_total_bytes", 50_000_000)
        assets = []
        urls = []
        total_bytes = 0
        matched = None
        received = 0
        next_url = page_url(config.resource_url, 0, page_size)
        for page_number in range(max_pages):
            urls.append(next_url)
            asset = await self.fetcher.download(
                replace(config, resource_url=next_url),
                Path(request.staging_dir) / f"page-{page_number:04d}.json",
            )
            total_bytes += asset.size_bytes
            if total_bytes > max_total_bytes:
                raise SchemaError("WFS cumulative download exceeded max_total_bytes")
            with Path(asset.path).open("rb") as source:
                collection = parse_page(source.read(), options)
            if matched is None:
                matched = collection["numberMatched"]
            elif collection["numberMatched"] != matched:
                raise SchemaError("WFS numberMatched changed between pages")
            features = collection["features"]
            if not features and received < matched:
                raise SchemaError("WFS returned an empty page before completion")
            if len(features) > page_size:
                raise SchemaError("WFS page exceeded configured page_size")
            received += len(features)
            if received > matched:
                raise SchemaError("WFS returned more features than numberMatched")
            assets.append(asset)
            if received == matched:
                break
            if page_number + 1 == max_pages:
                raise SchemaError("WFS page cap exceeded before all features were received")
            links = collection.get("links", [])
            next_links = (
                [link for link in links if isinstance(link, dict) and link.get("rel") == "next"]
                if isinstance(links, list)
                else []
            )
            if len(next_links) != 1 or not isinstance(next_links[0].get("href"), str):
                raise SchemaError("WFS omitted the next link before all features were received")
            href = next_links[0]["href"]
            try:
                HTTPFetcher.validate_url(href, config.allowed_hosts)
            except ConfigError as exc:
                raise SchemaError("WFS next link URL is not allowed") from exc
            source_parts, link_parts = urlsplit(config.resource_url), urlsplit(href)
            if (
                link_parts.scheme.lower() != source_parts.scheme.lower()
                or (link_parts.hostname or "").lower() != (source_parts.hostname or "").lower()
                or link_parts.port != source_parts.port
                or link_parts.path != source_parts.path
            ):
                raise SchemaError("WFS next link changed the reviewed resource origin or path")
            if _query_params(href) != _query_params(config.resource_url):
                raise SchemaError("WFS next link changed reviewed query parameters")
            if _single_query(href, "TYPENAMES") != options["typename"]:
                raise SchemaError("WFS next link changed TYPENAMES")
            try:
                link_count = int(_single_query(href, "COUNT") or "")
                link_start = int(_single_query(href, "STARTINDEX") or "")
            except (TypeError, ValueError, SchemaError) as exc:
                raise SchemaError("WFS next link has invalid COUNT or STARTINDEX") from exc
            if link_count != page_size or link_start != received:
                raise SchemaError("WFS next link COUNT or STARTINDEX is inconsistent")
            next_url = href
        else:
            raise SchemaError("WFS page cap exceeded before all features were received")

        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "resource_url": config.resource_url,
                    "page_size": page_size,
                    "typename": options["typename"],
                    "page_urls": urls,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        return RawBundle(
            assets=tuple(assets),
            retrieved_at=datetime.now(timezone.utc),
            snapshot_id=request.snapshot_id,
            request_fingerprint=fingerprint,
        )

    def normalize(self, raw: RawBundle, config: DatasetConfig) -> NormalizedBatch:
        if config.parser_options.get("record_type") == "development_plan":
            from app.connectors.berlin_wfs_plans import normalize_development_plans

            return normalize_development_plans(raw, config)
        options = _options(config)
        if not raw.assets:
            raise SchemaError("WFS requires at least one GeoJSON asset")
        pages = []
        matched = None
        for asset in raw.assets:
            try:
                with Path(asset.path).open("rb") as source:
                    collection = parse_page(source.read(), options)
            except SchemaError:
                raise
            except Exception as exc:
                raise SchemaError(f"Unable to read WFS GeoJSON asset: {exc}") from exc
            if matched is None:
                matched = collection["numberMatched"]
            elif collection["numberMatched"] != matched:
                raise SchemaError("WFS numberMatched changed between raw assets")
            pages.append(collection)
        features = [feature for collection in pages for feature in collection["features"]]
        if len(features) != matched:
            raise SchemaError(
                f"WFS count mismatch: numberMatched {matched}, received {len(features)}"
            )
        expected = options.get("expected_count")
        count_range = options.get("expected_count_range")
        if expected is not None and matched != expected:
            raise SchemaError(f"WFS expected {expected} features, received {matched}")
        if count_range is not None and not count_range[0] <= matched <= count_range[1]:
            raise SchemaError(f"WFS expected feature count in {count_range}, received {matched}")
        if options["record_type"] == "road_traffic_segment":
            return normalize_road_segments(features, options, config, raw)

        source_bounds = _bbox(options, "source_bbox")
        allowed_bounds = _bbox(options, "allowed_bbox", [13.0, 52.3, 13.9, 52.8])
        geometry_types = options.get("geometry_types", ["Polygon", "MultiPolygon"])
        required_properties = set(options["expected_properties"])
        required_properties.update(
            (options["id_field"], options["field_map"]["area_code"], options["field_map"]["name"])
        )
        required_properties.update(options["attribute_fields"])
        records = []
        seen_ids = set()
        extra_properties = set()
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
                source_id = properties[options["id_field"]]
                if not isinstance(source_id, str) or not source_id:
                    raise SchemaError("WFS ID must be a nonempty string")
                if options.get("id_pattern") and not re.fullmatch(options["id_pattern"], source_id):
                    raise SchemaError(f"WFS ID does not match id_pattern: {source_id!r}")
                if source_id in seen_ids:
                    raise SchemaError(f"Duplicate WFS ID: {source_id}")
                seen_ids.add(source_id)
                area_code = properties[options["field_map"]["area_code"]]
                if not isinstance(area_code, str) or not area_code:
                    raise SchemaError(f"Invalid WFS area code for ID {source_id}")
                name = properties[options["field_map"]["name"]]
                if not isinstance(name, str) or not name.strip():
                    raise SchemaError(f"Invalid WFS area name for ID {source_id}")
                geometry_data = feature.get("geometry")
                if not isinstance(geometry_data, dict):
                    raise SchemaError(f"Invalid or missing WFS geometry: {source_id}")
                try:
                    geometry = shape(geometry_data)
                except Exception as exc:
                    raise SchemaError(f"Invalid WFS geometry: {source_id}") from exc
                if geometry.is_empty or geometry.geom_type not in geometry_types:
                    raise SchemaError(f"Unsupported WFS geometry type: {source_id}")
                minx, miny, maxx, maxy = geometry.bounds
                if not (
                    source_bounds[0] <= minx <= maxx <= source_bounds[2]
                    and source_bounds[1] <= miny <= maxy <= source_bounds[3]
                ):
                    raise SchemaError(f"WFS geometry outside source_bbox: {source_id}")
                geometry = transform(transformer.transform, geometry)
                if geometry.is_empty or not geometry.is_valid:
                    raise SchemaError(f"Invalid WFS polygon after reprojection: {source_id}")
                west, south, east, north = geometry.bounds
                if not (
                    allowed_bounds[0] <= west <= east <= allowed_bounds[2]
                    and allowed_bounds[1] <= south <= north <= allowed_bounds[3]
                ):
                    raise SchemaError(f"WFS geometry outside allowed_bbox: {source_id}")
                attributes = {}
                quality_flags = []
                for key in options["attribute_fields"]:
                    value = properties[key]
                    if value is None:
                        quality_flags.append(f"attribute_missing:{key}")
                    elif isinstance(value, str):
                        attributes[key] = value
                    else:
                        raise SchemaError(f"WFS attribute {key} must be a string or null")
                records.append(
                    Area(
                        source_id=config.dataset_id,
                        snapshot_id=raw.snapshot_id,
                        source_record_id=source_id,
                        record_id=f"{options['hierarchy']}:{source_id}",
                        geometry=mapping(geometry),
                        area_code=area_code,
                        name=name,
                        hierarchy=options["hierarchy"],
                        boundary_edition=config.boundary_edition,
                        attributes=attributes,
                        quality_flags=tuple(quality_flags),
                    )
                )
        except SchemaError:
            raise
        except Exception as exc:
            raise SchemaError(f"Unable to normalize WFS features: {exc}") from exc
        warnings = list(options.get("limitations", ()))
        if extra_properties:
            warnings.append(f"WFS returned extra properties: {', '.join(sorted(extra_properties))}")
        return NormalizedBatch(
            records=tuple(records),
            input_rows=len(features),
            warnings=tuple(warnings),
        )

    def validate(self, batch: NormalizedBatch) -> QualityReport:
        if batch.records and batch.records[0].record_type == "development_plan":
            from app.connectors.berlin_wfs_plans import validate_development_plans

            return validate_development_plans(batch)
        if batch.records and all(
            isinstance(record, RoadTrafficSegment) for record in batch.records
        ):
            return validate_road_segments(batch)
        errors = []
        if not batch.records:
            errors.append("WFS snapshot is empty")
        if batch.rejected_rows or batch.input_rows != len(batch.records):
            errors.append("WFS snapshot contains rejected or missing rows")
        keys = set()
        editions = set()
        for record in batch.records:
            if not isinstance(record, Area):
                errors.append("WFS snapshot contains a non-area record")
                continue
            key = (record.hierarchy, record.area_code)
            if key in keys:
                errors.append(f"Duplicate WFS area key: {key}")
            keys.add(key)
            editions.add(record.boundary_edition)
            try:
                geometry = shape(record.geometry)
            except Exception:
                errors.append(f"Invalid WFS polygon: {record.area_code}")
                continue
            if (
                geometry.is_empty
                or not geometry.is_valid
                or geometry.geom_type not in {"Polygon", "MultiPolygon"}
            ):
                errors.append(f"Invalid WFS polygon: {record.area_code}")
        if len(editions) > 1:
            errors.append("WFS snapshot mixes boundary editions")
        return QualityReport(
            valid=not errors,
            input_rows=batch.input_rows,
            output_rows=len(batch.records),
            rejected_rows=batch.rejected_rows,
            warnings=batch.warnings,
            errors=tuple(errors),
        )

    def check_parent(self, config: DatasetConfig, records: tuple, parent_records: tuple) -> dict:
        if config.parser_options.get("record_type") == "development_plan":
            from app.connectors.berlin_wfs_plans import check_development_plan_parent

            return check_development_plan_parent(records, parent_records)
        options = _options(config)
        invalid_records = sum(
            not isinstance(record, Area) for record in (*records, *parent_records)
        )
        edition_mismatches = sum(
            isinstance(record, Area) and record.boundary_edition != config.boundary_edition
            for record in (*records, *parent_records)
        )
        child_keys = {
            (record.hierarchy, record.area_code) for record in records if isinstance(record, Area)
        }
        parent_keys = {
            (record.hierarchy, record.area_code)
            for record in parent_records
            if isinstance(record, Area)
        }
        missing = parent_keys - child_keys
        unmatched = child_keys - parent_keys
        if invalid_records or edition_mismatches or missing or unmatched:
            missing_codes = sorted(code for _, code in missing)[:5]
            unmatched_codes = sorted(code for _, code in unmatched)[:5]
            raise IncompatibleSnapshotError(
                "WFS/parent coverage differs: "
                f"{len(missing)} missing from WFS, {len(unmatched)} not in parent; "
                f"examples missing={missing_codes}, not_in_parent={unmatched_codes}; "
                f"non-area records={invalid_records}, boundary-edition mismatches={edition_mismatches}"
            )

        threshold = options.get("geometry_difference_threshold", 0.01)
        if (
            isinstance(threshold, bool)
            or not isinstance(threshold, (int, float))
            or not math.isfinite(threshold)
            or threshold < 0
        ):
            raise SchemaError("WFS geometry_difference_threshold must be nonnegative")
        transformer = Transformer.from_crs("EPSG:4326", "EPSG:25833", always_xy=True)
        parent_by_key = {
            (record.hierarchy, record.area_code): record
            for record in parent_records
            if isinstance(record, Area)
        }
        ratios = []
        for record in records:
            key = (record.hierarchy, record.area_code)
            wfs_geometry = transform(transformer.transform, shape(record.geometry))
            parent_geometry = transform(transformer.transform, shape(parent_by_key[key].geometry))
            parent_area = parent_geometry.area
            ratio = (
                wfs_geometry.symmetric_difference(parent_geometry).area / parent_area
                if parent_area > 0
                else 0.0
                if wfs_geometry.symmetric_difference(parent_geometry).area == 0
                else 1.0
            )
            ratios.append((record.area_code, ratio))
        ordered = sorted(ratios, key=lambda item: item[1], reverse=True)
        ratio_values = [ratio for _, ratio in ratios]
        parent_dataset = options.get("parent_dataset", "berlin_lor")
        return {
            "parent_dataset": parent_dataset,
            "parent_code_match": "exact",
            "parent_area_count": len(parent_keys),
            "geometry_comparison": {
                "crs": "EPSG:25833",
                "metric": "symmetric_difference_area / parent_area",
                "threshold": threshold,
                "areas_over_threshold": sum(ratio > threshold for ratio in ratio_values),
                "max_ratio": _significant(max(ratio_values, default=0.0)),
                "median_ratio": _significant(statistics.median(ratio_values))
                if ratio_values
                else 0.0,
                "top_differences": [
                    {"area_code": code, "ratio": _significant(ratio)}
                    for code, ratio in ordered[:10]
                    if ratio > 0
                ],
            },
            "limitations": [
                "WFS geometry stand 2021-01-01 vs pinned LOR shapefile release; "
                "geometry differences are reported, not reconciled."
            ],
        }
