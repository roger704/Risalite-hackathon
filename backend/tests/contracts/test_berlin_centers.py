"""Recorded WFS contracts for Berlin StEP Zentren 2040."""

import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from pyproj import Transformer
from shapely.geometry import shape

from app.connectors.berlin_centers import BerlinCentersConnector
from app.domain import (
    Area,
    DatasetConfig,
    FetchRequest,
    IncompatibleSnapshotError,
    NormalizedBatch,
    RawAsset,
    RawBundle,
    RetailCenter,
    SchemaError,
    ValidationError,
)
from app.infrastructure.http import HTTPFetcher

BACKEND = Path(__file__).parents[2]
FIXTURES = BACKEND / "tests/fixtures/berlin_centers"
CONFIG_PATH = BACKEND / "config/datasets/berlin_centers.json"
_TO_WGS84 = Transformer.from_crs("EPSG:25833", "EPSG:4326", always_xy=True)
_TO_METRIC = Transformer.from_crs("EPSG:4326", "EPSG:25833", always_xy=True)


def configuration(**changes):
    values = json.loads(CONFIG_PATH.read_text())
    values["allowed_hosts"] = tuple(values["allowed_hosts"])
    options = values["parser_options"]
    options["page_size"] = 2
    options["layers"]["zh"]["expected_count_range"] = [3, 3]
    options["layers"]["fma"]["expected_count_range"] = [2, 2]
    options.update(changes.pop("parser_options", {}))
    values.update(changes)
    return DatasetConfig(**values)


def make_bundle(paths, *, snapshot_id="berlin-centers-test"):
    config = configuration()
    resources = {
        key: layer["resource_url"] for key, layer in config.parser_options["layers"].items()
    }
    assets = []
    for path in paths:
        key = Path(path).name.split("-", 1)[0]
        assets.append(
            RawAsset(
                path=str(path),
                sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                size_bytes=Path(path).stat().st_size,
                resource_url=resources[key],
            )
        )
    return RawBundle(
        assets=tuple(assets),
        retrieved_at=datetime.now(timezone.utc),
        snapshot_id=snapshot_id,
        request_fingerprint="fixture",
    )


def fixture_paths():
    return [
        FIXTURES / name
        for name in (
            "zh-page-0000.json",
            "zh-page-0001.json",
            "fma-page-0000.json",
        )
    ]


def edit_fixtures(tmp_path, mutations):
    paths = []
    for source in fixture_paths():
        data = json.loads(source.read_text())
        for mutation in mutations.get(source.name, ()):
            mutation(data)
        path = tmp_path / source.name
        path.write_text(json.dumps(data, ensure_ascii=False))
        paths.append(path)
    return paths


def normalize(paths=None, config=None):
    return BerlinCentersConnector().normalize(
        make_bundle(paths or fixture_paths()), config or configuration()
    )


def test_normalizes_layers_types_hierarchy_parent_source_and_limitations():
    connector = BerlinCentersConnector()
    config = configuration()
    batch = connector.normalize(make_bundle(fixture_paths()), config)
    by_id = {record.record_id: record for record in batch.records}

    assert set(by_id) == {"Z4001", "Z4058", "Z40F1", "F4001", "F4002"}
    assert by_id["Z4001"].geometry["type"] == "Point"
    assert by_id["Z40F1"].geometry["type"] == "MultiPolygon"
    assert by_id["F4001"].geometry["type"] == "Point"
    assert by_id["Z4001"].center_type == "Zentrumsbereichskern"
    assert by_id["Z4001"].center_category == "zentrumsbereichskern"
    assert by_id["Z4001"].center_role == "center"
    assert by_id["Z4001"].hierarchy_level == 1
    assert by_id["Z4058"].center_category == "ortsteilzentrum"
    assert by_id["Z4058"].hierarchy_level == 4
    assert by_id["Z4001"].parent_center_id == "Z40F1"
    assert by_id["F4001"].center_role == "specialist_retail_site"
    assert by_id["F4001"].hierarchy_level is None
    assert by_id["Z4001"].source_url
    assert parse_qs(urlsplit(by_id["Z4001"].source_url).query)["RESOURCEID"] == [
        "step_zen_2040_zh.Z4001"
    ]
    assert by_id["Z4001"].wfs_feature_id == "step_zen_2040_zh.Z4001"
    for record in batch.records:
        bounds = shape(record.geometry).bounds
        assert 13.0 <= bounds[0] <= bounds[2] <= 13.9
        assert 52.3 <= bounds[1] <= bounds[3] <= 52.8
    assert all(
        any(warning in item for item in batch.warnings)
        for warning in (
            "planning designation",
            "Boundaries are published",
            "not walking distance",
        )
    )
    quality = connector.validate(batch)
    assert quality.valid
    assert quality.input_rows == quality.output_rows == 5


