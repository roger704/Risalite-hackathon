"""Offline contracts for the bounded Berlin ALKIS parcel layer."""

import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from shapely.geometry import Polygon, mapping

from app.connectors.berlin_alkis_parcels import BerlinALKISParcelsConnector
from app.connectors.berlin_wfs import page_url
from app.domain import (
    ConfigError,
    DatasetConfig,
    FetchRequest,
    Parcel,
    RawAsset,
    RawBundle,
    SchemaError,
    record_from_dict,
    record_to_dict,
)
from app.geo.parcels import ParcelIndex, summarize_by_related
from app.infrastructure.http import HTTPFetcher

BACKEND = Path(__file__).parents[2]
FIXTURES = BACKEND / "tests/fixtures/berlin_alkis_parcels"
CONFIG_PATH = BACKEND / "config/datasets/berlin_alkis_parcels.json"


def configuration(**changes):
    values = json.loads(CONFIG_PATH.read_text())
    values["allowed_hosts"] = tuple(values["allowed_hosts"])
    options = values["parser_options"]
    options.update(page_size=6, max_pages=10, expected_count_range=[12, 12])
    options.update(changes.pop("parser_options", {}))
    values.update(changes)
    return DatasetConfig(**values)


def page_paths(tmp_path, mutations=None, number_matched=12):
    paths = []
    for index, start in enumerate((0, 6)):
        data = json.loads((FIXTURES / f"page-{index:04d}.json").read_text())
        data["numberMatched"] = number_matched
        if mutations and index in mutations:
            mutations[index](data)
        path = tmp_path / f"page-{index:04d}.json"
        path.write_text(json.dumps(data))
        paths.append(path)
    return paths


def bundle(paths, config=None):
    config = config or configuration()
    return RawBundle(
        assets=tuple(
            RawAsset(
                path=str(path),
                sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                size_bytes=path.stat().st_size,
                resource_url=page_url(config.resource_url, index * 6, 6),
            )
            for index, path in enumerate(paths)
        ),
        retrieved_at=datetime.now(timezone.utc),
        snapshot_id="alkis-contract-snapshot",
        request_fingerprint="alkis-contract",
    )


def normalize(tmp_path, mutations=None, number_matched=12, config=None):
    current = config or configuration()
    return BerlinALKISParcelsConnector().normalize(
        bundle(page_paths(tmp_path, mutations, number_matched), current),
        current,
    )


def test_normalizes_pages_preserving_strings_areas_dates_and_warnings(tmp_path):
    connector = BerlinALKISParcelsConnector()
    batch = normalize(tmp_path)
    first = batch.records[0]

    assert len(batch.records) == 12
    assert first.source_record_id == "DEBE00YY11p000zH"
    assert first.record_id == "flurstueck:DEBE00YY11p000zH"
    assert first.parcel_id == "11000102100383____"
    assert first.cadastral_district_code == "0001"
    assert first.parcel_numerator == "383"
    assert first.parcel_denominator is None
    assert first.geometry["type"] == "MultiPolygon"
    assert first.official_area_m2 == 1065.0
    assert first.geometry_area_m2 != first.official_area_m2
    assert first.created_date is None
    assert first.lifespan_start == "2025-01-13"
    assert connector.validate(batch).valid
    assert any("11,434 parcels" in warning for warning in batch.warnings)


@pytest.mark.parametrize(
    "value,flag",
    [
        (None, "official_area_missing"),
        ("", "official_area_missing"),
        (0, "official_area_zero"),
        (-1, "official_area_invalid:-1"),
        ("unknown", "official_area_invalid:'unknown'"),
    ],
)
def test_missing_or_invalid_official_area_never_replaces_geometry_area(tmp_path, value, flag):
    batch = normalize(
        tmp_path,
        {0: lambda data: data["features"][0]["properties"].update(afl=value)},
    )
    first = batch.records[0]
    assert first.official_area_m2 is None
    assert first.geometry_area_m2 > 0
    assert flag in first.quality_flags


