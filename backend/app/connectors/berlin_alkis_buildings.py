"""Bounded ALKIS building footprints from Berlin's WFS."""

from __future__ import annotations

import math
import re
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pyproj import Transformer
from shapely import make_valid
from shapely.geometry import MultiPolygon, mapping, shape
from shapely.ops import transform

from app.connectors.base import FileConnector
from app.connectors.berlin_wfs import BerlinWFSConnector, parse_page
from app.domain import (
    Building,
    ConfigError,
    DatasetConfig,
    FetchRequest,
    NormalizedBatch,
    QualityReport,
    RawBundle,
    SchemaError,
)

_TO_WGS84 = Transformer.from_crs("EPSG:25833", "EPSG:4326", always_xy=True)
_EXPECTED_PROPERTIES = {
    "uuid",
    "gfk",
    "bezgfk",
    "ofl",
    "bezofl",
    "aog",
    "aug",
    "hoh",
    "bat",
    "bezbat",
    "nam",
    "baw",
    "bezbaw",
    "zus",
    "bezzus",
    "gkn",
    "des",
    "bezdes",
    "lag",
    "namlag",
    "hnr",
    "pnr",
    "lnr",
    "bezeich",
    "shape_area",
}
_INTEGER_FIELDS = {"gfk", "ofl", "aog", "aug"}
_CODE_FIELDS = {"bat", "baw", "zus"}
_STRING_FIELDS = _EXPECTED_PROPERTIES - _INTEGER_FIELDS - _CODE_FIELDS - {"shape_area"}
_ATTRIBUTE_FIELDS = (
    "hoh",
    "ofl",
    "bezofl",
    "bat",
    "baw",
    "bezbaw",
    "zus",
    "bezzus",
    "gkn",
    "des",
    "bezdes",
    "hnr",
    "pnr",
    "lnr",
    "lag",
    "namlag",
)
_TO_METRIC = Transformer.from_crs("EPSG:4326", "EPSG:25833", always_xy=True)
_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_BBOX_FILTER = re.compile(
    rf"bezeich='AX_Gebaeude' AND BBOX\(geom,({_NUMBER}),({_NUMBER}),({_NUMBER}),({_NUMBER})\)"
)


def _query_values(url: str) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for key, value in parse_qsl(urlsplit(url).query, keep_blank_values=True):
        values.setdefault(key.upper(), []).append(value)
    return values


def extent_from_resource_url(resource_url: str) -> tuple[float, float, float, float] | None:
    try:
        filters = _query_values(resource_url).get("CQL_FILTER", ())
    except (TypeError, ValueError):
        return None
    if len(filters) != 1:
        return None
    match = _BBOX_FILTER.fullmatch(filters[0])
    if match is None:
        return None
    bbox = tuple(float(value) for value in match.groups())
    if not all(math.isfinite(value) for value in bbox) or bbox[0] > bbox[2] or bbox[1] > bbox[3]:
        return None
    return bbox


def _extent_filter(options: dict) -> str:
    extent = options.get("extent")
    bbox = extent.get("bbox_25833") if isinstance(extent, dict) else None
    if (
        not isinstance(extent, dict)
        or not isinstance(extent.get("name"), str)
        or not (
            isinstance(bbox, (list, tuple))
            and len(bbox) == 4
            and all(
                not isinstance(value, bool)
                and isinstance(value, (int, float))
                and math.isfinite(value)
                for value in bbox
            )
            and bbox[0] <= bbox[2]
            and bbox[1] <= bbox[3]
        )
    ):
        raise ConfigError("ALKIS buildings requires a valid configured extent")
    bounds = ",".join(str(value) for value in bbox)
    return f"bezeich='AX_Gebaeude' AND BBOX(geom,{bounds})"