def test_fetch_pages_both_layers_and_keeps_raw_asset_names_distinct(tmp_path):
    requests = []

    async def handler(request):
        requests.append(request)
        typename = request.url.params["TYPENAMES"]
        start = int(request.url.params["STARTINDEX"])
        if typename.endswith("_zh"):
            path = FIXTURES / f"zh-page-{start // 2:04d}.json"
        else:
            path = FIXTURES / "fma-page-0000.json"
        return httpx.Response(200, content=path.read_bytes(), request=request)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            connector = BerlinCentersConnector(fetcher=HTTPFetcher(client))
            return await connector.fetch(
                FetchRequest(
                    config=configuration(),
                    staging_dir=str(tmp_path / "staging"),
                    snapshot_id="fetch-test",
                )
            )

    bundle = asyncio.run(run())
    assert len(bundle.assets) == 3
    assert len(requests) == 3
    assert [request.url.params["TYPENAMES"].split(":")[-1] for request in requests] == [
        "step_zen_2040_zh",
        "step_zen_2040_zh",
        "step_zen_2040_fma",
    ]
    assert {Path(asset.path).name for asset in bundle.assets} == {
        "zh-page-0000.json",
        "zh-page-0001.json",
        "fma-page-0000.json",
    }


def test_warns_on_extra_properties(tmp_path):
    paths = edit_fixtures(
        tmp_path,
        {"zh-page-0000.json": [lambda data: data["features"][0]["properties"].update(extra="x")]},
    )
    batch = normalize(paths)
    assert any("extra properties: extra" in warning for warning in batch.warnings)


def test_unmapped_type_and_null_name_remain_explicit(tmp_path):
    paths = edit_fixtures(
        tmp_path,
        {
            "zh-page-0000.json": [
                lambda data: data["features"][0]["properties"].update(typ="FutureType"),
                lambda data: data["features"][1]["properties"].update(name=None),
            ]
        },
    )
    batch = normalize(paths)
    by_id = {record.record_id: record for record in batch.records}
    assert by_id["Z4001"].center_category is None
    assert by_id["Z4001"].center_role is None
    assert "center_type_unmapped" in by_id["Z4001"].quality_flags
    assert by_id["Z4058"].name is None
    assert "name_missing" in by_id["Z4058"].quality_flags
    assert any("FutureType" in warning for warning in batch.warnings)


@pytest.mark.parametrize(
    "mutations,pattern",
    [
        (
            {"zh-page-0000.json": [lambda data: data["features"][0]["properties"].pop("typ")]},
            "missing expected properties",
        ),
        (
            {"zh-page-0000.json": [lambda data: data["features"][0].update(geometry=None)]},
            "geometry",
        ),
        (
            {
                "fma-page-0000.json": [
                    lambda data: data["features"][0].update(
                        geometry={
                            "type": "Polygon",
                            "coordinates": [
                                [
                                    [389000, 5814000],
                                    [389100, 5814000],
                                    [389100, 5814100],
                                    [389000, 5814000],
                                ]
                            ],
                        }
                    )
                ]
            },
            "Unsupported WFS geometry",
        ),
        (
            {
                "zh-page-0001.json": [
                    lambda data: data["features"][0].update(
                        geometry={
                            "type": "Polygon",
                            "coordinates": [
                                [
                                    [390000, 5820000],
                                    [391000, 5821000],
                                    [390000, 5821000],
                                    [391000, 5820000],
                                    [390000, 5820000],
                                ]
                            ],
                        }
                    )
                ]
            },
            "Invalid WFS geometry in EPSG:25833",
        ),
        (
            {
                "zh-page-0000.json": [
                    lambda data: data["features"][0]["geometry"].update(
                        coordinates=[500000, 6000000]
                    )
                ]
            },
            "source_bbox",
        ),
    ],
)
def test_rejects_schema_and_invalid_geometry(mutations, pattern, tmp_path):
    with pytest.raises(SchemaError, match=pattern):
        normalize(edit_fixtures(tmp_path, mutations))


def test_rejects_duplicate_ids_within_a_layer(tmp_path):
    paths = edit_fixtures(
        tmp_path,
        {
            "zh-page-0000.json": [
                lambda data: data["features"][1]["properties"].update(id="Z4001"),
                lambda data: data["features"][1].update(id="step_zen_2040_zh.Z4001"),
            ]
        },
    )
    with pytest.raises(SchemaError, match="Duplicate Berlin centers WFS ID"):
        normalize(paths)