def test_rejects_duplicate_uuid_and_fsko(tmp_path):
    with pytest.raises(SchemaError, match="Duplicate ALKIS uuid"):
        normalize(
            tmp_path,
            {1: lambda data: data["features"][0]["properties"].update(uuid="DEBE00YY11p000zH")},
        )
    with pytest.raises(SchemaError, match="Duplicate ALKIS fsko"):
        normalize(
            tmp_path,
            {1: lambda data: data["features"][0]["properties"].update(fsko="11000102100383____")},
        )


@pytest.mark.parametrize(
    "mutation,match",
    [
        (
            lambda data: data["crs"]["properties"].update(name="EPSG:4326"),
            "CRS differs",
        ),
        (
            lambda data: data["features"][0].update(
                geometry={"type": "Point", "coordinates": [390000, 5820000]}
            ),
            "Invalid ALKIS polygon",
        ),
        (
            lambda data: data["features"][0].update(
                geometry={"type": "Polygon", "coordinates": []}
            ),
            "Invalid ALKIS polygon",
        ),
        (
            lambda data: data["features"][0].update(
                geometry=mapping(
                    Polygon(
                        [
                            (390000, 5820000),
                            (390100, 5820100),
                            (390100, 5820000),
                            (390000, 5820100),
                            (390000, 5820000),
                        ]
                    )
                )
            ),
            "Invalid ALKIS polygon",
        ),
    ],
)
def test_rejects_crs_and_invalid_geometries(tmp_path, mutation, match):
    with pytest.raises(SchemaError, match=match):
        normalize(tmp_path, {0: mutation})


def test_rejects_exception_report_even_with_success_status(tmp_path):
    paths = page_paths(tmp_path)
    paths[0].write_text(
        '<ows:ExceptionReport xmlns:ows="http://www.opengis.net/ows/1.1">'
        '<ows:Exception exceptionCode="InvalidParameterValue">'
        "<ows:ExceptionText>bad request</ows:ExceptionText></ows:Exception>"
        "</ows:ExceptionReport>"
    )
    raw = bundle(paths)
    with pytest.raises(SchemaError, match="ExceptionReport"):
        BerlinALKISParcelsConnector().normalize(raw, configuration())


def test_rejects_incomplete_number_matched_pages(tmp_path):
    with pytest.raises(SchemaError, match="numberMatched 13, received 12"):
        normalize(tmp_path, number_matched=13)


def test_local_file_mode_and_mismatched_extent_are_rejected(tmp_path):
    paths = page_paths(tmp_path)
    raw = bundle(paths)
    local_asset = replace(raw.assets[0], resource_url=configuration().resource_url)
    with pytest.raises(SchemaError, match="does not support --file"):
        BerlinALKISParcelsConnector().normalize(
            replace(raw, assets=(local_asset,)), configuration()
        )
    with pytest.raises(ConfigError, match="must match"):
        _ = BerlinALKISParcelsConnector().normalize(
            raw,
            configuration(parser_options={"extent_bbox": [387501, 5816500, 392500, 5821500]}),
        )


def test_fetch_reuses_safe_wfs_paging_and_checks_next_link(tmp_path):
    requests = []

    async def handler(request):
        requests.append(request)
        start = int(request.url.params["STARTINDEX"])
        response = FIXTURES / f"page-{start // 6:04d}.json"
        data = json.loads(response.read_text())
        if start == 0:
            data["links"][0]["href"] = page_url(configuration().resource_url, 6, 6)
        return httpx.Response(200, json=data, request=request)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            connector = BerlinALKISParcelsConnector(HTTPFetcher(client))
            return await connector.fetch(
                FetchRequest(
                    config=configuration(),
                    staging_dir=str(tmp_path / "staging"),
                    snapshot_id="fetch-test",
                )
            )

    raw = asyncio.run(run())
    assert len(raw.assets) == 2
    assert [request.url.params["STARTINDEX"] for request in requests] == ["0", "6"]
    assert [request.url.params["COUNT"] for request in requests] == ["6", "6"]

    async def bad_handler(request):
        data = json.loads((FIXTURES / "page-0000.json").read_text())
        data["links"][0]["href"] = page_url(
            configuration().resource_url.replace("SORTBY=uuid", "SORTBY=not_uuid"), 6, 6
        )
        return httpx.Response(200, json=data, request=request)

    async def bad_run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(bad_handler)) as client:
            await BerlinALKISParcelsConnector(HTTPFetcher(client)).fetch(
                FetchRequest(
                    config=configuration(),
                    staging_dir=str(tmp_path / "bad-staging"),
                    snapshot_id="bad-fetch",
                )
            )

    with pytest.raises(SchemaError, match="changed reviewed query parameters"):
        asyncio.run(bad_run())