def _check_config(config: DatasetConfig) -> dict:
    options = config.parser_options
    if options.get("typename") != "alkis_gebaeude:gebaeude":
        raise ConfigError("ALKIS buildings requires typename alkis_gebaeude:gebaeude")
    if options.get("object_type") != "AX_Gebaeude":
        raise ConfigError("ALKIS buildings object_type must be AX_Gebaeude")
    query = _query_values(config.resource_url)
    expected_filter = _extent_filter(options)
    expected_bbox = tuple(float(value) for value in options["extent"]["bbox_25833"])
    if (
        query.get("CQL_FILTER") != [expected_filter]
        or extent_from_resource_url(config.resource_url) != expected_bbox
    ):
        raise ConfigError(
            "ALKIS buildings resource_url CQL_FILTER must match the configured extent"
        )
    for key, expected in (
        ("TYPENAMES", options["typename"]),
        ("OUTPUTFORMAT", "application/json"),
        ("SORTBY", "uuid"),
    ):
        if query.get(key) != [expected]:
            raise ConfigError(f"ALKIS buildings resource_url requires {key}={expected}")
    if options.get("expected_crs") != "urn:ogc:def:crs:EPSG::25833":
        raise ConfigError("ALKIS buildings requires the reviewed EPSG:25833 CRS")
    if set(options.get("expected_properties", ())) != _EXPECTED_PROPERTIES:
        raise ConfigError("ALKIS buildings expected_properties must contain the reviewed 25 fields")
    if type(options.get("page_size")) is not int or options["page_size"] <= 0:
        raise ConfigError("ALKIS buildings page_size must be a positive integer")
    if type(options.get("max_pages")) is not int or options["max_pages"] <= 0:
        raise ConfigError("ALKIS buildings max_pages must be a positive integer")
    if type(options.get("max_total_bytes")) is not int or options["max_total_bytes"] <= 0:
        raise ConfigError("ALKIS buildings max_total_bytes must be a positive integer")
    count_range = options.get("expected_count_range")
    if (
        not isinstance(count_range, (list, tuple))
        or len(count_range) != 2
        or any(type(value) is not int or value < 0 for value in count_range)
        or count_range[0] > count_range[1]
    ):
        raise ConfigError("ALKIS buildings requires a valid expected_count_range")
    if not isinstance(options.get("id_pattern"), str):
        raise ConfigError("ALKIS buildings requires an id_pattern")
    return options


def _wfs_options(options: dict) -> dict:
    return {
        **options,
        "id_field": "uuid",
        "record_type": "area",
        "field_map": {"area_code": "uuid", "name": "uuid"},
        "attribute_fields": [],
        "hierarchy": "building",
        "geometry_types": ["Polygon", "MultiPolygon"],
    }


def _polygonal(geometry, original_type: str):
    if geometry.geom_type == "GeometryCollection":
        polygons = []

        def collect(item):
            if item.geom_type == "Polygon":
                polygons.append(item)
            elif item.geom_type in {"MultiPolygon", "GeometryCollection"}:
                for child in item.geoms:
                    collect(child)

        collect(geometry)
        if not polygons:
            return None
        geometry = polygons[0] if len(polygons) == 1 else MultiPolygon(polygons)
    if geometry.geom_type not in {"Polygon", "MultiPolygon"} or geometry.is_empty:
        return None
    if original_type == "MultiPolygon" and geometry.geom_type == "Polygon":
        geometry = MultiPolygon([geometry])
    return geometry