def test_rejects_duplicate_ids_across_layers(tmp_path):
    paths = edit_fixtures(
        tmp_path,
        {
            "fma-page-0000.json": [
                lambda data: data["features"][0]["properties"].update(id="Z4001"),
                lambda data: data["features"][0].update(id="step_zen_2040_fma.Z4001"),
            ]
        },
    )
    config = configuration(
        parser_options={
            "layers": {
                "zh": configuration().parser_options["layers"]["zh"],
                "fma": {
                    **configuration().parser_options["layers"]["fma"],
                    "id_pattern": "^Z4001$",
                },
            }
        }
    )
    with pytest.raises(SchemaError, match="Duplicate Berlin centers WFS ID"):
        normalize(paths, config)


def test_rejects_feature_id_mismatch(tmp_path):
    paths = edit_fixtures(
        tmp_path,
        {"zh-page-0000.json": [lambda data: data["features"][0].update(id="other.Z4001")]},
    )
    with pytest.raises(SchemaError, match="feature id mismatch"):
        normalize(paths)


def test_rejects_incomplete_layers_counts_and_ogc_exception(tmp_path):
    with pytest.raises(SchemaError, match="missing layer fma"):
        normalize(fixture_paths()[:2])
    paths = edit_fixtures(
        tmp_path,
        {
            "zh-page-0000.json": [lambda data: data.update(numberMatched=4)],
            "zh-page-0001.json": [lambda data: data.update(numberMatched=4)],
        },
    )
    with pytest.raises(SchemaError, match="count mismatch"):
        normalize(paths)

    config = configuration(
        parser_options={
            "layers": {
                "zh": {
                    **configuration().parser_options["layers"]["zh"],
                    "expected_count_range": [4, 5],
                },
                "fma": configuration().parser_options["layers"]["fma"],
            }
        }
    )
    with pytest.raises(SchemaError, match="expected zh feature count"):
        normalize(config=config)


def test_fetch_rejects_missing_next_link_and_http_200_exception_report(tmp_path):
    requests = []
    exception = b'<ExceptionReport><Exception exceptionCode="InvalidParameterValue"><ExceptionText>bad request</ExceptionText></Exception></ExceptionReport>'

    async def handler(request):
        requests.append(request)
        if request.url.params["TYPENAMES"].endswith("_zh"):
            if len(requests) == 1:
                data = json.loads((FIXTURES / "zh-page-0000.json").read_text())
                data.pop("links")
                return httpx.Response(200, json=data, request=request)
            return httpx.Response(200, content=exception, request=request)
        return httpx.Response(
            200,
            content=(FIXTURES / "fma-page-0000.json").read_bytes(),
            request=request,
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            from app.infrastructure.http import HTTPFetcher

            connector = BerlinCentersConnector(HTTPFetcher(client))
            return await connector.fetch(
                FetchRequest(
                    config=configuration(),
                    staging_dir=str(tmp_path / "staging"),
                    snapshot_id="incomplete",
                )
            )

    with pytest.raises(SchemaError, match="omitted the next link"):
        asyncio.run(run())

    async def exception_handler(request):
        return httpx.Response(200, content=exception, request=request)

    async def exception_run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(exception_handler)) as client:
            from app.infrastructure.http import HTTPFetcher

            connector = BerlinCentersConnector(HTTPFetcher(client))
            return await connector.fetch(
                FetchRequest(
                    config=configuration(),
                    staging_dir=str(tmp_path / "exception"),
                    snapshot_id="exception",
                )
            )

    with pytest.raises(SchemaError, match="ExceptionReport"):
        asyncio.run(exception_run())


def test_validate_checks_record_type_duplicate_ids_and_layer_presence():
    connector = BerlinCentersConnector()
    batch = normalize()
    assert connector.validate(batch).valid
    assert not connector.validate(NormalizedBatch(records=(), input_rows=0)).valid

    first = batch.records[0]
    duplicate = replace(batch, records=batch.records + (first,), input_rows=6)
    assert any(
        "Duplicate Berlin centers record ID" in error
        for error in connector.validate(duplicate).errors
    )

    one_layer = tuple(record for record in batch.records if record.layer.endswith("_zh"))
    missing_layer = NormalizedBatch(records=one_layer, input_rows=len(one_layer))
    assert any("missing a layer" in error for error in connector.validate(missing_layer).errors)

    wrong_type = NormalizedBatch(
        records=(
            Area(
                source_id="x",
                snapshot_id="s",
                source_record_id="1",
                record_id="1",
                geometry={
                    "type": "Polygon",
                    "coordinates": [[[13, 52.4], [13.1, 52.4], [13, 52.4]]],
                },
                area_code="1",
                name="x",
                hierarchy="planungsraum",
                boundary_edition="2021",
            ),
        ),
        input_rows=1,
    )
    assert any("non-RetailCenter" in error for error in connector.validate(wrong_type).errors)


