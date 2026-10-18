"""Offline LOR safety/meaning checks using synthetic, clearly labeled geometry."""

import hashlib
import json
import zipfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import py7zr
import pytest

from app.connectors.lor import LORConnector
from app.domain import DatasetConfig, RawAsset, RawBundle, SchemaError

FIXTURE = Path(__file__).parents[1] / "fixtures/lor/areas.geojson"
CONFIG = Path(__file__).parents[2] / "config/datasets/berlin_lor.json"


def configuration(**parser_options):
    values = json.loads(CONFIG.read_text())
    values["allowed_hosts"] = tuple(values["allowed_hosts"])
    values["source_crs"] = "EPSG:4326"
    values["parser_options"].update(expected_rows=2, name_field="PLR_NAME", **parser_options)
    return DatasetConfig(**values)


def bundle(path):
    return RawBundle(
        assets=(
            RawAsset(
                path=str(path),
                sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                size_bytes=path.stat().st_size,
                resource_url="https://example.org/synthetic",
            ),
        ),
        retrieved_at=datetime.now(timezone.utc),
        snapshot_id="synthetic-lor",
        request_fingerprint="synthetic",
    )


def edited(tmp_path, mutate):
    source = json.loads(FIXTURE.read_text())
    mutate(source)
    path = tmp_path / "edited.geojson"
    path.write_text(json.dumps(source))
    return path


def test_preserves_polygons_codes_edition_and_identity():
    batch = LORConnector().normalize(bundle(FIXTURE), configuration())
    first, second = batch.records
    assert first.area_code == "01011101"
    assert first.boundary_edition == "2021"
    assert first.hierarchy == "planungsraum"
    assert first.source_record_id == first.area_code
    assert first.geometry["type"] == "Polygon"
    assert second.geometry["type"] == "MultiPolygon"
    assert LORConnector().validate(batch).valid


def test_code_only_archive_does_not_invent_area_name():
    config = configuration()
    config = replace(config, parser_options={**config.parser_options, "name_field": None})
    batch = LORConnector().normalize(bundle(FIXTURE), config)
    assert batch.records[0].name == "01011101"
    assert batch.records[0].quality_flags == ("name_not_supplied",)
    assert batch.warnings


@pytest.mark.parametrize(
    "mutation,match",
    [
        (lambda data: data["features"][0]["properties"].pop("PLR_ID"), "Invalid LOR string code"),
        (
            lambda data: data["features"][0]["properties"].update(PLR_ID=1011101),
            "Invalid LOR string code",
        ),
        (lambda data: data["features"][1]["properties"].update(PLR_ID="01011101"), "Duplicate LOR"),
        (lambda data: data["features"].pop(), "Incomplete LOR"),
        (
            lambda data: data["features"][0].update(
                geometry={"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}
            ),
            "Berlin extent",
        ),
        (
            lambda data: data["features"][0].update(
                geometry={
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [13.35, 52.5],
                            [13.36, 52.51],
                            [13.36, 52.5],
                            [13.35, 52.51],
                            [13.35, 52.5],
                        ]
                    ],
                }
            ),
            "Invalid or missing",
        ),
    ],
)
def test_rejects_schema_drift_duplicates_incomplete_or_invalid_data(tmp_path, mutation, match):
    with pytest.raises(SchemaError, match=match):
        LORConnector().normalize(bundle(edited(tmp_path, mutation)), configuration())


def test_projects_shapefile_to_wgs84_and_requires_known_crs(tmp_path):
    path = tmp_path / "areas.shp"
    gpd.read_file(FIXTURE).to_crs(25833).to_file(path)
    config = replace(configuration(), source_crs="EPSG:25833")
    result = LORConnector().normalize(bundle(path), config)
    assert result.records[0].geometry["coordinates"][0][0] == pytest.approx((13.35, 52.5))
    path.with_suffix(".prj").unlink()
    with pytest.raises(SchemaError, match="CRS is absent"):
        LORConnector().normalize(bundle(path), replace(config, source_crs=None))


def test_rejects_declared_crs_mismatch():
    with pytest.raises(SchemaError, match="CRS differs"):
        LORConnector().normalize(bundle(FIXTURE), replace(configuration(), source_crs="EPSG:25833"))


@pytest.mark.parametrize("kind", ["zip", "7z"])
def test_explicit_archive_member(tmp_path, kind):
    path = tmp_path / f"areas.{kind}"
    if kind == "zip":
        with zipfile.ZipFile(path, "w") as archive:
            archive.write(FIXTURE, "areas.geojson")
    else:
        with py7zr.SevenZipFile(path, "w") as archive:
            archive.write(FIXTURE, "areas.geojson")
    batch = LORConnector().normalize(bundle(path), configuration(archive_member="areas.geojson"))
    assert len(batch.records) == 2


@pytest.mark.parametrize(
    "name,budget,match", [("../escape.geojson", 500000, "unsafe"), ("areas.geojson", 1, "budget")]
)
def test_rejects_archive_traversal_and_expansion_budget(tmp_path, name, budget, match):
    path = tmp_path / "areas.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(name, FIXTURE.read_bytes())
    with pytest.raises(SchemaError, match=match):
        LORConnector().normalize(
            bundle(path), configuration(archive_member="areas.geojson", max_unpacked_bytes=budget)
        )
