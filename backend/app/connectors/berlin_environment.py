"""Berlin Umweltgerechtigkeit WFS layers normalized as area classes."""

from __future__ import annotations

import hashlib
import json
import math
import re
import statistics
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pyproj import Transformer
from shapely.geometry import mapping, shape
from shapely.ops import transform

from app.connectors.base import FileConnector
from app.connectors.berlin_wfs import BerlinWFSConnector, parse_page
from app.domain import (
    Area,
    AreaIndicatorClass,
    ConfigError,
    DatasetConfig,
    FetchRequest,
    IncompatibleSnapshotError,
    NormalizedBatch,
    QualityReport,
    RawBundle,
    SchemaError,
)

EXPECTED_INDICATORS = (
    "noise_burden",
    "air_pollution_burden",
    "green_space_provision",
    "thermal_burden",
    "social_disadvantage",
    "multiple_burden_environment",
    "multiple_burden_environment_social",
    "population_density_class",
    "simple_residential_location_hint",
)


def _layer_options(config: DatasetConfig, layer: dict) -> dict:
    options = config.parser_options
    return {
        "typename": layer["typename"],
        "page_size": options["page_size"],
        "max_pages": options["max_pages"],
        "max_total_bytes": options["max_total_bytes"],
        "expected_crs": options["expected_crs"],
        "source_bbox": options["source_bbox"],
        "allowed_bbox": options["allowed_bbox"],
        "expected_properties": layer["expected_properties"],
        "id_field": "plr_id",
        "id_pattern": r"^\d{8}$",
        "expected_count": options["expected_count"],
        "record_type": "area",
        "field_map": {"area_code": "plr_id", "name": "plr_name"},
        "attribute_fields": [],
        "hierarchy": options["hierarchy"],
    }


def _layer_config(config: DatasetConfig, layer: dict) -> DatasetConfig:
    parts = urlsplit(config.resource_url)
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.upper() != "TYPENAMES"
    ]
    query.append(("TYPENAMES", layer["typename"]))
    return replace(
        config,
        resource_url=urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
        ),
        parser_options=_layer_options(config, layer),
    )


def _source_layer(resource_url: str) -> str:
    typenames = [
        value
        for key, value in parse_qsl(urlsplit(resource_url).query, keep_blank_values=True)
        if key.upper() == "TYPENAMES"
    ]
    if len(typenames) != 1:
        raise SchemaError("Berlin environment asset must identify one TYPENAMES layer")
    return typenames[0]


def _significant(value: float) -> float:
    return float(f"{value:.3g}")


