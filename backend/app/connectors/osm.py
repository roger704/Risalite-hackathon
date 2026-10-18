"""OSM POIs from Geofabrik extracts or bounded Overpass responses."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import osmium
from shapely import wkb
from shapely.geometry import LineString, MultiPoint, Point, mapping

from app.connectors.base import FileConnector
from app.domain import (
    ConfigError,
    DatasetConfig,
    NormalizedBatch,
    Place,
    QualityReport,
    RawBundle,
    SchemaError,
)

_MAPPING_NAME = re.compile(r"^[a-z0-9_]+$")
_OSM_ID_TYPES = {"node": "node", "way": "way", "relation": "relation"}


class OSMConnector(FileConnector):
    adapter_version = "1.0.0"

    def normalize(self, raw: RawBundle, config: DatasetConfig) -> NormalizedBatch:
        options = config.parser_options
        mapping_data, selectors = self._load_mapping(options)
        bbox = self._bbox(options.get("allowed_bbox"))
        minimums = self._minimum_counts(options.get("min_expected_counts", {}), mapping_data)
        ratio = options.get("max_unlocatable_ratio", 0.01)
        if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not 0 <= ratio <= 1:
            raise ConfigError("max_unlocatable_ratio must be between zero and one")
        if len(raw.assets) != 1:
            raise SchemaError("OSM import requires exactly one source asset")
        source = Path(raw.assets[0].path)
        source_format = options.get("format")
        if source_format == "osm_pbf":
            records, selected, unlocatable, warnings = self._normalize_osm_file(
                source, config, mapping_data, selectors, raw.snapshot_id
            )
        elif source_format == "overpass_json":
            records, selected, unlocatable, warnings = self._normalize_overpass(
                source, config, mapping_data, selectors, raw.snapshot_id
            )
        else:
            raise ConfigError("OSM format must explicitly be osm_pbf or overpass_json")
        self._expectations = {
            "allowed_bbox": bbox,
            "minimums": minimums,
            "max_unlocatable_ratio": float(ratio),
            "unlocatable": unlocatable,
            "selected": selected,
            "categories": tuple(item["canonical_category"] for item in mapping_data["categories"]),
            "mapping_version": mapping_data["mapping_version"],
            "assembly_failures": sum(
                "geometry_assembly_failed" in r.quality_flags for r in records
            ),
        }
        return NormalizedBatch(
            records=tuple(records),
            input_rows=selected,
            warnings=tuple(options.get("limitations", ())) + tuple(warnings),
        )

    def _normalize_osm_file(self, source, config, mapping_data, selectors, snapshot_id):
        if not source.is_file():
            raise ConfigError("OSM source file does not exist")
        if source.suffix.lower() not in {".osm", ".pbf"}:
            raise SchemaError("OSM PBF/XML source must have a .pbf or .osm extension")
        try:
            return self._read_osm_file(source, config, mapping_data, selectors, snapshot_id)
        except (ConfigError, SchemaError):
            raise
        except Exception as exc:
            raise SchemaError(f"OSM PBF/XML could not be parsed: {exc}") from exc

    def _read_osm_file(self, source, config, mapping_data, selectors, snapshot_id):
        header_warnings = []
        timestamp = None
        try:
            reader = osmium.io.Reader(source)
            timestamp = reader.header().get("osmosis_replication_timestamp")
            reader.close()
        except Exception:
            timestamp = None
        if config.source_edition is not None:
            if not timestamp:
                if config.parser_options.get("allow_unverified_edition") is True:
                    header_warnings.append("source_edition not verified from file header")
                else:
                    raise SchemaError(
                        "OSM extract has no osmosis_replication_timestamp in file header"
                    )
            else:
                try:
                    header_datetime = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                except (AttributeError, ValueError) as exc:
                    raise SchemaError(
                        f"OSM extract has an invalid osmosis_replication_timestamp: {timestamp}"
                    ) from exc
                if header_datetime.tzinfo is None:
                    header_datetime = header_datetime.replace(tzinfo=timezone.utc)
                header_date = header_datetime.astimezone(timezone.utc).date().isoformat()
                if header_date != config.source_edition:
                    raise SchemaError(
                        f"OSM extract edition {header_date} does not match configured "
                        f"source_edition {config.source_edition}"
                    )
        if timestamp:
            header_warnings.append(f"osmosis_replication_timestamp: {timestamp}")

        processor = osmium.FileProcessor(source).with_locations().with_areas()
        factory = osmium.geom.WKBFactory()
        selected_nodes = []
        selected_ways = {}
        selected_relations = {}
        areas = {}
        selected_count = 0

        for obj in processor:
            if isinstance(obj, osmium.osm.Area):
                area_tags = self._tags(obj.tags)
                if self._category(area_tags, mapping_data, selectors) is None:
                    continue
                key_type = "way" if obj.from_way() else "relation"
                key = f"{key_type}/{obj.orig_id()}"
                try:
                    geometry = wkb.loads(factory.create_multipolygon(obj), hex=True)
                    if geometry.is_empty or geometry.geom_type not in {"Polygon", "MultiPolygon"}:
                        continue
                    point = geometry.representative_point()
                    areas[key] = point
                except (RuntimeError, ValueError, TypeError):
                    continue
            elif isinstance(obj, osmium.osm.Node):
                tags = self._tags(obj.tags)
                category = self._category(tags, mapping_data, selectors)
                if category is None:
                    continue
                selected_count += 1
                loc = obj.location
                point = self._point(loc.lon, loc.lat) if loc.valid() else None
                selected_nodes.append((obj.id, tags, category, point))
            elif isinstance(obj, osmium.osm.Way):
                tags = self._tags(obj.tags)
                category = self._category(tags, mapping_data, selectors)
                if category is None:
                    continue
                selected_count += 1
                nodes = list(obj.nodes)
                coordinates = self._way_coordinates(nodes)
                closed = len(nodes) > 2 and nodes[0].ref == nodes[-1].ref
                selected_ways[f"way/{obj.id}"] = (tags, category, coordinates, closed)
            elif isinstance(obj, osmium.osm.Relation):
                tags = self._tags(obj.tags)
                category = self._category(tags, mapping_data, selectors)
                if category is None:
                    continue
                selected_count += 1
                way_members = tuple(member.ref for member in obj.members if member.type == "w")
                node_members = tuple(member.ref for member in obj.members if member.type == "n")
                selected_relations[f"relation/{obj.id}"] = (
                    tags,
                    category,
                    way_members,
                    node_members,
                )

        member_ids = {
            member_id
            for _, _, way_members, _ in selected_relations.values()
            for member_id in way_members
        }
        node_member_ids = {
            member_id
            for _, _, _, node_members in selected_relations.values()
            for member_id in node_members
        }
        relation_coordinates, relation_node_coordinates = self._member_coordinates(
            source, member_ids, node_member_ids
        )
        records = []
        unlocatable = []

        for osm_id, tags, category, point in selected_nodes:
            key = f"node/{osm_id}"
            if point is None:
                unlocatable.append(key)
                continue
            records.append(
                self._place(config, snapshot_id, key, tags, category, mapping_data, point, "node")
            )

        for key, (tags, category, coordinates, closed) in selected_ways.items():
            point = areas.get(key)
            flags = ()
            if point is not None and closed:
                geometry_type = "way_polygon"
            elif not closed and len(coordinates) >= 2:
                point = LineString(coordinates).representative_point()
                geometry_type = "way_line"
            else:
                fallback = coordinates if coordinates else ()
                point = self._fallback_point(fallback)
                if point is None:
                    unlocatable.append(key)
                    continue
                geometry_type = "way_unassembled"
                flags = ("geometry_assembly_failed", "position_from_member_coordinates")
            records.append(
                self._place(
                    config,
                    snapshot_id,
                    key,
                    tags,
                    category,
                    mapping_data,
                    point,
                    geometry_type,
                    flags,
                )
            )

        for key, (tags, category, way_member_ids, node_member_ids) in selected_relations.items():
            relation_type = tags.get("type")
            point = areas.get(key) if relation_type in {"multipolygon", "boundary"} else None
            flags = ()
            if point is not None:
                geometry_type = "relation_multipolygon"
            else:
                coordinates = [
                    coord
                    for member_id in way_member_ids
                    for coord in relation_coordinates.get(member_id, ())
                ]
                coordinates.extend(
                    relation_node_coordinates[member_id]
                    for member_id in node_member_ids
                    if member_id in relation_node_coordinates
                )
                point = self._fallback_point(coordinates)
                if point is None:
                    unlocatable.append(key)
                    continue
                geometry_type = "relation_unassembled"
                flags = ("geometry_assembly_failed", "position_from_member_coordinates")
            records.append(
                self._place(
                    config,
                    snapshot_id,
                    key,
                    tags,
                    category,
                    mapping_data,
                    point,
                    geometry_type,
                    flags,
                )
            )

        warnings = []
        if unlocatable:
            ids = ", ".join(unlocatable[:20])
            warnings.append(
                f"{len(unlocatable)} selected elements had no locatable geometry: {ids}"
            )
        warnings.extend(header_warnings)
        return records, selected_count, len(unlocatable), warnings

    @staticmethod
    def _way_coordinates(nodes):
        return [(node.location.lon, node.location.lat) for node in nodes if node.location.valid()]

    @staticmethod
    def _member_coordinates(source, way_ids, node_ids):
        if not way_ids and not node_ids:
            return {}, {}
        way_coordinates = {}
        node_coordinates = {}
        for obj in osmium.FileProcessor(source).with_locations():
            if isinstance(obj, osmium.osm.Way) and obj.id in way_ids:
                way_coordinates[obj.id] = OSMConnector._way_coordinates(list(obj.nodes))
            elif isinstance(obj, osmium.osm.Node) and obj.id in node_ids and obj.location.valid():
                node_coordinates[obj.id] = (obj.location.lon, obj.location.lat)
        return way_coordinates, node_coordinates

    def _normalize_overpass(self, source, config, mapping_data, selectors, snapshot_id):
        try:
            data = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SchemaError(f"Overpass response is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise SchemaError("Overpass response must be a JSON object")
        if "remark" in data:
            raise SchemaError(f"Overpass response contains a remark: {data['remark']}")
        if not isinstance(data.get("elements"), list):
            raise SchemaError("Overpass response is missing its elements array")

        records = []
        unlocatable = []
        selected = 0
        for element in data["elements"]:
            if not isinstance(element, dict):
                raise SchemaError("Overpass elements must be objects")
            kind = element.get("type")
            if kind not in _OSM_ID_TYPES:
                raise SchemaError(f"Unsupported Overpass element type: {kind!r}")
            osm_id = element.get("id")
            if isinstance(osm_id, bool) or not isinstance(osm_id, (int, str)) or not str(osm_id):
                raise SchemaError("Overpass elements require a valid OSM id")
            raw_tags = element.get("tags", {})
            if not isinstance(raw_tags, dict):
                raise SchemaError("Overpass element tags must be an object")
            tags = self._tags(raw_tags)
            category = self._category(tags, mapping_data, selectors)
            if category is None:
                continue
            selected += 1
            key = f"{kind}/{osm_id}"
            geometry_type = kind
            flags = ()
            if kind == "node":
                point = self._point(element.get("lon"), element.get("lat"))
            else:
                center = element.get("center")
                point = (
                    self._point(center.get("lon"), center.get("lat"))
                    if isinstance(center, dict)
                    else None
                )
                geometry_type = f"{kind}_center"
                flags = ("position_from_overpass_center",)
            if point is None:
                unlocatable.append(key)
                continue
            records.append(
                self._place(
                    config,
                    snapshot_id,
                    key,
                    tags,
                    category,
                    mapping_data,
                    point,
                    geometry_type,
                    flags,
                )
            )
        warnings = []
        if unlocatable:
            warnings.append(
                f"{len(unlocatable)} selected elements had no locatable geometry: "
                f"{', '.join(unlocatable[:20])}"
            )
        osm3s = data.get("osm3s", {})
        if not isinstance(osm3s, dict):
            raise SchemaError("Overpass osm3s metadata must be an object")
        timestamp = osm3s.get("timestamp_osm_base")
        if timestamp:
            warnings.append(f"timestamp_osm_base: {timestamp}")
        return records, selected, len(unlocatable), warnings

    @staticmethod
    def _tags(tags):
        if tags is None:
            return {}
        if isinstance(tags, dict):
            items = tags.items()
        else:
            try:
                items = ((tag.k, tag.v) for tag in tags)
            except (AttributeError, TypeError, ValueError) as exc:
                raise SchemaError("OSM tags must be key/value pairs") from exc
        result = {}
        try:
            for key, value in items:
                if not isinstance(key, str) or not isinstance(value, str):
                    raise SchemaError("OSM tag keys and values must be strings")
                result[key] = value
        except (AttributeError, TypeError, ValueError) as exc:
            raise SchemaError("OSM tags must be key/value pairs") from exc
        return result

    @staticmethod
    def _category(tags, mapping_data, selectors):
        first_selector_match = None
        for key, pattern in selectors:
            value = tags.get(key)
            if value is not None and pattern.search(value):
                if first_selector_match is None:
                    first_selector_match = f"{key}={value}"
        if first_selector_match is None:
            return None
        for category in mapping_data["categories"]:
            for match in category["match"]:
                if tags.get(match["key"]) == match["value"]:
                    return category["canonical_category"], f"{match['key']}={match['value']}", ()
        return None, first_selector_match, ("category_unmapped",)

    @staticmethod
    def _point(longitude, latitude):
        if (
            isinstance(longitude, bool)
            or isinstance(latitude, bool)
            or not isinstance(longitude, (int, float))
            or not isinstance(latitude, (int, float))
            or not math.isfinite(longitude)
            or not math.isfinite(latitude)
            or not -180 <= longitude <= 180
            or not -90 <= latitude <= 90
        ):
            return None
        return Point(float(longitude), float(latitude))

    @staticmethod
    def _fallback_point(coordinates):
        points = [Point(lon, lat) for lon, lat in coordinates]
        return MultiPoint(points).representative_point() if points else None

    @staticmethod
    def _place(
        config, snapshot_id, key, tags, category, mapping_data, point, geometry_type, flags=()
    ):
        canonical, original, category_flags = category
        attributes = {
            tag: tags[tag]
            for tag in mapping_data["attribute_tags"]
            if tag in tags and tags[tag].strip()
        }
        name = (tags.get("name") or "").strip() or None
        return Place(
            source_id=config.dataset_id,
            snapshot_id=snapshot_id,
            source_record_id=key,
            record_id=key,
            geometry=mapping(point),
            source_geometry_type=geometry_type,
            original_category=original,
            category_mapping_version=mapping_data["mapping_version"],
            canonical_category=canonical,
            name=name,
            attributes=attributes,
            quality_flags=tuple(flags) + tuple(category_flags),
        )

    @staticmethod
    def _bbox(value):
        if (
            not isinstance(value, (list, tuple))
            or len(value) != 4
            or any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in value)
            or not all(math.isfinite(item) for item in value)
        ):
            raise ConfigError("allowed_bbox must contain four finite numbers")
        min_lon, min_lat, max_lon, max_lat = value
        if not -180 <= min_lon < max_lon <= 180 or not -90 <= min_lat < max_lat <= 90:
            raise ConfigError("allowed_bbox coordinates are invalid")
        return tuple(float(item) for item in value)

    @staticmethod
    def _minimum_counts(value, mapping_data):
        if not isinstance(value, dict):
            raise ConfigError("min_expected_counts must be an object")
        categories = {item["canonical_category"] for item in mapping_data["categories"]}
        result = {}
        for category, count in value.items():
            if (
                category not in categories
                or isinstance(count, bool)
                or not isinstance(count, int)
                or count < 0
            ):
                raise ConfigError(
                    "min_expected_counts must use mapped categories and nonnegative integers"
                )
            result[category] = count
        return result

    @classmethod
    def _load_mapping(cls, options):
        name = options.get("category_mapping")
        if not isinstance(name, str) or not _MAPPING_NAME.fullmatch(name):
            raise ConfigError("category_mapping must be a lowercase mapping name")
        path = Path(__file__).parents[2] / "config" / "categories" / f"{name}.json"
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise ConfigError(f"Category mapping {name!r} is unavailable") from exc
        expected_sha = options.get("category_mapping_sha256")
        if not isinstance(expected_sha, str) or hashlib.sha256(raw).hexdigest() != expected_sha:
            raise ConfigError(f"Category mapping SHA-256 mismatch for {name}")
        try:
            data = json.loads(raw)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ConfigError(f"Category mapping {name!r} is not valid JSON") from exc
        if (
            not isinstance(data, dict)
            or data.get("mapping_version") != name
            or not isinstance(data.get("selectors"), list)
            or not data["selectors"]
            or not isinstance(data.get("categories"), list)
            or not data["categories"]
            or not isinstance(data.get("attribute_tags"), list)
            or not data["attribute_tags"]
            or any(not isinstance(tag, str) or not tag for tag in data["attribute_tags"])
        ):
            raise ConfigError(f"Category mapping {name!r} has an invalid structure")
        selectors = []
        for selector in data["selectors"]:
            if (
                not isinstance(selector, dict)
                or not isinstance(selector.get("key"), str)
                or not selector["key"]
                or not isinstance(selector.get("value_regex"), str)
                or not selector["value_regex"]
            ):
                raise ConfigError("Category mapping selectors require a key and value_regex")
            try:
                selectors.append((selector["key"], re.compile(selector["value_regex"])))
            except re.error as exc:
                raise ConfigError("Category mapping contains an invalid selector regex") from exc
        names = set()
        for category in data["categories"]:
            if (
                not isinstance(category, dict)
                or not isinstance(category.get("canonical_category"), str)
                or not category["canonical_category"]
                or category["canonical_category"] in names
                or not isinstance(category.get("match"), list)
                or not category["match"]
            ):
                raise ConfigError("Category mapping categories require unique names and matches")
            names.add(category["canonical_category"])
            for match in category["match"]:
                if (
                    not isinstance(match, dict)
                    or not isinstance(match.get("key"), str)
                    or not match["key"]
                    or not isinstance(match.get("value"), str)
                    or not match["value"]
                ):
                    raise ConfigError("Category mapping matches require key and value")
        return data, selectors

    def validate(self, batch: NormalizedBatch) -> QualityReport:
        expectations = getattr(self, "_expectations", None)
        if expectations is None:
            raise SchemaError("OSM validate requires normalize to be called first")
        errors = []
        if not batch.records:
            errors.append("OSM import contains no usable places")
        seen = set()
        counts = Counter()
        geometry_counts = Counter()
        bbox = expectations["allowed_bbox"]
        for record in batch.records:
            if not isinstance(record, Place):
                errors.append("OSM batch contains a non-Place record")
                continue
            if record.record_id in seen:
                errors.append(f"Duplicate OSM record_id: {record.record_id}")
            seen.add(record.record_id)
            point = record.geometry["coordinates"]
            if not bbox[0] <= point[0] <= bbox[2] or not bbox[1] <= point[1] <= bbox[3]:
                errors.append(f"Place {record.record_id} is outside allowed_bbox")
            if record.canonical_category is not None:
                counts[record.canonical_category] += 1
            geometry_counts[record.source_geometry_type] += 1
            if record.category_mapping_version != expectations["mapping_version"]:
                errors.append(f"Place {record.record_id} has a mixed category mapping version")
        for category, minimum in expectations["minimums"].items():
            observed = counts[category]
            if observed < minimum:
                errors.append(
                    f"OSM category {category} count {observed} is below minimum {minimum}"
                )
        selected = expectations["selected"]
        if (
            selected
            and expectations["unlocatable"] / selected > expectations["max_unlocatable_ratio"]
        ):
            errors.append(
                "OSM unlocatable selected-element ratio "
                f"{expectations['unlocatable'] / selected:.4f} exceeds "
                f"{expectations['max_unlocatable_ratio']:.4f}"
            )
        category_counts = {category: counts[category] for category in expectations["categories"]}
        category_counts["unmapped"] = sum(
            1
            for record in batch.records
            if isinstance(record, Place) and record.canonical_category is None
        )
        warnings = list(batch.warnings)
        warnings.append(
            "category_counts: "
            + ", ".join(f"{key}={category_counts[key]}" for key in sorted(category_counts))
        )
        warnings.append(
            "source_geometry_type_counts: "
            + (
                ", ".join(f"{key}={geometry_counts[key]}" for key in sorted(geometry_counts))
                if geometry_counts
                else "none"
            )
        )
        warnings.append(f"geometry_assembly_failed: {expectations['assembly_failures']}")
        return QualityReport(
            valid=not errors,
            input_rows=batch.input_rows,
            output_rows=len(batch.records),
            rejected_rows=batch.rejected_rows,
            warnings=tuple(warnings),
            errors=tuple(errors),
        )
