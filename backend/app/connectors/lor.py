"""Explicitly mapped Berlin LOR boundaries; no network access during parsing."""

from __future__ import annotations

import re
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

import geopandas as gpd
import py7zr
from pyproj import CRS
from shapely.geometry import mapping, shape

from app.connectors.base import FileConnector
from app.domain import (
    Area,
    DatasetConfig,
    NormalizedBatch,
    QualityReport,
    RawBundle,
    SchemaError,
)


def _safe_name(name: str) -> None:
    path = PurePosixPath(name)
    if not name or "\\" in name or path.is_absolute() or ".." in path.parts or ":" in name:
        raise SchemaError("Archive contains an unsafe member path")


def _unpack(source: Path, target: Path, max_bytes: int) -> None:
    """Check every member before extraction, including links and expanded sizes."""
    if max_bytes <= 0:
        raise SchemaError("Archive expansion budget must be positive")
    if source.suffix.lower() == ".zip":
        with zipfile.ZipFile(source) as archive:
            members = archive.infolist()
            if len(members) > 1000 or sum(m.file_size for m in members) > max_bytes:
                raise SchemaError("Archive expansion budget exceeded")
            names = []
            for member in members:
                _safe_name(member.filename)
                if (member.external_attr >> 16) & 0o170000 == 0o120000:
                    raise SchemaError("Archive links are not permitted")
                names.append(str(PurePosixPath(member.filename)))
            if len(names) != len(set(names)):
                raise SchemaError("Archive contains duplicate member paths")
            archive.extractall(target)
    else:
        with py7zr.SevenZipFile(source, mode="r") as archive:
            members = archive.list()
            if len(members) > 1000 or sum(m.uncompressed or 0 for m in members) > max_bytes:
                raise SchemaError("Archive expansion budget exceeded")
            names = []
            for member in members:
                _safe_name(member.filename)
                if member.is_symlink or not (member.is_file or member.is_directory):
                    raise SchemaError("Archive links and special members are not permitted")
                names.append(str(PurePosixPath(member.filename)))
            if len(names) != len(set(names)):
                raise SchemaError("Archive contains duplicate member paths")
            archive.extractall(path=target)
    # Never pass extracted links to a vector reader, regardless of archive metadata.
    if any(path.is_symlink() for path in target.rglob("*")):
        raise SchemaError("Archive contains a link")
    if sum(path.stat().st_size for path in target.rglob("*") if path.is_file()) > max_bytes:
        raise SchemaError("Archive expansion budget exceeded")