class BerlinEnvironmentConnector(FileConnector):
    adapter_version = "1.0.0"

    async def fetch(self, request: FetchRequest) -> RawBundle:
        config = request.config
        if not config.enabled or config.access_validation == "blocked":
            raise ConfigError("Dataset is disabled or blocked")
        layers = config.parser_options.get("layers")
        if not isinstance(layers, list) or not layers:
            raise SchemaError("Berlin environment requires configured layers")
        connector = BerlinWFSConnector(self.fetcher)
        assets = []
        fingerprints = []
        for layer in layers:
            layer_config = _layer_config(config, layer)
            bundle = await connector.fetch(
                FetchRequest(
                    config=layer_config,
                    staging_dir=str(Path(request.staging_dir) / layer["typename"].split(":")[-1]),
                    snapshot_id=request.snapshot_id,
                )
            )
            assets.extend(bundle.assets)
            fingerprints.append(bundle.request_fingerprint)
        fingerprint = hashlib.sha256(
            json.dumps(fingerprints, separators=(",", ":")).encode()
        ).hexdigest()
        return RawBundle(
            assets=tuple(assets),
            retrieved_at=datetime.now(timezone.utc),
            snapshot_id=request.snapshot_id,
            request_fingerprint=fingerprint,
        )

    def normalize(self, raw: RawBundle, config: DatasetConfig) -> NormalizedBatch:
        options = config.parser_options
        layers = options.get("layers")
        if not isinstance(layers, list) or not layers:
            raise SchemaError("Berlin environment requires configured layers")
        if len(raw.assets) == 1 and not any(
            key.upper() == "TYPENAMES"
            for key, _ in parse_qsl(
                urlsplit(raw.assets[0].resource_url).query, keep_blank_values=True
            )
        ):
            raise SchemaError("Local --file imports are not supported for berlin_environment")
        layer_by_name = {layer["typename"]: layer for layer in layers}
        assets_by_layer = {typename: [] for typename in layer_by_name}
        for asset in raw.assets:
            typename = _source_layer(asset.resource_url)
            if typename not in layer_by_name:
                raise SchemaError(f"Unknown Berlin environment asset layer: {typename}")
            assets_by_layer[typename].append(asset)
        missing_layers = [
            typename.split(":")[-1] for typename, assets in assets_by_layer.items() if not assets
        ]
        if missing_layers:
            raise SchemaError(f"Berlin environment is missing layer assets: {missing_layers}")

        source_bounds = options["source_bbox"]
        allowed_bounds = options["allowed_bbox"]
        transformer = Transformer.from_crs(options["expected_crs"], "EPSG:4326", always_xy=True)
        layer_features = {}
        layer_ids = {}
        layer_names = {}
        source_geometries = {}
        extra_properties = {}
        input_rows = 0
        expected_count = options["expected_count"]

        for layer in layers:
            typename = layer["typename"]
            name = typename.split(":")[-1]
            if len(assets_by_layer[typename]) > options["max_pages"]:
                raise SchemaError(f"{name} exceeded the configured page cap")
            parse_options = _layer_options(config, layer)
            pages = []
            matched = None
            layer_assets = assets_by_layer[typename]
            if sum(asset.size_bytes for asset in layer_assets) > options["max_total_bytes"]:
                raise SchemaError(f"{name} exceeded max_total_bytes")
            for asset in layer_assets:
                try:
                    collection = parse_page(Path(asset.path).read_bytes(), parse_options)
                except SchemaError:
                    raise
                except Exception as exc:
                    raise SchemaError(f"Unable to read {name} WFS asset: {exc}") from exc
                if matched is None:
                    matched = collection["numberMatched"]
                elif collection["numberMatched"] != matched:
                    raise SchemaError(f"{name} numberMatched changed between raw assets")
                if len(collection["features"]) > parse_options["page_size"]:
                    raise SchemaError(f"{name} WFS page exceeded configured page_size")
                pages.extend(collection["features"])
            if matched != expected_count or len(pages) != expected_count:
                raise SchemaError(
                    f"{name} count mismatch: numberMatched {matched}, received {len(pages)}; "
                    f"expected {expected_count}"
                )
            ids = {}
            names = {}
            geometries = {}
            extras = set()
            required = set(layer["expected_properties"])
            for feature in pages:
                if not isinstance(feature, dict) or not isinstance(feature.get("properties"), dict):
                    raise SchemaError(f"{name} feature properties must be an object")
                properties = feature["properties"]
                absent = sorted(required - properties.keys())
                if absent:
                    raise SchemaError(f"{name} feature is missing expected properties: {absent}")
                extras.update(properties.keys() - required)
                area_code = properties["plr_id"]
                if not isinstance(area_code, str) or not re.fullmatch(r"\d{8}", area_code):
                    raise SchemaError(f"{name} plr_id must be an 8-digit string: {area_code!r}")
                if area_code in ids:
                    raise SchemaError(f"Duplicate {name} WFS ID: {area_code}")
                area_name = properties["plr_name"]
                if not isinstance(area_name, str) or not area_name.strip():
                    raise SchemaError(f"{name} has an invalid plr_name for {area_code}")
                geometry_data = feature.get("geometry")
                if not isinstance(geometry_data, dict):
                    raise SchemaError(f"{name} has invalid or missing geometry: {area_code}")
                try:
                    source_geometry = shape(geometry_data)
                except Exception as exc:
                    raise SchemaError(f"{name} has invalid geometry: {area_code}") from exc
                if source_geometry.is_empty or source_geometry.geom_type not in {
                    "Polygon",
                    "MultiPolygon",
                }:
                    raise SchemaError(f"{name} has unsupported geometry type: {area_code}")
                minx, miny, maxx, maxy = source_geometry.bounds
                if not (
                    source_bounds[0] <= minx <= maxx <= source_bounds[2]
                    and source_bounds[1] <= miny <= maxy <= source_bounds[3]
                ):
                    raise SchemaError(f"{name} geometry outside source_bbox: {area_code}")
                geometry = transform(transformer.transform, source_geometry)
                if geometry.is_empty or not geometry.is_valid:
                    raise SchemaError(f"{name} invalid polygon after reprojection: {area_code}")
                west, south, east, north = geometry.bounds
                if not (
                    allowed_bounds[0] <= west <= east <= allowed_bounds[2]
                    and allowed_bounds[1] <= south <= north <= allowed_bounds[3]
                ):
                    raise SchemaError(f"{name} geometry outside allowed_bbox: {area_code}")
                ids[area_code] = properties
                names[area_code] = area_name
                geometries[area_code] = source_geometry
            layer_features[name] = ids
            layer_ids[name] = set(ids)
            layer_names[name] = names
            source_geometries[name] = geometries
            input_rows += len(pages)
            if extras:
                extra_properties[name] = sorted(extras)

        reference_layer = layers[0]["typename"].split(":")[-1]
        reference_ids = layer_ids[reference_layer]
        for layer in layers[1:]:
            name = layer["typename"].split(":")[-1]
            if layer_ids[name] != reference_ids:
                raise SchemaError(f"Berlin environment PLR set mismatch in layer {name}")
            for area_code in reference_ids:
                if layer_names[name][area_code] != layer_names[reference_layer][area_code]:
                    raise SchemaError(
                        f"Berlin environment plr_name mismatch in {name} for {area_code}"
                    )

        indicators = {}
        for layer in layers:
            source_layer = layer["typename"].split(":")[-1]
            for indicator in layer.get("indicators", []):
                indicators[indicator["indicator_code"]] = (source_layer, indicator)

        max_geometry_ratio = 0.0
        for layer in layers[1:]:
            name = layer["typename"].split(":")[-1]
            for area_code in reference_ids:
                baseline = source_geometries[reference_layer][area_code]
                geometry = source_geometries[name][area_code]
                symmetric_difference = geometry.symmetric_difference(baseline).area
                ratio = symmetric_difference / baseline.area if baseline.area > 0 else 1.0
                max_geometry_ratio = max(max_geometry_ratio, ratio)
                if ratio > 1e-6:
                    raise SchemaError(
                        f"{name} geometry differs from {reference_layer} for {area_code}: "
                        f"{ratio:.6g}"
                    )

        records = []
        for area_code in sorted(reference_ids):
            area_properties = layer_features[reference_layer][area_code]
            area_geometry = transform(
                transformer.transform, source_geometries[reference_layer][area_code]
            )
            records.append(
                Area(
                    source_id=config.dataset_id,
                    snapshot_id=raw.snapshot_id,
                    source_record_id=area_code,
                    record_id=f"planungsraum:{area_code}",
                    geometry=mapping(area_geometry),
                    area_code=area_code,
                    name=area_properties["plr_name"],
                    hierarchy=options["hierarchy"],
                    boundary_edition=config.boundary_edition,
                    attributes={"source_layer": reference_layer},
                )
            )

        distributions = {}
        for indicator_code, (source_layer, indicator) in indicators.items():
            field_name = indicator["source_field"]
            classes = {item["label"]: item for item in indicator["classes"]}
            missing_markers = indicator.get("missing_markers", {})
            counts = Counter()
            missing_count = 0
            for area_code in sorted(reference_ids):
                properties = layer_features[source_layer][area_code]
                if field_name not in properties:
                    raise SchemaError(f"{source_layer} missing indicator field {field_name}")
                source_value = properties[field_name]
                marker = "null" if source_value is None else source_value
                class_value = classes.get(source_value) if isinstance(source_value, str) else None
                missing = missing_markers.get(marker) if isinstance(marker, str) else None
                if class_value is None and missing is None:
                    raise SchemaError(
                        f"Unknown class label {source_layer}.{field_name}={source_value!r}"
                    )
                if class_value is not None:
                    class_code = class_value["class_code"]
                    class_label = class_value["label"]
                    class_rank = class_value["rank"]
                    status = indicator["value_status"]
                    flags = ()
                    counts[class_label] += 1
                else:
                    class_code = class_label = class_rank = None
                    status = missing.get("value_status", "missing")
                    flags = (missing["flag"],)
                    missing_count += 1
                extra_field = (
                    f"#{field_name}"
                    if source_layer == "z_gesamt_umwelt2023"
                    and field_name in {"einwohner", "wol_l_l"}
                    else ""
                )
                records.append(
                    AreaIndicatorClass(
                        source_id=config.dataset_id,
                        snapshot_id=raw.snapshot_id,
                        source_record_id=f"{source_layer}.{area_code}{extra_field}",
                        record_id=f"{indicator_code}:{area_code}",
                        area_code=area_code,
                        hierarchy=options["hierarchy"],
                        boundary_edition=config.boundary_edition,
                        indicator_code=indicator_code,
                        class_code=class_code,
                        class_label=class_label,
                        class_rank=class_rank,
                        class_count=len(indicator["classes"]),
                        rank_direction=indicator["rank_direction"],
                        reference_period=indicator["reference_period"],
                        value_status=status,
                        attributes={
                            "source_layer": source_layer,
                            "source_field": field_name,
                            "plr_name": layer_names[source_layer][area_code],
                        },
                        quality_flags=flags,
                    )
                )
            pieces = [f"{item['label']}={counts[item['label']]}" for item in indicator["classes"]]
            pieces.append(f"missing={missing_count}")
            distributions[indicator_code] = f"{indicator_code} classes: " + ", ".join(pieces)

        self._validate_components(layers, layer_features, indicators)

        warnings = list(options.get("limitations", ()))
        warnings.extend(distributions.values())
        warnings.append(
            "Maximum cross-layer geometry symmetric-difference ratio: "
            f"{_significant(max_geometry_ratio)}"
        )
        for layer_name, properties in sorted(extra_properties.items()):
            warnings.append(f"{layer_name} returned extra properties: {', '.join(properties)}")
        return NormalizedBatch(
            records=tuple(records), input_rows=input_rows, warnings=tuple(warnings)
        )

    @staticmethod
    def _validate_components(layers, layer_features, indicators):
        for layer in layers:
            validation = layer.get("validation")
            if not validation:
                continue
            name = layer["typename"].split(":")[-1]
            components = validation["components"]
            worst_labels = validation["worst_labels"]
            expected_count_labels = validation["count_labels"]
            for area_code, properties in layer_features[name].items():
                worst_count = 0
                for field_name, indicator_code in components.items():
                    source_layer, indicator = indicators[indicator_code]
                    source_value = layer_features[source_layer][area_code][
                        indicator["source_field"]
                    ]
                    component_value = properties[field_name]
                    expected = (
                        None
                        if indicator_code == "social_disadvantage" and source_value == "-"
                        else source_value
                    )
                    if component_value != expected:
                        raise SchemaError(
                            f"{name} component mismatch {field_name} for {area_code}: "
                            f"{component_value!r} != {expected!r}"
                        )
                    if component_value == worst_labels[field_name]:
                        worst_count += 1
                category = properties[validation["count_field"]]
                match_layer = validation.get("matches_layer")
                if match_layer:
                    match_name = match_layer.split(":")[-1]
                    match_value = layer_features[match_name][area_code][
                        validation.get("matches_field", "kategorie")
                    ]
                    if category != match_value:
                        raise SchemaError(
                            f"{name}.kategorie differs from {match_name}.kategorie for {area_code}"
                        )
                expected_category = expected_count_labels[str(worst_count)]
                if category != expected_category:
                    raise SchemaError(
                        f"{name} combined count mismatch for {area_code}: "
                        f"{category!r} != {expected_category!r}"
                    )

    def validate(self, batch: NormalizedBatch) -> QualityReport:
        errors = []
        if not batch.records:
            errors.append("Berlin environment snapshot is empty")
        if batch.rejected_rows:
            errors.append("Berlin environment snapshot contains rejected rows")
        areas = [record for record in batch.records if isinstance(record, Area)]
        indicators = [record for record in batch.records if isinstance(record, AreaIndicatorClass)]
        expected_count = batch.input_rows // 8
        if batch.input_rows % 8:
            errors.append("Berlin environment input row count is not divisible by eight layers")
        if expected_count == 0:
            errors.append("Berlin environment snapshot has no input rows")
        if len(areas) != expected_count:
            errors.append(f"Expected {expected_count} Area records, found {len(areas)}")
        counts = Counter(record.indicator_code for record in indicators)
        for indicator_code in EXPECTED_INDICATORS:
            if counts[indicator_code] != expected_count:
                errors.append(
                    f"Expected {expected_count} {indicator_code} records, "
                    f"found {counts[indicator_code]}"
                )
        unexpected_indicators = set(counts) - set(EXPECTED_INDICATORS)
        if unexpected_indicators:
            errors.append(
                "Unexpected Berlin environment indicators: "
                + ", ".join(sorted(unexpected_indicators))
            )
        seen = set()
        editions = set()
        for record in batch.records:
            editions.add(record.boundary_edition)
            if isinstance(record, AreaIndicatorClass):
                key = (record.indicator_code, record.area_code)
                if key in seen:
                    errors.append(f"Duplicate Berlin environment indicator key: {key}")
                seen.add(key)
            elif isinstance(record, Area):
                try:
                    geometry = shape(record.geometry)
                    valid = (
                        not geometry.is_empty
                        and geometry.is_valid
                        and geometry.geom_type in {"Polygon", "MultiPolygon"}
                    )
                except Exception:
                    valid = False
                if not valid:
                    errors.append(f"Invalid Berlin environment Area geometry: {record.area_code}")
            else:
                errors.append("Berlin environment batch contains an unsupported record type")
        if len(editions) > 1:
            errors.append("Berlin environment snapshot mixes boundary editions")
        return QualityReport(
            valid=not errors,
            input_rows=batch.input_rows,
            output_rows=len(batch.records),
            rejected_rows=batch.rejected_rows,
            warnings=batch.warnings,
            errors=tuple(errors),
        )

    def check_parent(self, config: DatasetConfig, records: tuple, parent_records: tuple) -> dict:
        options = config.parser_options
        invalid_records = sum(
            not isinstance(record, (Area, AreaIndicatorClass)) for record in records
        ) + sum(not isinstance(record, Area) for record in parent_records)
        edition_mismatches = sum(
            isinstance(record, Area) and record.boundary_edition != config.boundary_edition
            for record in (*records, *parent_records)
        )
        child_keys = {
            (record.hierarchy, record.area_code) for record in records if isinstance(record, Area)
        }
        indicator_codes = {
            record.indicator_code for record in records if isinstance(record, AreaIndicatorClass)
        }
        indicator_keys_by_code = {
            code: {
                (record.hierarchy, record.area_code)
                for record in records
                if isinstance(record, AreaIndicatorClass) and record.indicator_code == code
            }
            for code in indicator_codes
        }
        parent_keys = {
            (record.hierarchy, record.area_code)
            for record in parent_records
            if isinstance(record, Area)
        }
        missing = parent_keys - child_keys
        unmatched = child_keys - parent_keys
        missing_indicators = set()
        unmatched_indicators = set()
        for code in EXPECTED_INDICATORS:
            keys = indicator_keys_by_code.get(code, set())
            missing_indicators.update(parent_keys - keys)
            unmatched_indicators.update(keys - parent_keys)
        unexpected_indicators = indicator_codes - set(EXPECTED_INDICATORS)
        if (
            invalid_records
            or edition_mismatches
            or missing
            or unmatched
            or missing_indicators
            or unmatched_indicators
            or unexpected_indicators
        ):
            missing_codes = sorted(
                {code for _, code in missing} | {code for _, code in missing_indicators}
            )[:5]
            unmatched_codes = sorted(
                {code for _, code in unmatched} | {code for _, code in unmatched_indicators}
            )[:5]
            raise IncompatibleSnapshotError(
                "Berlin environment/parent coverage differs: "
                f"{len(missing | missing_indicators)} missing from dataset, "
                f"{len(unmatched | unmatched_indicators)} not in parent; "
                f"examples missing={missing_codes}, not_in_parent={unmatched_codes}; "
                f"non-area records={invalid_records}, "
                f"boundary-edition mismatches={edition_mismatches}, "
                f"unexpected indicators={sorted(unexpected_indicators)}"
            )
        threshold = options.get("geometry_difference_threshold", 0.01)
        if (
            isinstance(threshold, bool)
            or not isinstance(threshold, (int, float))
            or not math.isfinite(threshold)
            or threshold < 0
        ):
            raise SchemaError(
                "Berlin environment geometry_difference_threshold must be nonnegative"
            )
        transformer = Transformer.from_crs("EPSG:4326", "EPSG:25833", always_xy=True)
        parent_by_key = {
            (record.hierarchy, record.area_code): record
            for record in parent_records
            if isinstance(record, Area)
        }
        ratios = []
        for record in records:
            if not isinstance(record, Area):
                continue
            key = (record.hierarchy, record.area_code)
            data_geometry = transform(transformer.transform, shape(record.geometry))
            parent_geometry = transform(transformer.transform, shape(parent_by_key[key].geometry))
            parent_area = parent_geometry.area
            difference = data_geometry.symmetric_difference(parent_geometry).area
            ratio = (
                difference / parent_area if parent_area > 0 else (0.0 if difference == 0 else 1.0)
            )
            ratios.append((record.area_code, ratio))
        ordered = sorted(ratios, key=lambda item: item[1], reverse=True)
        ratio_values = [ratio for _, ratio in ratios]
        return {
            "parent_dataset": options.get("parent_dataset", "berlin_lor"),
            "parent_code_match": "exact",
            "parent_area_count": len(parent_keys),
            "join_method": "exact planungsraum code",
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
                "Environmental WFS geometry is compared with the pinned berlin_lor boundary and not reconciled."
            ],
        }
