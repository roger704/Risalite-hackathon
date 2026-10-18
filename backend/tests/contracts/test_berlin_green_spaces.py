"""Offline contract tests for official green spaces and playgrounds (synthetic WFS pages)."""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from shapely.geometry import shape

from app.connectors.berlin_green_spaces import BerlinGreenSpacesConnector
from app.domain import (
    DatasetConfig,
    FetchRequest,
    GreenSpace,
    RawAsset,
    RawBundle,
    SchemaError,
    ValidationError,
    record_from_dict,
    record_to_dict,
)
from app.infrastructure.http import HTTPFetcher

BACKEND = Path(__file__).parents[2]
FIXTURES = BACKEND / "tests/fixtures/berlin_parks"
CONFIG = BACKEND / "config/datasets/berlin_parks.json"
LAYER_URL = (
    "https://gdi.berlin.de/services/wfs/gruenanlagen?SERVICE=WFS&TYPENAMES=gruenanlagen%3A{}"
    "&COUNT=2&STARTINDEX={}"
)


def configuration():
    values = json.loads(CONFIG.read_text())
    values["allowed_hosts"] = tuple(values["allowed_hosts"])
    options = values["parser_options"]
    options["page_size"] = 2
    options["layers"][0]["expected_count_range"] = [1, 10]
    options["layers"][1]["expected_count_range"] = [1, 10]
    return DatasetConfig(**values)


def pages(tmp_path, mutate=None):
    data = {
        name: json.loads((FIXTURES / f"{name}.json").read_text())
        for name in ("gruenanlagen-0000", "gruenanlagen-0001", "spielplaetze-0000")
    }
    if mutate:
        mutate(data)
    assets = []
    for name, collection in data.items():
        layer, page = name.split("-")
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(collection))
        assets.append(
            RawAsset(
                path=str(path),
                sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                size_bytes=path.stat().st_size,
                resource_url=LAYER_URL.format(layer, int(page) * 2),
            )
        )
    return RawBundle(
        assets=tuple(assets),
        retrieved_at=datetime.now(timezone.utc),
        snapshot_id="synthetic-parks",
        request_fingerprint="synthetic",
    )


def by_id(batch):
    return {record.source_record_id: record for record in batch.records}


def test_maps_layers_preserves_polygons_and_keeps_missing_values_missing(tmp_path):
    connector = BerlinGreenSpacesConnector()
    batch = connector.normalize(pages(tmp_path), configuration())
    report = connector.validate(batch)
    assert report.valid and report.output_rows == batch.input_rows == 5
    records = by_id(batch)
    park, multi, blank_code = (records[f"0000a001:0000000{n}"] for n in (1, 2, 3))
    playground, unknown = records["0000b001:00000001"], records["0000b001:00000002"]

    assert {r.feature_type for r in batch.records} == {"green_space", "playground"}
    assert park.record_id == "green_space:0000a001:00000001"
    assert playground.record_id == "playground:0000b001:00000001"
    assert park.original_category == "Grünanlage" and playground.original_category == "Spielplatz"
    assert park.geometry["type"] == "Polygon"
    assert multi.geometry["type"] == "MultiPolygon" and len(multi.geometry["coordinates"]) == 2
    assert unknown.geometry["type"] == "MultiPolygon"
    for record in batch.records:
        assert shape(record.geometry).contains(shape(record.display_point))
        assert record.display_point_method == "representative_point_epsg25833"
        assert 13.0 < shape(record.geometry).bounds[0] < 13.9

    assert park.name == "Synthetic Volkspark" and park.source_area_m2 == 74000
    assert park.attributes["baujahr"] == "1923" and "namezusatz" not in park.attributes
    assert "attribute_missing:namezusatz" in park.quality_flags
    assert multi.name is None and "name_missing" in multi.quality_flags
    assert multi.source_area_m2 is None
    assert "katasterfl_zero_treated_as_missing" in multi.quality_flags
    assert multi.geometry_area_m2 > 0
    assert blank_code.area_code is None and blank_code.hierarchy is None
    assert blank_code.source_area_m2 is None and "katasterfl_missing" in blank_code.quality_flags
    assert playground.net_play_area_m2 == 6800.5 and park.net_play_area_m2 is None
    assert unknown.net_play_area_m2 is None
    assert "nettospfl_zero_treated_as_missing" in unknown.quality_flags

    assert park.adjacent_record_ids == (playground.record_id,)
    assert playground.adjacent_record_ids == (park.record_id,)
    assert unknown.adjacent_record_ids == ()
    warnings = " ".join(report.warnings)
    assert "not walking distance" in warnings and "OSM" in warnings
    assert "Feature counts: green_space=3, playground=2" in warnings
    assert "2026-04-09" in warnings
    assert record_from_dict(json.loads(json.dumps(record_to_dict(park)))) == park