def test_parcel_record_round_trip_preserves_record_type(tmp_path):
    record = normalize(tmp_path).records[0]
    restored = record_from_dict(record_to_dict(record))
    assert isinstance(restored, Parcel)
    assert record_to_dict(restored) == record_to_dict(record)


def _parcel(parcel_id, west, east):
    return Parcel(
        source_id="berlin_alkis_parcels",
        snapshot_id="parcel-index",
        source_record_id=parcel_id,
        record_id=f"flurstueck:{parcel_id}",
        geometry=mapping(
            Polygon(
                [
                    (west, 52.49),
                    (east, 52.49),
                    (east, 52.51),
                    (west, 52.51),
                    (west, 52.49),
                ]
            )
        ),
        parcel_id=parcel_id,
        cadastral_district_code="0001",
        parcel_numerator="1",
        geometry_area_m2=100,
    )


def test_parcel_index_boundary_bbox_and_many_to_many_relations():
    first = _parcel("parcel-a", 13.40, 13.41)
    second = _parcel("parcel-b", 13.41, 13.42)
    index = ParcelIndex((second, first))

    interior = index.at_point(13.405, 52.5)
    assert [(match.parcel.parcel_id, match.match_type) for match in interior] == [
        ("parcel-a", "interior")
    ]
    boundary = index.at_point(13.41, 52.5)
    assert [(match.parcel.parcel_id, match.match_type) for match in boundary] == [
        ("parcel-a", "boundary"),
        ("parcel-b", "boundary"),
    ]
    assert index.at_point(13.5, 52.5) == []
    assert index.get("parcel-a") == first
    assert index.get_record(first.record_id) == first
    assert len(index.in_bbox(13.4, 52.49, 13.42, 52.51, limit=10)) == 2
    with pytest.raises(ValueError, match="0.05"):
        index.in_bbox(13.0, 52.3, 13.5, 52.8, limit=10)

    building = type(
        "FixtureRecord",
        (),
        {
            "record_id": "building-1",
            "geometry": mapping(
                Polygon(
                    [
                        (13.405, 52.495),
                        (13.415, 52.495),
                        (13.415, 52.505),
                        (13.405, 52.505),
                        (13.405, 52.495),
                    ]
                )
            ),
        },
    )()
    address = type(
        "FixtureRecord",
        (),
        {"record_id": "address-1", "geometry": {"type": "Point", "coordinates": [13.405, 52.5]}},
    )()
    touches = type(
        "FixtureRecord",
        (),
        {
            "record_id": "touch-only",
            "geometry": mapping(
                Polygon(
                    [
                        (13.42, 52.49),
                        (13.43, 52.49),
                        (13.43, 52.51),
                        (13.42, 52.51),
                        (13.42, 52.49),
                    ]
                )
            ),
        },
    )()
    relations, touches_only = index.relate_geometries(
        "fixture_buildings", "building-snapshot", (building, address, touches)
    )
    building_relations = [
        relation for relation in relations if relation.related_record_id == "building-1"
    ]
    assert len(building_relations) == 2
    assert all(relation.relation == "intersects" for relation in building_relations)
    assert all(relation.crs == "EPSG:25833" for relation in building_relations)
    assert sum(relation.related_area_share for relation in building_relations) == pytest.approx(1)
    assert any(relation.related_record_id == "address-1" for relation in relations)
    assert "touch-only" not in {relation.related_record_id for relation in relations}
    assert touches_only == 1
    assert summarize_by_related(relations)["building-1"] == {
        "parcel_ids": ["parcel-a", "parcel-b"],
        "multiple_parcels": True,
    }