def _parent_area(snapshot_id="lor-parent", bounds=(13.0, 52.3, 13.9, 52.8)):
    west, south, east, north = bounds
    return Area(
        source_id="berlin_lor",
        snapshot_id=snapshot_id,
        source_record_id="01011101",
        record_id="planungsraum:01011101",
        geometry={
            "type": "Polygon",
            "coordinates": [
                [
                    [west, south],
                    [east, south],
                    [east, north],
                    [west, north],
                    [west, south],
                ]
            ],
        },
        area_code="01011101",
        name="Synthetic PLR",
        hierarchy="planungsraum",
        boundary_edition="2021",
    )


def test_check_parent_returns_layer_geometry_coverage_and_fails_outside():
    connector = BerlinCentersConnector()
    records = normalize().records
    coverage = connector.check_parent(configuration(), records, (_parent_area(),))
    assert coverage == {
        "parent_dataset": "berlin_lor",
        "centers_located_in_parent": 5,
        "layer_counts": {
            "step_zen_2040:step_zen_2040_fma": 2,
            "step_zen_2040:step_zen_2040_zh": 3,
        },
        "geometry_type_counts": {"MultiPolygon": 1, "Point": 4},
    }
    outside = _parent_area(bounds=(13.0, 52.3, 13.01, 52.31))
    with pytest.raises(IncompatibleSnapshotError, match="outside the parent"):
        connector.check_parent(configuration(), records, (outside,))


def _point_center(record_id, lon, lat):
    return RetailCenter(
        source_id="berlin_centers",
        snapshot_id="centers",
        source_record_id=record_id,
        record_id=record_id,
        geometry={"type": "Point", "coordinates": [lon, lat]},
        layer="step_zen_2040:step_zen_2040_zh",
        wfs_feature_id=f"step_zen_2040_zh.{record_id}",
        center_type="Ortsteilzentrum",
        center_category="ortsteilzentrum",
        center_role="center",
        hierarchy_level=4,
        name=record_id,
        parent_center_id=None,
        category_mapping_version="step_zentren_2040_v1",
        source_url="https://gdi.berlin.de/source",
    )


def test_center_relationships_cover_inside_distance_radius_and_sort():
    from app.geo.centers import center_relationships

    batch = normalize()
    by_id = {record.record_id: record for record in batch.records}
    point = shape(by_id["Z4001"].geometry)
    inside = center_relationships([by_id["Z40F1"]], point.x, point.y, 100)
    assert inside[0]["relation"] == "inside_boundary"
    assert inside[0]["distance_m"] == 0.0
    assert inside[0]["distance_basis"] == "nearest_published_boundary"

    origin = _TO_METRIC.transform(13.4, 52.5)
    east_500 = _TO_WGS84.transform(origin[0] + 500, origin[1])
    distance = center_relationships([_point_center("p", *origin_to_lonlat(origin))], *east_500, 501)
    assert distance[0]["distance_m"] == pytest.approx(500.0, abs=0.5)
    assert distance[0]["relation"] == "point_only_no_boundary"

    nearby = [
        _point_center("z-far", *_TO_WGS84.transform(origin[0] + 300, origin[1])),
        _point_center("a-near", *_TO_WGS84.transform(origin[0] + 100, origin[1])),
    ]
    assert [item["record_id"] for item in center_relationships(nearby, 13.4, 52.5, 150)] == [
        "a-near"
    ]
    assert [item["record_id"] for item in center_relationships(nearby, 13.4, 52.5, 500)] == [
        "a-near",
        "z-far",
    ]


def origin_to_lonlat(origin):
    return _TO_WGS84.transform(*origin)


@pytest.mark.parametrize("radius", [0, -1, 5000.1, float("nan")])
def test_center_relationships_reject_bad_radius(radius):
    from app.geo.centers import center_relationships

    with pytest.raises(ValueError):
        center_relationships([], 13.4, 52.5, radius)


def test_center_relationships_reject_non_finite_coordinates():
    from app.geo.centers import center_relationships

    with pytest.raises(ValueError):
        center_relationships([], float("inf"), 52.5, 100)


def test_retail_center_rejects_invalid_wgs84_coordinates_and_boolean_levels():
    point = _point_center("invalid", 13.4, 52.5)
    with pytest.raises(ValidationError, match="finite WGS84"):
        replace(point, geometry={"type": "Point", "coordinates": [200, 52.5]})
    with pytest.raises(ValidationError, match="finite WGS84"):
        replace(point, geometry={"type": "Point", "coordinates": [13.4, True]})
    with pytest.raises(ValidationError, match="hierarchy_level"):
        replace(point, hierarchy_level=True)