def test_blank_area_values_are_missing(tmp_path):
    def blank(data):
        properties = data["spielplaetze-0000"]["features"][0]["properties"]
        properties.update(nettospfl=" ", katasterfl=" ")

    record = by_id(BerlinGreenSpacesConnector().normalize(pages(tmp_path, blank), configuration()))[
        "0000b001:00000001"
    ]
    assert record.net_play_area_m2 is None and record.source_area_m2 is None
    assert {"nettospfl_missing", "katasterfl_missing"} <= set(record.quality_flags)


def test_rejects_duplicate_ids_across_layers(tmp_path):
    def duplicate(data):
        data["spielplaetze-0000"]["features"][0]["properties"]["pitid"] = "0000a001:00000001"

    with pytest.raises(SchemaError, match="Duplicate WFS ID"):
        BerlinGreenSpacesConnector().normalize(pages(tmp_path, duplicate), configuration())


def test_flags_object_types_that_disagree_with_their_layer(tmp_path):
    def retype(data):
        features = data["spielplaetze-0000"]["features"]
        features[0]["properties"]["objartname"] = "Grünanlage"
        features[1]["properties"]["objartname"] = "Kleingarten"

    records = by_id(
        BerlinGreenSpacesConnector().normalize(pages(tmp_path, retype), configuration())
    )
    assert records["0000b001:00000001"].feature_type == "playground"
    assert "object_type_conflicts_with_layer" in records["0000b001:00000001"].quality_flags
    assert records["0000b001:00000002"].original_category == "Kleingarten"
    assert "object_type_unmapped" in records["0000b001:00000002"].quality_flags


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda d: d["gruenanlagen-0000"]["features"][0].update(
                geometry={"type": "Point", "coordinates": [390000, 5820000]}
            ),
            "Unsupported WFS geometry type",
        ),
        (
            lambda d: d["gruenanlagen-0000"]["features"][0]["properties"].pop("plannr"),
            "missing expected properties",
        ),
        (lambda d: d["gruenanlagen-0001"].update(features=[]), "count mismatch"),
        (
            lambda d: d["spielplaetze-0000"]["features"][0]["properties"].update(objartname=" "),
            "object type is missing",
        ),
        (
            lambda d: d["spielplaetze-0000"]["features"][0]["properties"].update(nettospfl="1"),
            "must be a number",
        ),
        (
            lambda d: d["gruenanlagen-0000"]["features"][0]["properties"].update(plannr="1"),
            "area code is invalid",
        ),
    ],
)
def test_rejects_invalid_features(tmp_path, mutate, message):
    with pytest.raises(SchemaError, match=message):
        BerlinGreenSpacesConnector().normalize(pages(tmp_path, mutate), configuration())


def test_rejects_assets_that_are_not_reviewed_layer_pages(tmp_path):
    bundle = pages(tmp_path)
    single = RawBundle(
        assets=(
            RawAsset(
                path=bundle.assets[0].path,
                sha256=bundle.assets[0].sha256,
                size_bytes=bundle.assets[0].size_bytes,
                resource_url="file:///local.geojson",
            ),
        ),
        retrieved_at=bundle.retrieved_at,
        snapshot_id=bundle.snapshot_id,
        request_fingerprint="local",
    )
    with pytest.raises(SchemaError, match="single-file imports are not supported"):
        BerlinGreenSpacesConnector().normalize(single, configuration())


