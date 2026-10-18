"""Validation and normalization for Berlin development-plan WFS layers."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pyproj import Transformer
from shapely import make_valid
from shapely.geometry import mapping, shape
from shapely.ops import transform

from app.domain import (
    Area,
    ConfigError,
    DatasetConfig,
    DevelopmentPlan,
    FetchRequest,
    NormalizedBatch,
    QualityReport,
    RawBundle,
    SchemaError,
)
from app.infrastructure.http import HTTPFetcher

from .berlin_wfs import _bbox, _query_params, _single_query, page_url, parse_page

_PLAN_STATUSES = {
    "in_progress",
    "adopted",
    "partially_in_force",
    "repealed",
}
_FIELD_MAP_KEYS = {
    "plan_id",
    "name",
    "raw_status",
    "district",
    "plan_type",
    "procedure_type",
    "area_description",
    "land_use_text",
    "aufstellungsbeschluss_date",
    "festsetzung_date",
}


def _options(config: DatasetConfig) -> dict:
    options = config.parser_options
    required = (
        "typename",
        "page_size",
        "max_pages",
        "expected_crs",
        "source_bbox",
        "allowed_bbox",
        "id_field",
        "id_pattern",
        "layers",
        "field_map",
        "status_map",
        "date_fields",
        "min_plausible_date",
        "url_fields",
        "attribute_fields",
        "record_type",
    )
    for name in required:
        if name not in options:
            raise SchemaError(f"Development-plan WFS requires explicit {name}")
    if options["record_type"] != "development_plan":
        raise SchemaError("Development-plan normalizer requires record_type development_plan")
    if _single_query(config.resource_url, "TYPENAMES") != options["typename"]:
        raise ConfigError("WFS parser typename must match resource_url TYPENAMES")
    for name in ("page_size", "max_pages"):
        if type(options[name]) is not int or options[name] <= 0:
            raise SchemaError(f"Development-plan WFS {name} must be a positive integer")
    max_total_bytes = options.get("max_total_bytes", 50_000_000)
    if type(max_total_bytes) is not int or max_total_bytes <= 0:
        raise SchemaError("Development-plan WFS max_total_bytes must be a positive integer")
    if not isinstance(options["expected_crs"], str) or not options["expected_crs"]:
        raise SchemaError("Development-plan WFS expected_crs must be a nonempty string")
    _bbox(options, "source_bbox")
    _bbox(options, "allowed_bbox")
    if not isinstance(options["id_field"], str) or not options["id_field"]:
        raise SchemaError("Development-plan WFS id_field must be a nonempty string")
    pattern = options["id_pattern"]
    if not isinstance(pattern, str):
        raise SchemaError("Development-plan WFS id_pattern must be a regular expression")
    try:
        re.compile(pattern)
    except re.error as exc:
        raise SchemaError(
            "Development-plan WFS id_pattern must be a valid regular expression"
        ) from exc
    layers = options["layers"]
    if not isinstance(layers, list) or not layers:
        raise SchemaError("Development-plan WFS layers must be a nonempty list")
    seen_layers = set()
    for layer in layers:
        if not isinstance(layer, dict):
            raise SchemaError("Development-plan WFS layer definitions must be objects")
        typename = layer.get("typename")
        count_range = layer.get("expected_count_range")
        statuses = layer.get("allowed_statuses")
        properties = layer.get("expected_properties")
        if not isinstance(typename, str) or not typename or typename in seen_layers:
            raise SchemaError("Development-plan WFS layer typenames must be unique strings")
        seen_layers.add(typename)
        if (
            not isinstance(count_range, list | tuple)
            or len(count_range) != 2
            or any(type(value) is not int or value < 0 for value in count_range)
            or count_range[0] > count_range[1]
        ):
            raise SchemaError(f"Invalid expected_count_range for {typename}")
        if not isinstance(statuses, list) or any(value not in _PLAN_STATUSES for value in statuses):
            raise SchemaError(f"Invalid allowed_statuses for {typename}")
        if not isinstance(properties, list) or any(
            not isinstance(value, str) or not value for value in properties
        ):
            raise SchemaError(f"Invalid expected_properties for {typename}")
    field_map = options["field_map"]
    if not isinstance(field_map, dict) or any(
        not isinstance(field_map.get(key), str) or not field_map[key] for key in _FIELD_MAP_KEYS
    ):
        raise SchemaError("Development-plan field_map must map all plan fields to properties")
    status_map = options["status_map"]
    if not isinstance(status_map, dict) or any(
        not isinstance(raw, str) or mapped not in _PLAN_STATUSES
        for raw, mapped in status_map.items()
    ):
        raise SchemaError("Development-plan status_map has an invalid status mapping")
    for name in ("date_fields", "url_fields", "attribute_fields"):
        values = options[name]
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise SchemaError(f"Development-plan {name} must be a list of property names")
    try:
        date.fromisoformat(options["min_plausible_date"])
    except (TypeError, ValueError) as exc:
        raise SchemaError("Development-plan min_plausible_date must be an ISO date") from exc
    return options


def _layer_resource_url(resource_url: str, typename: str) -> str:
    parts = urlsplit(resource_url)
    query = [
        (key, typename if key.lower() == "typenames" else value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
    ]
    if not any(key.lower() == "typenames" for key, _ in query):
        raise SchemaError("WFS resource URL is missing TYPENAMES")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _validate_next_link(
    href: str,
    current_resource: str,
    config: DatasetConfig,
    typename: str,
    page_size: int,
    received: int,
) -> None:
    try:
        HTTPFetcher.validate_url(href, config.allowed_hosts)
    except ConfigError as exc:
        raise SchemaError("WFS next link URL is not allowed") from exc
    source_parts, link_parts = urlsplit(current_resource), urlsplit(href)
    if (
        link_parts.scheme.lower() != source_parts.scheme.lower()
        or (link_parts.hostname or "").lower() != (source_parts.hostname or "").lower()
        or link_parts.port != source_parts.port
        or link_parts.path != source_parts.path
    ):
        raise SchemaError("WFS next link changed the reviewed resource origin or path")
    if _query_params(href) != _query_params(current_resource):
        raise SchemaError("WFS next link changed reviewed query parameters")
    if _single_query(href, "TYPENAMES") != typename:
        raise SchemaError("WFS next link changed TYPENAMES")
    try:
        count = int(_single_query(href, "COUNT") or "")
        start = int(_single_query(href, "STARTINDEX") or "")
    except (TypeError, ValueError, SchemaError) as exc:
        raise SchemaError("WFS next link has invalid COUNT or STARTINDEX") from exc
    if count != page_size or start != received:
        raise SchemaError("WFS next link COUNT or STARTINDEX is inconsistent")


async def fetch_development_plans(connector, request: FetchRequest) -> RawBundle:
    config = request.config
    if not config.enabled or config.access_validation == "blocked":
        raise ConfigError("Dataset is disabled or blocked")
    options = _options(config)
    layers = options["layers"]
    page_size = options["page_size"]
    max_pages = options["max_pages"]
    max_total_bytes = options.get("max_total_bytes", 50_000_000)
    assets = []
    page_urls = []
    total_bytes = 0

    for layer_index, layer in enumerate(layers):
        typename = layer["typename"]
        resource_url = _layer_resource_url(config.resource_url, typename)
        received = 0
        matched = None
        next_url = page_url(resource_url, 0, page_size)
        for page_number in range(max_pages):
            page_urls.append(next_url)
            asset = await connector.fetcher.download(
                replace(config, resource_url=next_url),
                Path(request.staging_dir) / f"layer-{layer_index:02d}-page-{page_number:04d}.json",
            )
            total_bytes += asset.size_bytes
            if total_bytes > max_total_bytes:
                raise SchemaError("WFS cumulative download exceeded max_total_bytes")
            with Path(asset.path).open("rb") as source:
                collection = parse_page(source.read(), options)
            if matched is None:
                matched = collection["numberMatched"]
            elif collection["numberMatched"] != matched:
                raise SchemaError(f"WFS numberMatched changed between pages for {typename}")
            features = collection["features"]
            if not features and received < matched:
                raise SchemaError(f"WFS returned an empty page before completion for {typename}")
            if len(features) > page_size:
                raise SchemaError("WFS page exceeded configured page_size")
            received += len(features)
            if received > matched:
                raise SchemaError(f"WFS returned more features than numberMatched for {typename}")
            assets.append(asset)
            if received == matched:
                break
            if page_number + 1 == max_pages:
                raise SchemaError(
                    f"WFS page cap exceeded before all features were received for {typename}"
                )
            links = collection.get("links", [])
            next_links = (
                [item for item in links if isinstance(item, dict) and item.get("rel") == "next"]
                if isinstance(links, list)
                else []
            )
            if len(next_links) != 1 or not isinstance(next_links[0].get("href"), str):
                raise SchemaError(
                    f"WFS omitted the next link before all features were received for {typename}"
                )
            href = next_links[0]["href"]
            _validate_next_link(href, resource_url, config, typename, page_size, received)
            next_url = href
        else:
            raise SchemaError(
                f"WFS page cap exceeded before all features were received for {typename}"
            )

    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "resource_url": config.resource_url,
                "page_size": page_size,
                "layers": [layer["typename"] for layer in layers],
                "page_urls": page_urls,
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


def _optional_string(value, field_name: str, plan_id: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise SchemaError(f"WFS {field_name} must be a string or null for plan {plan_id}")
    return value


def _date_value(value) -> tuple[str | None, bool]:
    if value is None or value == "":
        return None, False
    if not isinstance(value, str):
        return None, True
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}Z", value):
            parsed = date.fromisoformat(value[:-1])
        else:
            parsed = date.fromisoformat(value)
            return parsed.isoformat(), False
    except ValueError:
        try:
            parsed_datetime = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None, True
        return parsed_datetime.date().isoformat(), False
    else:
        return parsed.isoformat(), False


def normalize_development_plans(raw: RawBundle, config: DatasetConfig) -> NormalizedBatch:
    options = _options(config)
    if not raw.assets:
        raise SchemaError("WFS requires at least one GeoJSON asset")
    if len(raw.assets) == 1 and (
        Path(raw.assets[0].path).name == "source"
        or Path(raw.assets[0].path).name.startswith("source.")
    ):
        raise SchemaError(
            "berlin_planning requires provider download of all three bplan layers; "
            "single-file local import is not supported"
        )
    layers = {layer["typename"]: layer for layer in options["layers"]}
    page_data: dict[str, list[dict]] = {typename: [] for typename in layers}
    page_matched: dict[str, int] = {}
    page_counts: Counter[str] = Counter()
    for asset in raw.assets:
        try:
            typename = _single_query(asset.resource_url, "TYPENAMES")
            if typename not in layers:
                raise SchemaError(f"Unknown or missing WFS layer in raw asset: {typename}")
            with Path(asset.path).open("rb") as source:
                collection = parse_page(source.read(), options)
        except SchemaError:
            raise
        except Exception as exc:
            raise SchemaError(f"Unable to read WFS GeoJSON asset: {exc}") from exc
        if typename in page_matched and page_matched[typename] != collection["numberMatched"]:
            raise SchemaError(f"WFS numberMatched changed between raw assets for {typename}")
        page_matched[typename] = collection["numberMatched"]
        page_data[typename].extend(collection["features"])
        page_counts[typename] += 1

    if set(page_counts) != set(layers):
        missing = sorted(set(layers) - set(page_counts))
        raise SchemaError(f"WFS snapshot is missing layer pages: {missing}")

    source_bounds = _bbox(options, "source_bbox")
    allowed_bounds = _bbox(options, "allowed_bbox")
    field_map = options["field_map"]
    extra_properties = set()
    known_properties = {key for layer in options["layers"] for key in layer["expected_properties"]}
    records = []
    seen_ids = set()
    matched_by_layer = {}
    normalized_by_layer = {}
    unmapped_statuses = Counter()
    status_counts = Counter()
    mismatch_counts = Counter()
    no_document_links = 0
    implausible_dates = Counter()
    reprojected_repairs = 0
    minimum_date = date.fromisoformat(options["min_plausible_date"])
    retrieved_date = raw.retrieved_at.date()
    try:
        transformer = Transformer.from_crs(options["expected_crs"], "EPSG:4326", always_xy=True)
        for typename, layer in layers.items():
            features = page_data[typename]
            matched = page_matched[typename]
            matched_by_layer[typename] = matched
            if matched != len(features):
                raise SchemaError(
                    f"WFS count mismatch for {typename}: numberMatched {matched}, "
                    f"received {len(features)}"
                )
            expected_range = layer["expected_count_range"]
            if not expected_range[0] <= matched <= expected_range[1]:
                raise SchemaError(
                    f"WFS expected {typename} feature count in {expected_range}, received {matched}"
                )
            if page_counts[typename] > options["max_pages"]:
                raise SchemaError(f"WFS page cap exceeded for {typename}")
            normalized_by_layer[typename] = len(features)
            for feature in features:
                if not isinstance(feature, dict) or not isinstance(feature.get("properties"), dict):
                    raise SchemaError("WFS feature properties must be an object")
                properties = feature["properties"]
                missing = sorted(set(layer["expected_properties"]) - properties.keys())
                if missing:
                    raise SchemaError(
                        f"WFS feature in {typename} is missing expected properties: {missing}"
                    )
                extra_properties.update(properties.keys() - known_properties)
                plan_id = properties.get(options["id_field"])
                if not isinstance(plan_id, str) or not plan_id:
                    raise SchemaError("WFS plan ID must be a nonempty string")
                if not re.fullmatch(options["id_pattern"], plan_id):
                    raise SchemaError(f"WFS plan ID does not match id_pattern: {plan_id!r}")
                if plan_id in seen_ids:
                    raise SchemaError(f"Duplicate WFS plan ID across layers: {plan_id}")
                seen_ids.add(plan_id)

                geometry_data = feature.get("geometry")
                if not isinstance(geometry_data, dict):
                    raise SchemaError(f"Invalid or missing WFS geometry: {plan_id}")
                try:
                    geometry = shape(geometry_data)
                except Exception as exc:
                    raise SchemaError(f"Invalid WFS geometry: {plan_id}") from exc
                if geometry.is_empty or geometry.geom_type not in {"Polygon", "MultiPolygon"}:
                    raise SchemaError(f"Unsupported WFS geometry type: {plan_id}")
                if not geometry.is_valid:
                    raise SchemaError(f"Invalid WFS polygon: {plan_id}")
                minx, miny, maxx, maxy = geometry.bounds
                if not (
                    source_bounds[0] <= minx <= maxx <= source_bounds[2]
                    and source_bounds[1] <= miny <= maxy <= source_bounds[3]
                ):
                    raise SchemaError(f"WFS geometry outside source_bbox: {plan_id}")
                geometry = transform(transformer.transform, geometry)
                if geometry.is_empty:
                    raise SchemaError(f"Invalid WFS polygon after reprojection: {plan_id}")
                repaired_after_reprojection = not geometry.is_valid
                if repaired_after_reprojection:
                    geometry = make_valid(geometry)
                if (
                    geometry.is_empty
                    or not geometry.is_valid
                    or geometry.geom_type not in {"Polygon", "MultiPolygon"}
                ):
                    raise SchemaError(f"Invalid WFS polygon after reprojection: {plan_id}")
                west, south, east, north = geometry.bounds
                if not (
                    allowed_bounds[0] <= west <= east <= allowed_bounds[2]
                    and allowed_bounds[1] <= south <= north <= allowed_bounds[3]
                ):
                    raise SchemaError(f"WFS geometry outside allowed_bbox: {plan_id}")

                raw_status = _optional_string(
                    properties.get(field_map["raw_status"]), "raw_status", plan_id
                )
                status = options["status_map"].get(raw_status, "unmapped")
                status_counts[status] += 1
                flags = []
                if repaired_after_reprojection:
                    flags.append("geometry_repaired_after_reprojection")
                    reprojected_repairs += 1
                if status == "unmapped":
                    flags.append("status_unmapped")
                    unmapped_statuses[raw_status if raw_status is not None else "<null>"] += 1
                elif status not in layer["allowed_statuses"]:
                    flags.append("status_layer_mismatch")
                    mismatch_counts[f"{typename}:{status}"] += 1

                dates = {}
                for field_name in options["date_fields"]:
                    if field_name not in properties:
                        continue
                    date_value, unparseable = _date_value(properties[field_name])
                    if unparseable:
                        flags.append(f"date_unparseable:{field_name}")
                    elif date_value is not None:
                        dates[field_name] = date_value
                        parsed = date.fromisoformat(date_value)
                        if parsed < minimum_date or parsed > retrieved_date + timedelta(days=1):
                            flags.append(f"date_implausible:{field_name}")
                            implausible_dates[field_name] += 1

                document_urls = {}
                for field_name in options["url_fields"]:
                    if field_name not in properties:
                        continue
                    value = properties[field_name]
                    if value is None or value == "":
                        continue
                    if not isinstance(value, str):
                        flags.append(f"document_url_rejected:{field_name}")
                        continue
                    try:
                        parsed_url = urlsplit(value)
                        valid_url = parsed_url.scheme.lower() in {"http", "https"} and bool(
                            parsed_url.netloc and parsed_url.hostname
                        )
                    except ValueError:
                        valid_url = False
                    if valid_url:
                        document_urls[field_name] = value
                    else:
                        flags.append(f"document_url_rejected:{field_name}")
                if not document_urls:
                    flags.append("no_document_links")
                    no_document_links += 1

                attributes = {}
                for field_name in options["attribute_fields"]:
                    if field_name not in properties or properties[field_name] is None:
                        continue
                    value = properties[field_name]
                    text = value if isinstance(value, str) else str(value)
                    if text:
                        attributes[field_name] = text

                def mapped_text(name):
                    return _optional_string(properties.get(field_map[name]), name, plan_id)

                records.append(
                    DevelopmentPlan(
                        source_id=config.dataset_id,
                        snapshot_id=raw.snapshot_id,
                        source_record_id=plan_id,
                        record_id=f"{config.dataset_id}:{plan_id}",
                        geometry=mapping(geometry),
                        plan_id=plan_id,
                        name=mapped_text("name"),
                        status=status,
                        raw_status=raw_status,
                        source_layer=typename,
                        district=mapped_text("district"),
                        plan_type=mapped_text("plan_type"),
                        procedure_type=mapped_text("procedure_type"),
                        area_description=mapped_text("area_description"),
                        land_use_text=mapped_text("land_use_text"),
                        aufstellungsbeschluss_date=dates.get(
                            field_map["aufstellungsbeschluss_date"]
                        ),
                        festsetzung_date=dates.get(field_map["festsetzung_date"]),
                        dates=dates,
                        document_urls=document_urls,
                        attributes=attributes,
                        quality_flags=tuple(flags),
                    )
                )
    except SchemaError:
        raise
    except Exception as exc:
        raise SchemaError(f"Unable to normalize WFS development plans: {exc}") from exc

    warnings = list(options.get("limitations", ()))
    for typename in layers:
        warnings.append(
            f"{typename}: numberMatched {matched_by_layer[typename]}, "
            f"normalized {normalized_by_layer[typename]}"
        )
    warnings.append(
        "Development-plan status counts: "
        + ", ".join(f"{status}={count}" for status, count in sorted(status_counts.items()))
    )
    if unmapped_statuses:
        warnings.append(
            "Unmapped raw development-plan statuses: "
            + ", ".join(
                f"{status!r}={count}" for status, count in sorted(unmapped_statuses.items())
            )
        )
    if mismatch_counts:
        warnings.append(
            "Development-plan status/layer mismatches: "
            + ", ".join(f"{key}={count}" for key, count in sorted(mismatch_counts.items()))
        )
    warnings.append(f"Development plans without document links: {no_document_links}")
    if reprojected_repairs:
        warnings.append(f"WGS84 geometry repair after reprojection: {reprojected_repairs} plan(s)")
    if implausible_dates:
        warnings.append(
            "Implausible development-plan dates: "
            + ", ".join(
                f"{field_name}={count}" for field_name, count in sorted(implausible_dates.items())
            )
        )
    if extra_properties:
        warnings.append(f"WFS returned extra properties: {', '.join(sorted(extra_properties))}")
    return NormalizedBatch(
        records=tuple(records),
        input_rows=sum(matched_by_layer.values()),
        warnings=tuple(warnings),
    )


def validate_development_plans(batch: NormalizedBatch) -> QualityReport:
    errors = []
    if not batch.records:
        errors.append("WFS development-plan snapshot is empty")
    if batch.rejected_rows or batch.input_rows != len(batch.records):
        errors.append("WFS development-plan snapshot contains rejected or missing rows")
    for record in batch.records:
        if not isinstance(record, DevelopmentPlan):
            errors.append("WFS snapshot contains a non-development-plan record")
            continue
        try:
            geometry = shape(record.geometry)
        except Exception:
            errors.append(f"Invalid development-plan polygon: {record.plan_id}")
            continue
        if (
            geometry.is_empty
            or not geometry.is_valid
            or geometry.geom_type
            not in {
                "Polygon",
                "MultiPolygon",
            }
        ):
            errors.append(f"Invalid development-plan polygon: {record.plan_id}")
    return QualityReport(
        valid=not errors,
        input_rows=batch.input_rows,
        output_rows=len(batch.records),
        rejected_rows=batch.rejected_rows,
        warnings=batch.warnings,
        errors=tuple(errors),
    )


def check_development_plan_parent(records, parent_records) -> dict:
    from pyproj import Transformer
    from shapely.geometry import shape
    from shapely.ops import transform, unary_union

    plans = [record for record in records if isinstance(record, DevelopmentPlan)]
    areas = [
        record
        for record in parent_records
        if isinstance(record, Area) and record.hierarchy == "planungsraum"
    ]
    transformer = Transformer.from_crs("EPSG:4326", "EPSG:25833", always_xy=True)
    parent_union = unary_union(
        [transform(transformer.transform, shape(area.geometry)) for area in areas]
    )
    intersecting = (
        sum(
            shape(record.geometry).is_valid
            and transform(transformer.transform, shape(record.geometry))
            .intersection(parent_union)
            .area
            > 0
            for record in plans
        )
        if areas
        else 0
    )
    outside = len(plans) - intersecting
    coverage = {
        "parent_area_count": len(areas),
        "plans_intersecting_lor": intersecting,
        "plans_outside_lor": outside,
    }
    if outside:
        coverage["warnings"] = [
            f"{outside} development plans do not intersect a Berlin Planungsraum polygon"
        ]
    return coverage
