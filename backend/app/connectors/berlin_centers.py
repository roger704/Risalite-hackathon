"""Berlin StEP Zentren 2040 retail centers and specialist retail sites."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pyproj import Transformer
from shapely.geometry import mapping, shape
from shapely.ops import transform

from app.connectors.base import FileConnector
from app.connectors.berlin_wfs import BerlinWFSConnector, parse_page
from app.domain import (
    Area,
    ConfigError,
    DatasetConfig,
    FetchRequest,
    IncompatibleSnapshotError,
    NormalizedBatch,
    QualityReport,
    RawBundle,
    RetailCenter,
    SchemaError,
)

_TO_WGS84 = Transformer.from_crs("EPSG:25833", "EPSG:4326", always_xy=True)
_TO_WGS84_FN = _TO_WGS84.transform
_TO_METRIC = Transformer.from_crs("EPSG:4326", "EPSG:25833", always_xy=True)
_TO_METRIC_FN = _TO_METRIC.transform


class BerlinCentersConnector(FileConnector):
    adapter_version = "1.0.0"

    @staticmethod
    def _layer_options(config: DatasetConfig, key: str) -> dict:
        options = config.parser_options
        layer = options["layers"][key]
        return {
            "typename": layer["typename"],
            "page_size": options["page_size"],
            "max_pages": options["max_pages"],
            "max_total_bytes": options["max_total_bytes"],
            "expected_crs": options["expected_crs"],
            "source_bbox": options["source_bbox"],
            "allowed_bbox": options["allowed_bbox"],
            "expected_properties": layer["expected_properties"],
            "id_field": "id",
            "id_pattern": layer["id_pattern"],
            "expected_count_range": layer["expected_count_range"],
            "record_type": "area",
            "field_map": {"area_code": "id", "name": "name"},
            "attribute_fields": [],
            "hierarchy": key,
        }

    async def fetch(self, request: FetchRequest) -> RawBundle:
        config = request.config
        options = config.parser_options
        mapping, mapping_version = self._load_mapping(options)
        generic = BerlinWFSConnector(self.fetcher)
        assets = []
        fingerprints = []
        for key, layer in options["layers"].items():
            layer_config = replace(
                config,
                resource_url=layer["resource_url"],
                parser_options=self._layer_options(config, key),
            )
            staging = Path(request.staging_dir) / key
            bundle = await generic.fetch(
                FetchRequest(
                    config=layer_config,
                    staging_dir=str(staging),
                    snapshot_id=request.snapshot_id,
                )
            )
            for page_number, asset in enumerate(bundle.assets):
                renamed = Path(asset.path).with_name(f"{key}-page-{page_number:04d}.json")
                Path(asset.path).rename(renamed)
                assets.append(replace(asset, path=str(renamed)))
            fingerprints.append((key, bundle.request_fingerprint))
        fingerprint = hashlib.sha256(
            json.dumps(
                {"layers": sorted(fingerprints), "mapping_version": mapping_version},
                sort_keys=True,
            ).encode()
        ).hexdigest()
        return RawBundle(
            assets=tuple(assets),
            retrieved_at=bundle.retrieved_at,
            snapshot_id=request.snapshot_id,
            request_fingerprint=fingerprint,
        )

    @staticmethod
    def _load_mapping(options: dict) -> tuple[dict, str]:
        name = options.get("category_mapping")
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9_]+", name):
            raise ConfigError("Berlin centers requires a reviewed category_mapping")
        path = Path(__file__).resolve().parents[2] / "config" / "categories" / f"{name}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ConfigError("Berlin centers category mapping is unavailable or invalid") from exc
        if data.get("mapping_version") != name or not isinstance(data.get("layers"), dict):
            raise ConfigError("Berlin centers category mapping version or layers are invalid")
        expected_hash = options.get("category_mapping_sha256")
        actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        if expected_hash and expected_hash != actual_hash:
            raise ConfigError("Berlin centers category mapping SHA-256 differs from configuration")
        return data, name

    @staticmethod
    def _source_url(layer_url: str, feature_id: str) -> str:
        parts = urlsplit(layer_url)
        query = parse_qsl(parts.query, keep_blank_values=True)
        query.append(("RESOURCEID", feature_id))
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
        )

    def normalize(self, raw: RawBundle, config: DatasetConfig) -> NormalizedBatch:
        options = config.parser_options
        category_mapping, mapping_version = self._load_mapping(options)
        layers = options.get("layers")
        if not isinstance(layers, dict) or not layers:
            raise SchemaError("Berlin centers requires configured layers")
        assets_by_layer: dict[str, list] = {key: [] for key in layers}
        for asset in raw.assets:
            values = dict(
                (key.upper(), value)
                for key, value in parse_qsl(
                    urlsplit(asset.resource_url).query, keep_blank_values=True
                )
            )
            if "TYPENAMES" not in values:
                raise ConfigError(
                    "berlin_centers does not support --file local imports; "
                    "it must be ingested from the two WFS layers"
                )
            typename = values.get("TYPENAMES")
            matches = [key for key, layer in layers.items() if layer["typename"] == typename]
            if len(matches) != 1:
                raise SchemaError("Berlin centers asset has an unknown WFS layer")
            assets_by_layer[matches[0]].append(asset)
        for key, assets in assets_by_layer.items():
            if not assets:
                raise SchemaError(f"Berlin centers retrieval is missing layer {key}")

        source_bounds = self._bbox(options, "source_bbox")
        allowed_bounds = self._bbox(options, "allowed_bbox")
        records = []
        metric_geometries = {}
        extra_properties: set[str] = set()
        unmapped_types: dict[str, set[str]] = {key: set() for key in layers}
        layer_counts: Counter[str] = Counter()
        geometry_counts: Counter[str] = Counter()
        type_counts: Counter[str] = Counter()
        seen_ids: set[str] = set()

        for key, layer in layers.items():
            layer_options = self._layer_options(config, key)
            matched = None
            features = []
            for asset in assets_by_layer[key]:
                try:
                    collection = parse_page(Path(asset.path).read_bytes(), layer_options)
                except SchemaError:
                    raise
                except Exception as exc:
                    raise SchemaError(f"Unable to read Berlin centers WFS asset: {exc}") from exc
                if matched is None:
                    matched = collection["numberMatched"]
                elif collection["numberMatched"] != matched:
                    raise SchemaError(f"Berlin centers numberMatched changed in layer {key}")
                features.extend(collection["features"])
            if len(features) != matched:
                raise SchemaError(
                    f"Berlin centers count mismatch in layer {key}: "
                    f"numberMatched {matched}, received {len(features)}"
                )
            minimum, maximum = layer["expected_count_range"]
            if not minimum <= matched <= maximum:
                raise SchemaError(
                    f"Berlin centers expected {key} feature count in "
                    f"{layer['expected_count_range']}, received {matched}"
                )
            pattern = re.compile(layer["id_pattern"])
            for feature in features:
                if not isinstance(feature, dict) or not isinstance(feature.get("properties"), dict):
                    raise SchemaError("WFS feature properties must be an object")
                properties = feature["properties"]
                missing = sorted(set(layer["expected_properties"]) - properties.keys())
                if missing:
                    raise SchemaError(f"WFS feature is missing expected properties: {missing}")
                extra_properties.update(properties.keys() - set(layer["expected_properties"]))
                source_id = properties.get("id")
                if not isinstance(source_id, str) or not source_id.strip():
                    raise SchemaError("WFS ID must be a nonempty string")
                if not pattern.fullmatch(source_id):
                    raise SchemaError(f"WFS ID does not match id_pattern: {source_id!r}")
                if source_id in seen_ids:
                    raise SchemaError(f"Duplicate Berlin centers WFS ID: {source_id}")
                seen_ids.add(source_id)
                expected_feature_id = f"{layer['typename'].split(':')[-1]}.{source_id}"
                if feature.get("id") != expected_feature_id:
                    raise SchemaError(
                        f"WFS feature id mismatch for {source_id}: expected {expected_feature_id}"
                    )
                geometry_data = feature.get("geometry")
                if not isinstance(geometry_data, dict):
                    raise SchemaError(f"Invalid or missing WFS geometry: {source_id}")
                try:
                    source_geometry = shape(geometry_data)
                except Exception as exc:
                    raise SchemaError(f"Invalid WFS geometry: {source_id}") from exc
                if (
                    source_geometry.is_empty
                    or source_geometry.geom_type not in layer["geometry_types"]
                ):
                    raise SchemaError(f"Unsupported WFS geometry type: {source_id}")
                minx, miny, maxx, maxy = source_geometry.bounds
                if not all(math.isfinite(value) for value in (minx, miny, maxx, maxy)) or not (
                    source_bounds[0] <= minx <= maxx <= source_bounds[2]
                    and source_bounds[1] <= miny <= maxy <= source_bounds[3]
                ):
                    raise SchemaError(f"WFS geometry outside source_bbox: {source_id}")
                metric_geometry = source_geometry
                if metric_geometry.is_empty or not metric_geometry.is_valid:
                    raise SchemaError(f"Invalid WFS geometry in EPSG:25833: {source_id}")
                geometry = transform(_TO_WGS84_FN, source_geometry)
                west, south, east, north = geometry.bounds
                if not (
                    allowed_bounds[0] <= west <= east <= allowed_bounds[2]
                    and allowed_bounds[1] <= south <= north <= allowed_bounds[3]
                ):
                    raise SchemaError(f"WFS geometry outside allowed_bbox: {source_id}")

                center_type = properties["typ"]
                if center_type is not None and not isinstance(center_type, str):
                    raise SchemaError(f"WFS typ must be a string or null for ID {source_id}")
                if isinstance(center_type, str) and not center_type.strip():
                    center_type = None
                category = category_mapping["layers"].get(key, {}).get(center_type)
                quality_flags = []
                if center_type is None:
                    quality_flags.append("center_type_missing")
                elif category is None:
                    quality_flags.append("center_type_unmapped")
                    unmapped_types[key].add(center_type)

                name_value = properties["name"]
                if name_value is None or isinstance(name_value, str) and not name_value.strip():
                    name = None
                    quality_flags.append("name_missing")
                elif isinstance(name_value, str):
                    name = name_value
                else:
                    raise SchemaError(f"WFS name must be a string or null for ID {source_id}")

                parent_center_id = None
                if key == "zh":
                    metric_geometries[source_id] = metric_geometry
                layer_counts[key] += 1
                geometry_counts[geometry.geom_type] += 1
                type_counts[center_type or "(missing)"] += 1
                records.append(
                    RetailCenter(
                        source_id=config.dataset_id,
                        snapshot_id=raw.snapshot_id,
                        source_record_id=source_id,
                        record_id=source_id,
                        geometry=mapping(geometry),
                        layer=layer["typename"],
                        wfs_feature_id=expected_feature_id,
                        center_type=center_type,
                        center_category=category["category"] if category else None,
                        center_role=category["role"] if category else None,
                        hierarchy_level=category["hierarchy_level"] if category else None,
                        name=name,
                        parent_center_id=parent_center_id,
                        category_mapping_version=mapping_version,
                        source_url=self._source_url(layer["resource_url"], expected_feature_id),
                        attributes={
                            name: value
                            for name, value in properties.items()
                            if name not in {"id", "typ", "name"} and isinstance(value, str)
                        },
                        quality_flags=tuple(quality_flags),
                    )
                )

        polygon_records = [
            record
            for record in records
            if record.layer == layers["zh"]["typename"]
            and record.geometry["type"] in {"Polygon", "MultiPolygon"}
        ]
        for index, record in enumerate(records):
            if record.layer != layers["zh"]["typename"] or record.geometry["type"] != "Point":
                continue
            metric_point = metric_geometries[record.source_record_id]
            covering = [
                polygon
                for polygon in polygon_records
                if metric_geometries[polygon.source_record_id].covers(metric_point)
            ]
            flags = list(record.quality_flags)
            parent_id = None
            if len(covering) == 1:
                parent_id = covering[0].source_record_id
            elif len(covering) > 1:
                flags.append("multiple_parent_areas")
            elif record.center_type in {"Zentrumsbereichskern", "Zentrumsbereichskern (klein)"}:
                flags.append("center_core_without_parent_area")
            records[index] = replace(record, parent_center_id=parent_id, quality_flags=tuple(flags))

        warnings = list(options.get("limitations", ()))
        for key in sorted(layer_counts):
            warnings.append(f"WFS {key} feature count: {layer_counts[key]}")
        warnings.append(
            "WFS geometry type counts: "
            + ", ".join(f"{name} {count}" for name, count in sorted(geometry_counts.items()))
        )
        warnings.append(
            "WFS center_type counts: "
            + ", ".join(f"{name} {count}" for name, count in sorted(type_counts.items()))
        )
        if extra_properties:
            warnings.append(f"WFS returned extra properties: {', '.join(sorted(extra_properties))}")
        for key, labels in unmapped_types.items():
            if labels:
                warnings.append(f"Unmapped {key} center types: {', '.join(sorted(labels))}")
        return NormalizedBatch(
            records=tuple(records), input_rows=len(records), warnings=tuple(warnings)
        )

    @staticmethod
    def _bbox(options: dict, name: str) -> tuple[float, float, float, float]:
        value = options.get(name)
        if (
            not isinstance(value, (list, tuple))
            or len(value) != 4
            or any(
                isinstance(item, bool)
                or not isinstance(item, (int, float))
                or not math.isfinite(item)
                for item in value
            )
            or value[0] > value[2]
            or value[1] > value[3]
        ):
            raise SchemaError(f"Berlin centers requires a valid {name}")
        return tuple(value)

    def validate(self, batch: NormalizedBatch) -> QualityReport:
        errors = []
        if not batch.records:
            errors.append("Berlin centers snapshot is empty")
        if batch.rejected_rows or batch.input_rows != len(batch.records):
            errors.append("Berlin centers snapshot contains rejected or missing rows")
        seen_ids = set()
        layers = set()
        for record in batch.records:
            if not isinstance(record, RetailCenter):
                errors.append("Berlin centers snapshot contains a non-RetailCenter record")
                continue
            if record.record_id in seen_ids:
                errors.append(f"Duplicate Berlin centers record ID: {record.record_id}")
            seen_ids.add(record.record_id)
            layers.add(record.layer)
            try:
                geometry = shape(record.geometry)
            except Exception:
                errors.append(f"Invalid retail-center geometry: {record.record_id}")
                continue
            if (
                geometry.is_empty
                or not geometry.is_valid
                or geometry.geom_type not in {"Point", "Polygon", "MultiPolygon"}
            ):
                errors.append(f"Invalid retail-center geometry: {record.record_id}")
        required_layers = {
            "step_zen_2040:step_zen_2040_zh",
            "step_zen_2040:step_zen_2040_fma",
        }
        if not required_layers.issubset(layers):
            errors.append("Berlin centers snapshot is missing a layer")
        return QualityReport(
            valid=not errors,
            input_rows=batch.input_rows,
            output_rows=len(batch.records),
            rejected_rows=batch.rejected_rows,
            warnings=batch.warnings,
            errors=tuple(errors),
        )

    def check_parent(self, config: DatasetConfig, records: tuple, parent_records: tuple) -> dict:
        centers = [record for record in records if isinstance(record, RetailCenter)]
        parents = [record for record in parent_records if isinstance(record, Area)]
        if len(centers) != len(records) or len(parents) != len(parent_records):
            raise IncompatibleSnapshotError(
                "Berlin centers parent check requires RetailCenter and Area records"
            )
        parent_geometries = []
        for area in parents:
            geometry = transform(_TO_METRIC_FN, shape(area.geometry))
            if geometry.is_empty or not geometry.is_valid:
                raise IncompatibleSnapshotError(f"Invalid parent LOR geometry: {area.area_code}")
            parent_geometries.append(geometry)
        layer_counts = Counter()
        geometry_counts = Counter()
        for center in centers:
            geometry = transform(_TO_METRIC_FN, shape(center.geometry))
            point = geometry if geometry.geom_type == "Point" else geometry.representative_point()
            if not any(parent.covers(point) for parent in parent_geometries):
                raise IncompatibleSnapshotError(
                    f"Retail center is outside the parent berlin_lor snapshot: {center.record_id}"
                )
            layer_counts[center.layer] += 1
            geometry_counts[geometry.geom_type] += 1
        return {
            "parent_dataset": config.parser_options.get("parent_dataset", "berlin_lor"),
            "centers_located_in_parent": len(centers),
            "layer_counts": dict(sorted(layer_counts.items())),
            "geometry_type_counts": dict(sorted(geometry_counts.items())),
        }
