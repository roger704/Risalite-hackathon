"""IHK Berlin business register extract: streamed CSV parsing, reviewed industry mapping.

IHK counts are a separate inventory from OSM places. Never add both as unique businesses.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import sys
from pathlib import Path

from app.connectors.base import FileConnector
from app.domain import (
    Area,
    Business,
    ConfigError,
    DatasetConfig,
    IncompatibleSnapshotError,
    NormalizedBatch,
    QualityReport,
    RawBundle,
    SchemaError,
    ValidationError,
)

CATEGORY_DIR = Path(__file__).resolve().parents[2] / "config" / "categories"
LFS_POINTER_PREFIX = b"version https://git-lfs.github.com/spec/"
_EMPLOYEES = (
    (re.compile(r"(\d+) Beschäftigte"), lambda m: (int(m[1]), int(m[1]))),
    (re.compile(r"(\d+) - (\d+) Beschäftigte"), lambda m: (int(m[1]), int(m[2]))),
    (re.compile(r"(\d+) und mehr Beschäftigte"), lambda m: (int(m[1]), None)),
)


def parse_employees(label: str | None, unknown_labels) -> tuple[int | None, int | None, str | None]:
    """Return inclusive (min, max, flag). An open upper bound is ``max=None``."""
    if label is None:
        return None, None, "employees_range_missing"
    if label in unknown_labels:
        return None, None, "employees_range_unknown"
    for pattern, bounds in _EMPLOYEES:
        match = pattern.fullmatch(label)
        if match:
            low, high = bounds(match)
            if high is not None and high < low:
                break
            return low, high, None if high is not None else "employees_open_upper_bound"
    return None, None, "employees_range_unrecognized"


class CategoryMapping:
    def __init__(self, data: dict):
        self.version = data.get("mapping_version")
        self.code_system = data.get("code_system")
        if not isinstance(self.version, str) or not isinstance(self.code_system, str):
            raise ConfigError("Category mapping needs mapping_version and code_system")
        self.exact: dict[str, str] = {}
        self.prefixes: dict[str, str] = {}
        for rule in data.get("rules", []):
            category = rule.get("canonical_category")
            if not isinstance(category, str) or not category:
                raise ConfigError("Every category rule needs a canonical_category")
            for kind, target in (("exact", self.exact), ("prefixes", self.prefixes)):
                for code in rule.get(kind, []):
                    if not isinstance(code, str) or not code.isdigit():
                        raise ConfigError(f"Category code must be a digit string: {code!r}")
                    if code in target:
                        raise ConfigError(f"Category code {code} is mapped twice")
                    target[code] = category
        if not self.exact and not self.prefixes:
            raise ConfigError("Category mapping has no rules")
        self._lengths = sorted({len(code) for code in self.prefixes}, reverse=True)

    def category(self, code: str | None) -> str | None:
        if code is None:
            return None
        if code in self.exact:
            return self.exact[code]
        for length in self._lengths:
            if length <= len(code) and code[:length] in self.prefixes:
                return self.prefixes[code[:length]]
        return None

    @classmethod
    def load(cls, options: dict) -> CategoryMapping:
        name = options.get("category_mapping")
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name):
            raise ConfigError("IHK requires a reviewed category_mapping name")
        path = CATEGORY_DIR / f"{name}.json"
        if not path.is_file():
            raise ConfigError(f"Category mapping file is missing: {name}")
        content = path.read_bytes()
        # The pinned hash makes mapping edits change the parser configuration hash.
        if hashlib.sha256(content).hexdigest() != options.get("category_mapping_sha256"):
            raise ConfigError(f"Category mapping {name} differs from the reviewed SHA-256")
        mapping = cls(json.loads(content))
        if mapping.version != name:
            raise ConfigError("Category mapping file name and mapping_version differ")
        return mapping


def check_prefix(path: Path) -> None:
    with path.open("rb") as handle:
        prefix = handle.read(1024)
    stripped = prefix.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if not stripped:
        raise SchemaError("IHK resource is empty")
    if stripped.startswith(LFS_POINTER_PREFIX):
        raise SchemaError("IHK resource is a Git LFS pointer, not the CSV content")
    if stripped.startswith((b"<!doctype html", b"<html")) or b"<html" in stripped:
        raise SchemaError("IHK resource returned HTML instead of CSV")


class IHKConnector(FileConnector):
    adapter_version = "1.0.0"

    def normalize(self, raw: RawBundle, config: DatasetConfig) -> NormalizedBatch:
        if len(raw.assets) != 1:
            raise SchemaError("IHK import requires exactly one reviewed CSV asset")
        options = config.parser_options
        mapping = CategoryMapping.load(options)
        encoding, delimiter = options.get("encoding"), options.get("delimiter")
        expected = options.get("expected_columns")
        if not encoding or not isinstance(delimiter, str) or len(delimiter) != 1:
            raise ConfigError("IHK CSV requires reviewed encoding and one-character delimiter")
        if not isinstance(expected, list) or not expected:
            raise ConfigError("IHK CSV requires reviewed expected_columns")
        nulls = set(options.get("null_markers", []))
        unknown_employees = set(options.get("unknown_employee_labels", []))
        bbox = options.get("berlin_bbox", [13.0, 52.3, 13.9, 52.8])
        code_length = int(options.get("area_code_length", 8))
        path = Path(raw.assets[0].path)
        check_prefix(path)
        pinned = options.get("expected_sha256")
        if pinned and raw.assets[0].sha256 != pinned:
            raise SchemaError("IHK file differs from the reviewed edition's SHA-256")

        records: list[Business] = []
        rejections: list[str] = []
        excluded: list[str] = []
        flag_counts: dict[str, int] = {}
        seen: set[str] = set()
        input_rows = rejected = 0
        intern = sys.intern

        def value(row: dict, column: str) -> str | None:
            text = row[column].strip()
            return None if text in nulls else intern(text)

        try:
            with path.open(encoding=encoding, newline="") as handle:
                reader = csv.DictReader(handle, delimiter=delimiter)
                fields = reader.fieldnames or []
                if len(fields) != len(set(fields)) or set(fields) != set(expected):
                    raise SchemaError(
                        "IHK CSV header changed: "
                        f"missing {sorted(set(expected) - set(fields))}, "
                        f"unexpected {sorted(set(fields) - set(expected))}"
                    )
                for row in reader:
                    input_rows += 1
                    if None in row or any(item is None for item in row.values()):
                        raise SchemaError(
                            f"IHK CSV row width differs from header at line {reader.line_num}"
                        )
                    source_id = value(row, "opendata_id")
                    if source_id is None or not source_id.isdigit():
                        rejections.append(f"Line {reader.line_num}: invalid opendata_id")
                        rejected += 1
                        continue
                    if source_id in seen:
                        rejections.append(f"Duplicate opendata_id {source_id}")
                        rejected += 1
                        continue
                    seen.add(source_id)
                    point = self._point(value(row, "longitude"), value(row, "latitude"))
                    if point is None:
                        excluded.append(source_id)
                        continue
                    flags = []
                    if not (bbox[0] <= point[0] <= bbox[2] and bbox[1] <= point[1] <= bbox[3]):
                        flags.append("outside_berlin_bbox")
                    area_code = value(row, "planungsraum_id")
                    if area_code is None:
                        flags.append("planungsraum_id_missing")
                    elif not re.fullmatch(rf"\d{{{code_length}}}", area_code):
                        flags.append("planungsraum_id_invalid")
                        area_code = None
                    code = value(row, "ihk_branch_id")
                    if code is None:
                        flags.append("industry_code_missing")
                    else:
                        nace, top = value(row, "nace_id"), value(row, "branch_top_level_id")
                        if nace != code[:4] or top != code[:2]:
                            flags.append("industry_hierarchy_inconsistent")
                    category = mapping.category(code)
                    if code is not None and category is None:
                        flags.append("category_unmapped")
                    label = value(row, "employees_range")
                    low, high, employee_flag = parse_employees(label, unknown_employees)
                    if employee_flag:
                        flags.append(employee_flag)
                    age = value(row, "business_age")
                    if age is None:
                        flags.append("business_age_missing")
                    for flag in flags:
                        flag_counts[flag] = flag_counts.get(flag, 0) + 1
                    try:
                        records.append(
                            Business(
                                source_id=config.dataset_id,
                                snapshot_id=raw.snapshot_id,
                                source_record_id=source_id,
                                record_id=source_id,
                                geometry={"type": "Point", "coordinates": point},
                                industry_code=code,
                                industry_code_system=mapping.code_system,
                                industry_description=value(row, "ihk_branch_desc"),
                                category_mapping_version=mapping.version,
                                canonical_category=category,
                                employees_label=label,
                                employees_min=low,
                                employees_max=high,
                                business_age_label=age,
                                area_code=area_code,
                                hierarchy=options.get("hierarchy", "planungsraum"),
                                boundary_edition=config.boundary_edition,
                                quality_flags=tuple(flags),
                            )
                        )
                    except ValidationError as exc:
                        rejections.append(f"opendata_id {source_id}: {exc}")
                        rejected += 1
        except (UnicodeError, csv.Error) as exc:
            raise SchemaError(f"IHK CSV cannot be parsed with reviewed settings: {exc}") from exc

        min_rows = options.get("min_expected_rows")
        if min_rows is not None and input_rows < min_rows:
            rejections.append(f"Expected at least {min_rows} rows, found {input_rows}")
        max_excluded = options.get("max_excluded_row_fraction", 0.0)
        if input_rows and len(excluded) / input_rows > max_excluded:
            rejections.append(
                f"{len(excluded)} of {input_rows} rows lack valid coordinates; "
                f"threshold is {max_excluded:.4%}"
            )
        warnings = list(options.get("limitations", ()))
        if excluded:
            sample = ", ".join(excluded[:10])
            warnings.append(
                f"{len(excluded)} rows without valid WGS84 coordinates were not published "
                f"(opendata_id {sample})"
            )
        for flag, count in sorted(flag_counts.items()):
            warnings.append(f"{count} records flagged {flag}")
        return NormalizedBatch(
            records=tuple(records),
            input_rows=input_rows,
            rejected_rows=rejected,
            rejections=tuple(rejections),
            warnings=tuple(warnings),
        )

    @staticmethod
    def _point(longitude: str | None, latitude: str | None) -> list[float] | None:
        try:
            lon, lat = float(longitude), float(latitude)
        except (TypeError, ValueError):
            return None
        if not (math.isfinite(lon) and math.isfinite(lat)):
            return None
        if not (-180 <= lon <= 180 and -90 <= lat <= 90) or (lon == 0 and lat == 0):
            return None
        return [lon, lat]

    def validate(self, batch: NormalizedBatch) -> QualityReport:
        errors = list(batch.rejections)
        if not batch.records:
            errors.append("IHK import contains no usable business records")
        if any(not isinstance(record, Business) for record in batch.records):
            errors.append("IHK batch contains a non-business record")
        if len({record.record_id for record in batch.records}) != len(batch.records):
            errors.append("IHK batch contains duplicate record IDs")
        return QualityReport(
            valid=not errors,
            input_rows=batch.input_rows,
            output_rows=len(batch.records),
            rejected_rows=batch.rejected_rows,
            warnings=batch.warnings,
            errors=tuple(errors),
        )

    def check_parent(self, config: DatasetConfig, records, parent_records) -> dict:
        """Compare supplied PLR codes with the pinned LOR snapshot; report, never reassign."""
        import geopandas as gpd
        import numpy as np
        import shapely
        from pyproj import Transformer
        from shapely.geometry import shape

        options = config.parser_options
        limits = options.get("parent_check", {})
        hierarchy = options.get("hierarchy", "planungsraum")
        areas = {
            area.area_code: area
            for area in parent_records
            if isinstance(area, Area)
            and area.hierarchy == hierarchy
            and area.boundary_edition == config.boundary_edition
        }
        if not areas:
            raise IncompatibleSnapshotError(
                f"LOR snapshot has no {hierarchy} areas for boundary edition "
                f"{config.boundary_edition}"
            )
        total = len(records)
        missing = [record for record in records if record.area_code is None]
        coded = [record for record in records if record.area_code is not None]
        matched = [record for record in coded if record.area_code in areas]
        unmatched_codes = sorted({r.area_code for r in coded if r.area_code not in areas})

        codes = sorted(areas)
        polygons = gpd.GeoSeries(
            [shape(areas[code].geometry) for code in codes], crs="EPSG:4326"
        ).to_crs("EPSG:25833")
        index = {code: position for position, code in enumerate(codes)}
        outside_ids: list[str] = []
        if matched:
            coordinates = np.array([record.geometry["coordinates"] for record in matched])
            x, y = Transformer.from_crs("EPSG:4326", "EPSG:25833", always_xy=True).transform(
                coordinates[:, 0], coordinates[:, 1]
            )
            supplied = polygons.values[[index[record.area_code] for record in matched]]
            inside = shapely.dwithin(
                np.asarray(supplied), shapely.points(x, y), limits.get("boundary_tolerance_m", 1.0)
            )
            outside_ids = [r.source_record_id for r, ok in zip(matched, inside) if not ok]

        categories: dict[str, int] = {}
        for record in records:
            key = record.canonical_category or "unmapped"
            categories[key] = categories.get(key, 0) + 1
        result = {
            "business_count": total,
            "category_counts": dict(sorted(categories.items())),
            "category_mapping_version": records[0].category_mapping_version if records else None,
            "area_code_missing_count": len(missing),
            "area_code_unmatched_count": len(coded) - len(matched),
            "area_code_unmatched_codes": unmatched_codes[:50],
            "outside_supplied_area_count": len(outside_ids),
            "outside_supplied_area_sample": outside_ids[:20],
            "lor_areas_without_business": len(set(areas) - {r.area_code for r in matched}),
            "boundary_tolerance_m": limits.get("boundary_tolerance_m", 1.0),
            "join_method": (
                "supplied planungsraum_id matched to the pinned LOR snapshot; point-in-polygon "
                "check in EPSG:25833; supplied codes are reported, never reassigned"
            ),
            "inventory": "IHK registered businesses only; not deduplicated against OSM places",
        }
        checks = (
            ("missing", len(missing), "max_missing_area_code_fraction"),
            ("unmatched", len(coded) - len(matched), "max_unmatched_area_code_fraction"),
            ("outside", len(outside_ids), "max_outside_supplied_area_fraction"),
        )
        for name, count, key in checks:
            threshold = limits.get(key, 0.01)
            if total and count / total > threshold:
                raise IncompatibleSnapshotError(
                    f"Systemic IHK/LOR mismatch: {count} of {total} records {name} "
                    f"({key}={threshold})"
                )
        return result
