"""Official Berlin green spaces and playgrounds from the gdi.berlin ``gruenanlagen`` WFS.

One snapshot combines the reviewed layers. Paging, size limits and OGC exception
handling are inherited from :class:`BerlinWFSConnector`; this module only adds the
multi-layer fetch and the ``GreenSpace`` mapping.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import numpy as np
import shapely
from pyproj import Transformer
from shapely.geometry import mapping, shape
from shapely.strtree import STRtree

from app.connectors.berlin_wfs import BerlinWFSConnector, _bbox, parse_page
from app.domain import (
    Area,
    DatasetConfig,
    FetchRequest,
    GreenSpace,
    IncompatibleSnapshotError,
    NormalizedBatch,
    QualityReport,
    RawBundle,
    SchemaError,
)


def transform(func, geometry):
    """Reproject with a pyproj ``transform`` callable (vectorised shapely 2 API)."""
    return shapely.transform(geometry, lambda xy: np.column_stack(func(xy[:, 0], xy[:, 1])))


def _geojson(geometry) -> dict:
    return json.loads(json.dumps(mapping(geometry)))


LAYER_REQUIRED = ("feature_type", "typename", "resource_url", "expected_properties")
OPTION_REQUIRED = (
    "layers",
    "page_size",
    "max_pages",
    "expected_crs",
    "source_bbox",
    "id_field",
    "category_field",
    "category_mapping",
    "category_mapping_version",
    "name_field",
    "area_code_field",
    "hierarchy",
    "source_area_field",
    "attribute_fields",
)


def _typename(url: str) -> str | None:
    values = [v for k, v in parse_qsl(urlsplit(url).query) if k.upper() == "TYPENAMES"]
    return values[0] if len(values) == 1 else None


def _options(config: DatasetConfig) -> dict:
    options = config.parser_options
    for name in OPTION_REQUIRED:
        if name not in options:
            raise SchemaError(f"Green-space parser requires explicit {name}")
    layers = options["layers"]
    if not isinstance(layers, list) or not layers:
        raise SchemaError("Green-space parser requires a nonempty layers list")
    seen = set()
    for layer in layers:
        if not isinstance(layer, dict) or any(name not in layer for name in LAYER_REQUIRED):
            raise SchemaError(f"Green-space layers require {', '.join(LAYER_REQUIRED)}")
        if layer["feature_type"] not in set(options["category_mapping"].values()):
            raise SchemaError(f"Layer feature_type is not mapped: {layer['feature_type']}")
        if layer["feature_type"] in seen or layer["typename"] in seen:
            raise SchemaError("Green-space layers must have unique feature types and typenames")
        seen.update((layer["feature_type"], layer["typename"]))
        if _typename(layer["resource_url"]) != layer["typename"]:
            raise SchemaError("Layer typename must match its resource_url TYPENAMES")
        count_range = layer.get("expected_count_range")
        if (
            not isinstance(count_range, list)
            or len(count_range) != 2
            or any(type(value) is not int or value < 0 for value in count_range)
            or count_range[0] > count_range[1]
        ):
            raise SchemaError("Layer expected_count_range must contain ordered integers")
    for name in ("adjacency_tolerance_m",):
        value = options.get(name, 1.0)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise SchemaError(f"{name} must be a nonnegative number")
    return options


def _fetch_options(options: dict, layer: dict) -> dict:
    """Options accepted by the inherited single-layer WFS pager (it only pages and checks
    CRS/counts; the area-specific keys are unused during fetch)."""
    return {
        "typename": layer["typename"],
        "page_size": options["page_size"],
        "max_pages": options["max_pages"],
        "max_total_bytes": options.get("max_total_bytes", 50_000_000),
        "expected_crs": options["expected_crs"],
        "source_bbox": options["source_bbox"],
        "allowed_bbox": options.get("allowed_bbox", [13.0, 52.3, 13.9, 52.8]),
        "expected_properties": layer["expected_properties"],
        "id_field": options["id_field"],
        "expected_count_range": layer["expected_count_range"],
        "record_type": "area",
        "field_map": {"area_code": options["id_field"], "name": options["id_field"]},
        "attribute_fields": [],
        "hierarchy": options["hierarchy"],
    }


def _blank(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _area_value(properties: dict, field: str | None, source_id: str, flags: list) -> float | None:
    if not field:
        return None
    value = properties[field]
    if _blank(value):
        flags.append(f"{field}_missing")
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise SchemaError(f"WFS {field} must be a number or null: {source_id}")
    if value < 0:
        raise SchemaError(f"WFS {field} must not be negative: {source_id}")
    if value == 0:
        # A zero cadastral or play area for a mapped polygon means "not recorded".
        flags.append(f"{field}_zero_treated_as_missing")
        return None
    return float(value)


class BerlinGreenSpacesConnector(BerlinWFSConnector):
    """Reviewed multi-layer WFS import producing ``GreenSpace`` polygon records."""

    adapter_version = "1.0.0"

    async def fetch(self, request: FetchRequest) -> RawBundle:
        config = request.config
        options = _options(config)
        assets, fingerprints = [], []
        retrieved_at = None
        for layer in options["layers"]:
            stage = Path(request.staging_dir) / layer["feature_type"]
            stage.mkdir(parents=True, exist_ok=True)
            bundle = await super().fetch(
                replace(
                    request,
                    config=replace(
                        config,
                        resource_url=layer["resource_url"],
                        parser_options=_fetch_options(options, layer),
                    ),
                    staging_dir=str(stage),
                )
            )
            for asset in bundle.assets:
                target = (
                    Path(request.staging_dir) / f"{layer['feature_type']}-{Path(asset.path).name}"
                )
                shutil.move(asset.path, target)
                assets.append(replace(asset, path=str(target)))
            fingerprints.append(bundle.request_fingerprint)
            retrieved_at = retrieved_at or bundle.retrieved_at
        return RawBundle(
            assets=tuple(assets),
            retrieved_at=retrieved_at,
            snapshot_id=request.snapshot_id,
            request_fingerprint=hashlib.sha256(
                json.dumps(fingerprints, sort_keys=True).encode()
            ).hexdigest(),
        )

    def normalize(self, raw: RawBundle, config: DatasetConfig) -> NormalizedBatch:
        options = _options(config)
        layers = {layer["typename"]: layer for layer in options["layers"]}
        pages: dict[str, list[dict]] = {name: [] for name in layers}
        for asset in raw.assets:
            typename = _typename(asset.resource_url)
            if typename not in layers:
                raise SchemaError(
                    "Green-space assets must be WFS pages of a reviewed layer; "
                    "single-file imports are not supported"
                )
            try:
                data = Path(asset.path).read_bytes()
            except OSError as exc:
                raise SchemaError(f"Unable to read WFS GeoJSON asset: {exc}") from exc
            pages[typename].append(parse_page(data, options))

        to_wgs84 = Transformer.from_crs(options["expected_crs"], "EPSG:4326", always_xy=True)
        source_bounds = _bbox(options, "source_bbox")
        allowed_bounds = _bbox(options, "allowed_bbox", [13.0, 52.3, 13.9, 52.8])
        id_pattern = options.get("id_pattern")
        code_pattern = options.get("area_code_pattern")
        mapping_table = options["category_mapping"]
        records, metric_geometries = [], []
        seen_ids: set[str] = set()
        warnings = list(options.get("limitations", ()))
        flag_counts: dict[str, int] = {}
        input_rows = 0
        for typename, layer in layers.items():
            collections = pages[typename]
            if not collections:
                raise SchemaError(f"WFS layer {typename} has no downloaded pages")
            matched = {collection["numberMatched"] for collection in collections}
            if len(matched) != 1:
                raise SchemaError(f"WFS numberMatched changed between pages of {typename}")
            features = [feature for page in collections for feature in page["features"]]
            (expected,) = matched
            if len(features) != expected:
                raise SchemaError(
                    f"WFS count mismatch for {typename}: numberMatched {expected}, "
                    f"received {len(features)}"
                )
            low, high = layer["expected_count_range"]
            if not low <= expected <= high:
                raise SchemaError(
                    f"WFS {typename} expected feature count in {[low, high]}, received {expected}"
                )
            input_rows += len(features)
            required = set(layer["expected_properties"])
            extra: set[str] = set()
            for feature in features:
                if not isinstance(feature, dict) or not isinstance(feature.get("properties"), dict):
                    raise SchemaError("WFS feature is missing properties")
                properties = feature["properties"]
                missing = sorted(required - properties.keys())
                if missing:
                    raise SchemaError(f"WFS feature is missing expected properties: {missing}")
                extra.update(properties.keys() - required)
                source_id = properties[options["id_field"]]
                if not isinstance(source_id, str) or not source_id.strip():
                    raise SchemaError("WFS ID must be a nonempty string")
                if id_pattern and not re.fullmatch(id_pattern, source_id):
                    raise SchemaError(f"WFS ID does not match id_pattern: {source_id!r}")
                if source_id in seen_ids:
                    raise SchemaError(f"Duplicate WFS ID: {source_id}")
                seen_ids.add(source_id)
                flags: list[str] = []

                category = properties[options["category_field"]]
                category = category.strip() if isinstance(category, str) else None
                if not category:
                    raise SchemaError(f"WFS object type is missing: {source_id}")
                mapped = mapping_table.get(category)
                if mapped is None:
                    flags.append("object_type_unmapped")
                elif mapped != layer["feature_type"]:
                    flags.append("object_type_conflicts_with_layer")

                geometry_data = feature.get("geometry")
                if not isinstance(geometry_data, dict):
                    raise SchemaError(f"Invalid or missing WFS geometry: {source_id}")
                try:
                    source_geometry = shape(geometry_data)
                except Exception as exc:
                    raise SchemaError(f"Invalid WFS geometry: {source_id}") from exc
                if source_geometry.is_empty or source_geometry.geom_type not in {
                    "Polygon",
                    "MultiPolygon",
                }:
                    raise SchemaError(f"Unsupported WFS geometry type: {source_id}")
                if not source_geometry.is_valid:
                    raise SchemaError(f"Invalid WFS polygon: {source_id}")
                minx, miny, maxx, maxy = source_geometry.bounds
                if not (
                    source_bounds[0] <= minx <= maxx <= source_bounds[2]
                    and source_bounds[1] <= miny <= maxy <= source_bounds[3]
                ):
                    raise SchemaError(f"WFS geometry outside source_bbox: {source_id}")
                geometry = transform(to_wgs84.transform, source_geometry)
                point = transform(to_wgs84.transform, source_geometry.representative_point())
                if geometry.is_empty or not geometry.is_valid:
                    raise SchemaError(f"Invalid WFS polygon after reprojection: {source_id}")
                west, south, east, north = geometry.bounds
                if not (
                    allowed_bounds[0] <= west <= east <= allowed_bounds[2]
                    and allowed_bounds[1] <= south <= north <= allowed_bounds[3]
                ):
                    raise SchemaError(f"WFS geometry outside allowed_bbox: {source_id}")

                name = properties[options["name_field"]]
                if not _blank(name) and not isinstance(name, str):
                    raise SchemaError(f"WFS name must be a string or null: {source_id}")
                if _blank(name):
                    flags.append("name_missing")
                area_code = properties[options["area_code_field"]]
                if _blank(area_code):
                    area_code = None
                    flags.append("area_code_missing")
                elif not isinstance(area_code, str) or (
                    code_pattern and not re.fullmatch(code_pattern, area_code.strip())
                ):
                    raise SchemaError(f"WFS area code is invalid: {source_id}")
                else:
                    area_code = area_code.strip()
                attributes = {}
                for key in options["attribute_fields"]:
                    value = properties.get(key)
                    if key not in properties or _blank(value):
                        flags.append(f"attribute_missing:{key}")
                    elif isinstance(value, str):
                        attributes[key] = value.strip()
                    else:
                        raise SchemaError(f"WFS attribute {key} must be a string or null")
                source_area = _area_value(
                    properties, options["source_area_field"], source_id, flags
                )
                net_play_area = _area_value(
                    properties, layer.get("net_play_area_field"), source_id, flags
                )
                for flag in flags:
                    flag_counts[f"{layer['feature_type']}:{flag}"] = (
                        flag_counts.get(f"{layer['feature_type']}:{flag}", 0) + 1
                    )
                metric_geometries.append(source_geometry)
                records.append(
                    dict(
                        source_id=config.dataset_id,
                        snapshot_id=raw.snapshot_id,
                        source_record_id=source_id,
                        record_id=f"{layer['feature_type']}:{source_id}",
                        geometry=_geojson(geometry),
                        display_point=_geojson(point),
                        display_point_method=options.get(
                            "display_point_method", "representative_point_epsg25833"
                        ),
                        feature_type=layer["feature_type"],
                        original_category=category,
                        category_mapping_version=options["category_mapping_version"],
                        geometry_area_m2=round(source_geometry.area, 1),
                        name=None if _blank(name) else name.strip(),
                        area_code=area_code,
                        hierarchy=options["hierarchy"] if area_code else None,
                        boundary_edition=config.boundary_edition if area_code else None,
                        source_area_m2=source_area,
                        net_play_area_m2=net_play_area,
                        attributes=attributes,
                        quality_flags=tuple(flags),
                    )
                )
            if extra:
                warnings.append(
                    f"WFS layer {typename} returned extra properties: {', '.join(sorted(extra))}"
                )

        adjacency, overlaps = self._adjacency(records, metric_geometries, options)
        counts: dict[str, int] = {}
        for values in records:
            counts[values["feature_type"]] = counts.get(values["feature_type"], 0) + 1
        warnings.append(
            "Feature counts: "
            + ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))
        )
        if flag_counts:
            warnings.append(
                "Quality flags: "
                + ", ".join(f"{key}={value}" for key, value in sorted(flag_counts.items()))
            )
        tolerance = options.get("adjacency_tolerance_m", 1.0)
        warnings.append(
            f"Cross-type adjacency (EPSG:25833, tolerance {tolerance} m): "
            f"{sum(bool(ids) for ids in adjacency)} records linked; "
            f"{overlaps} green-space/playground pairs overlap by more than 1 m²."
        )
        return NormalizedBatch(
            records=tuple(
                GreenSpace(**values, adjacent_record_ids=ids)
                for values, ids in zip(records, adjacency)
            ),
            input_rows=input_rows,
            warnings=tuple(warnings),
        )

    @staticmethod
    def _adjacency(records: list[dict], geometries: list, options: dict):
        tolerance = options.get("adjacency_tolerance_m", 1.0)
        adjacency: list[set[str]] = [set() for _ in records]
        overlaps = 0
        if not geometries:
            return [()] * len(records), overlaps
        tree = STRtree(geometries)
        for index, geometry in enumerate(geometries):
            for other in tree.query(geometry, predicate="dwithin", distance=tolerance):
                other = int(other)
                if other <= index or (
                    records[other]["feature_type"] == records[index]["feature_type"]
                ):
                    continue
                adjacency[index].add(records[other]["record_id"])
                adjacency[other].add(records[index]["record_id"])
                if geometry.intersection(geometries[other]).area > 1.0:
                    overlaps += 1
        return [tuple(sorted(ids)) for ids in adjacency], overlaps

    def validate(self, batch: NormalizedBatch) -> QualityReport:
        errors = []
        if not batch.records:
            errors.append("Green-space snapshot is empty")
        if batch.rejected_rows or batch.input_rows != len(batch.records):
            errors.append("Green-space snapshot contains rejected or missing rows")
        keys = set()
        types = set()
        for record in batch.records:
            if not isinstance(record, GreenSpace):
                errors.append("Green-space snapshot contains a non-green-space record")
                continue
            if record.source_record_id in keys:
                errors.append(f"Duplicate source ID: {record.source_record_id}")
            keys.add(record.source_record_id)
            types.add(record.feature_type)
            geometry = shape(record.geometry)
            if geometry.is_empty or not geometry.is_valid:
                errors.append(f"Invalid green-space polygon: {record.record_id}")
        if batch.records and types != {"green_space", "playground"}:
            errors.append(f"Green-space snapshot lacks a feature type: found {sorted(types)}")
        return QualityReport(
            valid=not errors,
            input_rows=batch.input_rows,
            output_rows=len(batch.records),
            rejected_rows=batch.rejected_rows,
            warnings=batch.warnings,
            errors=tuple(errors),
        )

    def check_parent(self, config: DatasetConfig, records: tuple, parent_records: tuple) -> dict:
        """Report supplied PLR codes against the pinned LOR snapshot; never reassign them."""
        options = _options(config)
        to_metric = Transformer.from_crs("EPSG:4326", "EPSG:25833", always_xy=True)
        areas = {
            area.area_code: transform(to_metric.transform, shape(area.geometry))
            for area in parent_records
            if isinstance(area, Area)
            and area.hierarchy == options["hierarchy"]
            and area.boundary_edition == config.boundary_edition
        }
        if not areas:
            raise IncompatibleSnapshotError(
                f"LOR snapshot has no {options['hierarchy']} areas for boundary edition "
                f"{config.boundary_edition}"
            )
        codes = sorted(areas)
        tree = STRtree([areas[code] for code in codes])
        missing, unmatched, outside, multiple = [], [], [], []
        unmatched_codes = set()
        for record in records:
            geometry = transform(to_metric.transform, shape(record.geometry))
            point = transform(to_metric.transform, shape(record.display_point))
            if record.area_code is None:
                missing.append(record.record_id)
            elif record.area_code not in areas:
                unmatched.append(record.record_id)
                unmatched_codes.add(record.area_code)
            elif not areas[record.area_code].buffer(1.0).contains(point):
                outside.append(record.record_id)
            hits = [
                codes[int(index)]
                for index in tree.query(geometry, predicate="intersects")
                if areas[codes[int(index)]].intersection(geometry).area > 0.01 * geometry.area
            ]
            if len(hits) > 1:
                multiple.append(record.record_id)
        return {
            "parent_dataset": options.get("parent_dataset", "berlin_lor"),
            "feature_counts": {
                kind: sum(record.feature_type == kind for record in records)
                for kind in ("green_space", "playground")
            },
            "area_code_missing_count": len(missing),
            "area_code_missing_sample": missing[:20],
            "area_code_unmatched_count": len(unmatched),
            "area_code_unmatched_codes": sorted(unmatched_codes)[:50],
            "display_point_outside_supplied_area_count": len(outside),
            "display_point_outside_supplied_area_sample": outside[:20],
            "features_spanning_multiple_areas_count": len(multiple),
            "join_method": (
                "provider plannr matched to the pinned LOR snapshot; display point checked "
                "against the supplied polygon (1 m tolerance) and polygon overlap (>1% of the "
                "feature area) counted in EPSG:25833; codes are reported, never reassigned"
            ),
            "inventory": "official Berlin inventory only; not deduplicated against OSM features",
        }