def test_validation_requires_both_feature_types(tmp_path):
    connector = BerlinGreenSpacesConnector()
    batch = connector.normalize(pages(tmp_path), configuration())
    parks_only = type(batch)(
        records=tuple(r for r in batch.records if r.feature_type == "green_space"),
        input_rows=3,
    )
    assert "lacks a feature type" in " ".join(connector.validate(parks_only).errors)


def test_green_space_record_rejects_points_as_source_geometry(tmp_path):
    record = BerlinGreenSpacesConnector().normalize(pages(tmp_path), configuration()).records[0]
    values = record_to_dict(record)
    values["geometry"] = record.display_point
    with pytest.raises(ValidationError, match="Polygon or MultiPolygon"):
        record_from_dict(values)


def wfs_handler(responses=None):
    requested = []

    async def handler(request):
        requested.append(str(request.url))
        layer = request.url.params["TYPENAMES"].split(":")[1]
        start = int(request.url.params["STARTINDEX"])
        body = (responses or {}).get(layer)
        content = body or (FIXTURES / f"{layer}-{start // 2:04d}.json").read_bytes()
        return httpx.Response(200, content=content, request=request)

    return handler, requested


async def test_fetches_every_reviewed_layer_with_paging(tmp_path):
    handler, requested = wfs_handler()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        raw = await BerlinGreenSpacesConnector(HTTPFetcher(client)).fetch(
            FetchRequest(config=configuration(), staging_dir=str(tmp_path), snapshot_id="s1")
        )
    assert len(raw.assets) == 3 and len(requested) == 3
    assert [Path(asset.path).name for asset in raw.assets] == [
        "green_space-page-0000.json",
        "green_space-page-0001.json",
        "playground-page-0000.json",
    ]
    assert all(Path(asset.path).parent == tmp_path for asset in raw.assets)
    batch = BerlinGreenSpacesConnector().normalize(raw, configuration())
    assert len(batch.records) == 5


async def test_fetch_treats_http_200_exception_report_as_error(tmp_path):
    report = (BACKEND / "tests/fixtures/berlin_wfs/exception_report.xml").read_bytes()
    handler, _ = wfs_handler({"spielplaetze": report})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SchemaError):
            await BerlinGreenSpacesConnector(HTTPFetcher(client)).fetch(
                FetchRequest(config=configuration(), staging_dir=str(tmp_path), snapshot_id="s1")
            )


def test_check_parent_reports_codes_without_reassigning(tmp_path):
    from app.connectors.lor import LORConnector

    lor_values = json.loads((BACKEND / "config/datasets/berlin_lor.json").read_text())
    lor_values.update(source_crs="EPSG:4326", allowed_hosts=("example.org",))
    lor_values["parser_options"].update(expected_rows=2, name_field="PLR_NAME")
    lor_config = DatasetConfig(**lor_values)
    source = BACKEND / "tests/fixtures/lor/areas.geojson"
    lor_raw = RawBundle(
        assets=(
            RawAsset(
                path=str(source),
                sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                size_bytes=source.stat().st_size,
                resource_url="https://example.org/lor",
            ),
        ),
        retrieved_at=datetime.now(timezone.utc),
        snapshot_id="lor",
        request_fingerprint="lor",
    )
    areas = LORConnector().normalize(lor_raw, lor_config).records
    connector = BerlinGreenSpacesConnector()
    records = connector.normalize(pages(tmp_path), configuration()).records
    report = connector.check_parent(configuration(), records, areas)
    assert report["feature_counts"] == {"green_space": 3, "playground": 2}
    assert report["area_code_missing_count"] == 1
    assert report["area_code_unmatched_codes"] == ["09999999"]
    assert report["display_point_outside_supplied_area_count"] == 0
    assert by_id_area(records) == {"0000b001:00000002": "09999999"}


def by_id_area(records):
    return {r.source_record_id: r.area_code for r in records if r.area_code == "09999999"}


def test_green_space_type_is_registered():
    from app.domain import RECORD_TYPES

    assert RECORD_TYPES["green_space"] is GreenSpace