def _as_nonblank(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise SchemaError("ALKIS text properties must be strings or null")
    return value if value.strip() else None


def _integer(properties: dict, name: str, source_id: str) -> int | None:
    value = properties[name]
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise SchemaError(f"ALKIS {name} must be a nonnegative integer or null for {source_id}")
    return value


def _coded_text(value, source_id: str, name: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value if value.strip() else None
    if type(value) is int and value >= 0:
        return str(value)
    raise SchemaError(
        f"ALKIS {name} must be a string, nonnegative integer, or null for {source_id}"
    )


def _address_labels(value: str | None, pseudonumber: str | None) -> tuple[tuple[str, ...], bool]:
    if value is None:
        return (), False
    labels = []
    unparsed = False
    for raw_part in value.split(";"):
        part = raw_part.strip()
        match = re.fullmatch(r"(?P<street>[^\d]+?)(?:\s+(?P<nums>\d.*))?", part)
        if not match:
            labels.append(part)
            unparsed = True
            continue
        street = match.group("street").strip()
        numbers = match.group("nums")
        if not street:
            labels.append(part)
            unparsed = True
            continue
        if pseudonumber is not None:
            labels.append(f"{street} {pseudonumber}")
        elif numbers:
            for number in numbers.split(","):
                if number.strip():
                    normalized_number = re.sub(r"\s+", "", number).upper()
                    labels.append(f"{street} {normalized_number}")
        else:
            labels.append(street)
    return tuple(labels), unparsed


def _source_url(resource_url: str, feature_id: str) -> str:
    parts = urlsplit(resource_url)
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.upper() != "RESOURCEID"
    ]
    query.append(("RESOURCEID", feature_id))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _check_bounds(geometry, bounds, message: str) -> None:
    minx, miny, maxx, maxy = geometry.bounds
    if not all(math.isfinite(value) for value in (minx, miny, maxx, maxy)) or not (
        bounds[0] <= minx <= maxx <= bounds[2] and bounds[1] <= miny <= maxy <= bounds[3]
    ):
        raise SchemaError(message)


class BerlinALKISBuildingsConnector(FileConnector):
    adapter_version = "1.0.0"

    async def fetch(self, request: FetchRequest) -> RawBundle:
        options = _check_config(request.config)
        area_config = replace(
            request.config,
            parser_options=_wfs_options(options),
        )
        return await BerlinWFSConnector(self.fetcher).fetch(
            request.__class__(
                config=area_config,
                staging_dir=request.staging_dir,
                snapshot_id=request.snapshot_id,
            )
        )

    def normalize(self, raw: RawBundle, config: DatasetConfig) -> NormalizedBatch:
        options = _check_config(config)
        if not raw.assets:
            raise SchemaError("ALKIS buildings requires the complete multi-page WFS bundle")
        parsed = []
        matched = None
        for asset in raw.assets:
            query = _query_values(asset.resource_url)
            if not all(
                len(query.get(key, ())) == 1
                for key in ("TYPENAMES", "CQL_FILTER", "COUNT", "STARTINDEX")
            ):
                raise ConfigError(
                    "ALKIS buildings cannot normalize a local file; a paged WFS bundle is required"
                )
            if query["TYPENAMES"] != [options["typename"]] or query["CQL_FILTER"] != [
                _extent_filter(options)
            ]:
                raise ConfigError("ALKIS page URL differs from the configured WFS request")
            try:
                collection = parse_page(Path(asset.path).read_bytes(), _wfs_options(options))
            except SchemaError:
                raise
            except Exception as exc:
                raise SchemaError(f"Unable to read ALKIS WFS asset: {exc}") from exc
            if matched is None:
                matched = collection["numberMatched"]
            elif collection["numberMatched"] != matched:
                raise SchemaError("ALKIS numberMatched changed between pages")
            parsed.extend(collection["features"])
        if matched is None or len(parsed) != matched:
            raise SchemaError(
                f"ALKIS count mismatch: numberMatched {matched}, received {len(parsed)}"
            )
        minimum, maximum = options["expected_count_range"]
        if not minimum <= matched <= maximum:
            raise SchemaError(
                f"ALKIS extent expected {minimum}..{maximum} buildings, received {matched}"
            )

        pattern = re.compile(options["id_pattern"])
        source_bounds = options["source_bbox"]
        allowed_bounds = options["allowed_bbox"]
        if not isinstance(source_bounds, (list, tuple)) or len(source_bounds) != 4:
            raise ConfigError("ALKIS buildings requires a source_bbox")
        if not isinstance(allowed_bounds, (list, tuple)) or len(allowed_bounds) != 4:
            raise ConfigError("ALKIS buildings requires an allowed_bbox")
        records = []
        seen_ids = set()
        extra_properties = set()
        made_valid_count = 0
        area_diff_count = 0
        mismatch_count = 0
        unparsed_address_count = 0
        pseudonumber_count = 0
        missing_aog_count = 0
        missing_function_count = 0

        for feature in parsed:
            if not isinstance(feature, dict) or not isinstance(feature.get("properties"), dict):
                raise SchemaError("ALKIS WFS feature properties must be an object")
            properties = feature["properties"]
            missing = sorted(_EXPECTED_PROPERTIES - properties.keys())
            if missing:
                raise SchemaError(f"ALKIS feature is missing expected properties: {missing}")
            extra_properties.update(properties.keys() - _EXPECTED_PROPERTIES)
            source_id = properties.get("uuid")
            if not isinstance(source_id, str) or not pattern.fullmatch(source_id):
                raise SchemaError(f"ALKIS uuid does not match id_pattern: {source_id!r}")
            if source_id in seen_ids:
                raise SchemaError(f"Duplicate ALKIS uuid: {source_id}")
            seen_ids.add(source_id)
            expected_feature_id = f"gebaeude.{source_id}"
            quality_flags = []
            if feature.get("id") != expected_feature_id:
                quality_flags.append("feature_id_mismatch")
                mismatch_count += 1
            object_type = _as_nonblank(properties["bezeich"])
            if object_type != options["object_type"]:
                raise SchemaError(f"ALKIS bezeich must be {options['object_type']} for {source_id}")

            typed = {}
            for name in _INTEGER_FIELDS:
                typed[name] = _integer(properties, name, source_id)
            if typed["aog"] is None:
                missing_aog_count += 1
            if typed["gfk"] is None:
                missing_function_count += 1
            for name in _STRING_FIELDS:
                typed[name] = _as_nonblank(properties[name])
            for name in _CODE_FIELDS:
                typed[name] = _coded_text(properties[name], source_id, name)
            for name in ("uuid", "bezeich"):
                if typed[name] is None:
                    raise SchemaError(f"ALKIS {name} must be populated for {source_id}")
            area_value = properties["shape_area"]
            if area_value is not None and (
                isinstance(area_value, bool)
                or not isinstance(area_value, (int, float))
                or not math.isfinite(area_value)
                or area_value < 0
            ):
                raise SchemaError(
                    f"ALKIS shape_area must be finite and nonnegative for {source_id}"
                )

            geometry_data = feature.get("geometry")
            if not isinstance(geometry_data, dict):
                raise SchemaError(f"ALKIS geometry is missing for {source_id}")
            try:
                source_geometry = shape(geometry_data)
            except Exception as exc:
                raise SchemaError(f"ALKIS geometry is invalid for {source_id}") from exc
            original_type = source_geometry.geom_type
            if source_geometry.is_empty or original_type not in {"Polygon", "MultiPolygon"}:
                raise SchemaError(f"ALKIS geometry must be polygonal for {source_id}")
            _check_bounds(
                source_geometry, source_bounds, f"ALKIS geometry outside source_bbox: {source_id}"
            )
            if not source_geometry.is_valid:
                source_geometry = _polygonal(make_valid(source_geometry), original_type)
                if source_geometry is None or not source_geometry.is_valid:
                    raise SchemaError(f"ALKIS geometry cannot be repaired for {source_id}")
                quality_flags.append("geometry_made_valid")
                made_valid_count += 1
            wgs_geometry = transform(_TO_WGS84.transform, source_geometry)
            if wgs_geometry.is_empty or wgs_geometry.geom_type not in {"Polygon", "MultiPolygon"}:
                raise SchemaError(
                    f"ALKIS reprojection produced nonpolygonal geometry for {source_id}"
                )
            if not wgs_geometry.is_valid:
                wgs_geometry = _polygonal(make_valid(wgs_geometry), original_type)
                if wgs_geometry is None or not wgs_geometry.is_valid:
                    raise SchemaError(
                        f"ALKIS reprojected geometry cannot be repaired for {source_id}"
                    )
                quality_flags.append("geometry_made_valid")
                made_valid_count += 1
            _check_bounds(
                wgs_geometry, allowed_bounds, f"ALKIS geometry outside allowed_bbox: {source_id}"
            )
            metric_area = source_geometry.area
            if (
                area_value is not None
                and abs(metric_area - area_value) > 1
                and (abs(metric_area - area_value) / max(metric_area, area_value, 1) > 0.05)
            ):
                quality_flags.append("footprint_area_differs_from_provider")
                area_diff_count += 1

            lag = typed["lag"]
            street_codes = (
                tuple(code.strip() for code in lag.split(",") if code.strip()) if lag else ()
            )
            pnr = typed["pnr"]
            address_labels, address_unparsed = _address_labels(typed["namlag"], pnr)
            if address_unparsed:
                quality_flags.append("address_reference_unparsed")
                unparsed_address_count += 1
            reference_kind = (
                "pseudonumber"
                if pnr is not None
                else "house_number"
                if address_labels and any(re.search(r"\s+\d", label) for label in address_labels)
                else "street_only"
                if address_labels
                else None
            )
            if reference_kind == "pseudonumber":
                pseudonumber_count += 1
            attributes = {}
            for name in _ATTRIBUTE_FIELDS:
                value = typed[name]
                if value is not None:
                    attributes[name] = str(value)
            if address_unparsed:
                attributes["namlag_unparsed"] = typed["namlag"] or ""
            if properties["shape_area"] is not None:
                footprint_area = float(area_value)
            else:
                footprint_area = None

            records.append(
                Building(
                    source_id=config.dataset_id,
                    snapshot_id=raw.snapshot_id,
                    source_record_id=source_id,
                    record_id=f"alkis_building:{source_id}",
                    geometry=mapping(wgs_geometry),
                    alkis_object_type=object_type,
                    function_code=str(typed["gfk"]) if typed["gfk"] is not None else None,
                    function_label=typed["bezgfk"],
                    storeys_above_ground=typed["aog"],
                    storeys_below_ground=typed["aug"],
                    building_name=typed["nam"],
                    footprint_area_m2=footprint_area,
                    street_codes=street_codes,
                    address_labels=address_labels,
                    address_reference_kind=reference_kind,
                    source_url=_source_url(config.resource_url, expected_feature_id),
                    attributes=attributes,
                    quality_flags=tuple(quality_flags),
                )
            )

        warnings = list(options.get("limitations", ()))
        warnings.extend(
            (
                f"ALKIS AX_Gebaeude features in imported extent: {len(records)}",
                f"{missing_aog_count} of {len(records)} buildings have no aog",
                f"{missing_function_count} of {len(records)} buildings have no gfk function code",
                f"{made_valid_count} geometries required make_valid",
                f"{pseudonumber_count} buildings have a pseudonumber reference",
            )
        )
        if area_diff_count:
            warnings.append(
                f"{area_diff_count} footprint areas differ from provider shape_area by more than 5% and 1 m²"
            )
        if mismatch_count:
            warnings.append(f"{mismatch_count} features had feature_id_mismatch")
        if unparsed_address_count:
            warnings.append(f"{unparsed_address_count} address references could not be parsed")
        if extra_properties:
            warnings.append(
                f"ALKIS returned extra properties: {', '.join(sorted(extra_properties))}"
            )
        return NormalizedBatch(
            records=tuple(records), input_rows=len(records), warnings=tuple(warnings)
        )

    def validate(self, batch: NormalizedBatch) -> QualityReport:
        errors = []
        if not batch.records:
            errors.append("ALKIS buildings snapshot is empty")
        if batch.rejected_rows or batch.input_rows != len(batch.records):
            errors.append("ALKIS buildings snapshot contains rejected or missing rows")
        seen = set()
        for record in batch.records:
            if not isinstance(record, Building):
                errors.append("ALKIS buildings snapshot contains a non-Building record")
                continue
            if record.record_id in seen:
                errors.append(f"Duplicate ALKIS building record ID: {record.record_id}")
            seen.add(record.record_id)
            try:
                geometry = shape(record.geometry)
            except Exception:
                errors.append(f"Invalid ALKIS building geometry: {record.record_id}")
                continue
            if (
                geometry.is_empty
                or not geometry.is_valid
                or geometry.geom_type not in {"Polygon", "MultiPolygon"}
            ):
                errors.append(f"Invalid ALKIS building geometry: {record.record_id}")
        return QualityReport(
            valid=not errors,
            input_rows=batch.input_rows,
            output_rows=len(batch.records),
            rejected_rows=batch.rejected_rows,
            warnings=batch.warnings,
            errors=tuple(errors),
        )