class LORConnector(FileConnector):
    adapter_version = "1.0.0"

    def normalize(self, raw: RawBundle, config: DatasetConfig) -> NormalizedBatch:
        options = config.parser_options
        for option in ("code_field", "hierarchy", "code_length", "expected_rows"):
            if option not in options:
                raise SchemaError(f"LOR parser requires explicit {option}")
        if len(raw.assets) != 1:
            raise SchemaError("LOR expects exactly one geospatial asset")
        source = Path(raw.assets[0].path)
        try:
            with tempfile.TemporaryDirectory(prefix="risalite-lor-") as directory:
                if source.suffix.lower() in {".zip", ".7z"}:
                    root = Path(directory)
                    _unpack(source, root, int(options.get("max_unpacked_bytes", 50_000_000)))
                    member = options.get("archive_member")
                    if not member:
                        raise SchemaError("Archive requires an explicit archive_member mapping")
                    _safe_name(member)
                    source = root / member
                    if not source.is_file():
                        raise SchemaError(f"Configured LOR archive member is absent: {member}")
                if source.suffix.lower() not in {".shp", ".geojson", ".json", ".gpkg"}:
                    raise SchemaError("Unsupported LOR vector format")
                frame = gpd.read_file(source, engine="pyogrio")
                return self._normalize_frame(frame, raw, config)
        except SchemaError:
            raise
        except Exception as exc:
            raise SchemaError(f"Cannot parse LOR resource: {exc}") from exc

    def _normalize_frame(
        self, frame: gpd.GeoDataFrame, raw: RawBundle, config: DatasetConfig
    ) -> NormalizedBatch:
        options = config.parser_options
        code_field = options["code_field"]
        name_field = options.get("name_field")
        required = {code_field} | ({name_field} if name_field else set())
        if not required.issubset(frame.columns):
            raise SchemaError(
                f"Missing configured LOR fields: {sorted(required - set(frame.columns))}"
            )
        if len(frame) != int(options["expected_rows"]):
            raise SchemaError(
                f"Incomplete LOR coverage: expected {options['expected_rows']}, received {len(frame)}"
            )
        if frame.crs is None:
            if not config.source_crs:
                raise SchemaError("LOR CRS is absent; a reviewed source_crs is required")
            frame = frame.set_crs(config.source_crs)
        elif config.source_crs and CRS(frame.crs) != CRS(config.source_crs):
            raise SchemaError("LOR file CRS differs from configured source_crs")
        frame = frame.to_crs("EPSG:4326")
        records = []
        seen = set()
        bounds = options.get("allowed_bbox", [13.0, 52.3, 13.9, 52.8])
        for index, row in frame.iterrows():
            code = row[code_field]
            if not isinstance(code, str) or not re.fullmatch(
                rf"\d{{{int(options['code_length'])}}}", code
            ):
                raise SchemaError(f"Invalid LOR string code at row {index}: {code!r}")
            if code in seen:
                raise SchemaError(f"Duplicate LOR code: {code}")
            seen.add(code)
            geometry = row.geometry
            if geometry is None or geometry.is_empty or not geometry.is_valid:
                raise SchemaError(f"Invalid or missing LOR geometry: {code}")
            if geometry.geom_type not in {"Polygon", "MultiPolygon"}:
                raise SchemaError(f"LOR geometry must be a polygon: {code}")
            west, south, east, north = geometry.bounds
            if not (
                bounds[0] <= west <= east <= bounds[2] and bounds[1] <= south <= north <= bounds[3]
            ):
                raise SchemaError(f"LOR geometry outside configured Berlin extent: {code}")
            name = row[name_field] if name_field else code
            if not isinstance(name, str) or not name.strip():
                raise SchemaError(f"Invalid LOR name: {code}")
            records.append(
                Area(
                    source_id=config.dataset_id,
                    snapshot_id=raw.snapshot_id,
                    source_record_id=code,
                    record_id=f"{options['hierarchy']}:{code}",
                    geometry=mapping(geometry),
                    area_code=code,
                    name=name,
                    hierarchy=options["hierarchy"],
                    boundary_edition=config.boundary_edition,
                    quality_flags=() if name_field else ("name_not_supplied",),
                )
            )
        warnings = (
            ()
            if name_field
            else ("Source archive supplies codes only; names use the code as a labeled fallback.",)
        )
        return NormalizedBatch(records=tuple(records), input_rows=len(frame), warnings=warnings)

    def validate(self, batch: NormalizedBatch) -> QualityReport:
        errors = []
        if not batch.records:
            errors.append("LOR snapshot is empty")
        if batch.rejected_rows or batch.input_rows != len(batch.records):
            errors.append("LOR snapshot contains rejected or missing rows")
        keys = set()
        editions = set()
        for record in batch.records:
            if not isinstance(record, Area):
                errors.append("LOR snapshot contains a non-area record")
                continue
            key = (record.hierarchy, record.area_code)
            if key in keys:
                errors.append(f"Duplicate LOR key: {key}")
            keys.add(key)
            editions.add(record.boundary_edition)
            geometry = shape(record.geometry)
            if (
                geometry.is_empty
                or not geometry.is_valid
                or geometry.geom_type not in {"Polygon", "MultiPolygon"}
            ):
                errors.append(f"Invalid LOR polygon: {record.area_code}")
        if len(editions) > 1:
            errors.append("LOR snapshot mixes boundary editions")
        return QualityReport(
            valid=not errors,
            input_rows=batch.input_rows,
            output_rows=len(batch.records),
            rejected_rows=batch.rejected_rows,
            warnings=batch.warnings,
            errors=tuple(errors),
        )
