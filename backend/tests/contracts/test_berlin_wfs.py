"""Offline WFS paging and schema checks using synthetic Berlin geometry."""

import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from shapely.affinity import translate
from shapely.geometry import mapping, shape

from app.connectors.berlin_wfs import BerlinWFSConnector, page_url, parse_page
from app.domain import (
    DatasetConfig,
    FetchRequest,
    IncompatibleSnapshotError,
    RawAsset,
    RawBundle,
    SchemaError,
)
from app.infrastructure.http import HTTPFetcher

FIXTURES = Path(__file__).parents[1] / "fixtures/berlin_wfs"
CONFIG = Path(__file__).parents[2] / "config/datasets/berlin_lor_names.json"


def configuration(**changes):
    values = json.loads(CONFIG.read_text())
    values["allowed_hosts"] = tuple(values["allowed_hosts"])
    options = values["parser_options"]
    options.update(page_size=2, max_pages=10, expected_count=3)
    options.update(changes.pop("parser_options", {}))
    values.update(changes)
    return DatasetConfig(**values)


def make_bundle(paths):
    return RawBundle(
        assets=tuple(
            RawAsset(
                path=str(path),
                sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                size_bytes=path.stat().st_size,
                resource_url=f"https://gdi.berlin.de/synthetic/{path.name}",
            )
            for path in paths
        ),
        retrieved_at=datetime.now(timezone.utc),
        snapshot_id="synthetic-wfs",
        request_fingerprint="synthetic",
    )


def edited_pages(tmp_path, mutate_first=None, mutate_second=None):
    paths = []
    for index, mutate in enumerate((mutate_first, mutate_second)):
        data = json.loads((FIXTURES / f"page-{index:04d}.json").read_text())
        if mutate:
            mutate(data)
        path = tmp_path / f"edited-page-{index:04d}.json"
        path.write_text(json.dumps(data))
        paths.append(path)
    return paths


def test_normalizes_pages_and_preserves_names_attributes_and_geometry_types(tmp_path):
    paths = edited_pages(
        tmp_path,
        mutate_second=lambda data: data["features"][0]["properties"].update(bzr_name=None),
    )
    connector = BerlinWFSConnector()
    batch = connector.normalize(make_bundle(paths), configuration())

    assert len(batch.records) == 3
    first, second, third = batch.records
    assert [record.name for record in batch.records] == [
        "Stülerstraße",
        "Synthetic Platz",
        "Synthetic Park",
    ]
    assert first.geometry["type"] == "Polygon"
    assert second.geometry["type"] == "MultiPolygon"
    assert first.record_id == "planungsraum:01011101"
    assert first.source_record_id == "01011101"
    assert first.attributes["bzr_name"] == "Synthetic Süd"
    assert first.attributes["stand"] == "01.01.2021"
    assert third.attributes.get("bzr_name") is None
    assert third.quality_flags == ("attribute_missing:bzr_name",)
    for record in batch.records:
        point = shape(record.geometry).representative_point()
        longitude, latitude = point.x, point.y
        assert 13.0 < longitude < 13.9
        assert 52.3 < latitude < 52.8
    assert connector.validate(batch).valid


def test_fetch_pages_downloads_each_asset_and_hashes_response(tmp_path):
    requests = []

    async def handler(request):
        requests.append(request)
        start = int(request.url.params["STARTINDEX"])
        response = (FIXTURES / f"page-{start // 2:04d}.json").read_bytes()
        return httpx.Response(
            200,
            headers={"content-type": "application/json;charset=UTF-8"},
            content=response,
            request=request,
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            connector = BerlinWFSConnector(HTTPFetcher(client))
            config = configuration()
            bundle = await connector.fetch(
                FetchRequest(
                    config=config,
                    staging_dir=str(tmp_path / "staging"),
                    snapshot_id="synthetic-fetch",
                )
            )
        return bundle

    bundle = asyncio.run(run())
    assert len(bundle.assets) == 2
    assert [asset.sha256 for asset in bundle.assets] == [
        hashlib.sha256(Path(asset.path).read_bytes()).hexdigest() for asset in bundle.assets
    ]
    assert [request.url.params["COUNT"] for request in requests] == ["2", "2"]
    assert [request.url.params["STARTINDEX"] for request in requests] == ["0", "2"]
    assert page_url(configuration().resource_url, 2, 2) == str(requests[1].url)


def test_rejects_ogc_exception_and_html():
    with pytest.raises(SchemaError, match="InvalidParameterValue"):
        parse_page((FIXTURES / "exception_report.xml").read_bytes(), configuration().parser_options)
    with pytest.raises(SchemaError, match="HTML instead of GeoJSON"):
        parse_page((FIXTURES / "html_page.html").read_bytes(), configuration().parser_options)


def test_rejects_number_matched_count_mismatch(tmp_path):
    paths = edited_pages(
        tmp_path,
        mutate_first=lambda data: data.update(numberMatched=4),
        mutate_second=lambda data: data.update(numberMatched=4),
    )
    with pytest.raises(SchemaError, match="count mismatch: numberMatched 4, received 3"):
        BerlinWFSConnector().normalize(make_bundle(paths), configuration())


def test_rejects_duplicate_ids_across_pages(tmp_path):
    paths = edited_pages(
        tmp_path,
        mutate_second=lambda data: data["features"][0]["properties"].update(plr_id="01011101"),
    )
    with pytest.raises(SchemaError, match="Duplicate WFS ID: 01011101"):
        BerlinWFSConnector().normalize(make_bundle(paths), configuration())


@pytest.mark.parametrize(
    "link,match",
    [
        (None, "omitted the next link"),
        ("start", "STARTINDEX is inconsistent"),
        ("host", "URL is not allowed"),
    ],
)
def test_fetch_rejects_missing_or_unsafe_next_links(tmp_path, link, match):
    async def handler(request):
        data = json.loads((FIXTURES / "page-0000.json").read_text())
        if link is None:
            data.pop("links")
        elif link == "start":
            data["links"][0]["href"] = data["links"][0]["href"].replace(
                "STARTINDEX=2", "STARTINDEX=4"
            )
        else:
            data["links"][0]["href"] = data["links"][0]["href"].replace(
                "https://gdi.berlin.de", "https://attacker.example"
            )
        return httpx.Response(200, json=data, request=request)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            connector = BerlinWFSConnector(HTTPFetcher(client))
            await connector.fetch(
                FetchRequest(
                    config=configuration(),
                    staging_dir=str(tmp_path / "staging"),
                    snapshot_id="synthetic-fetch",
                )
            )

    with pytest.raises(SchemaError, match=match):
        asyncio.run(run())


def test_fetch_enforces_page_cap(tmp_path):
    async def handler(request):
        return httpx.Response(
            200,
            content=(FIXTURES / "page-0000.json").read_bytes(),
            request=request,
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            connector = BerlinWFSConnector(HTTPFetcher(client))
            await connector.fetch(
                FetchRequest(
                    config=configuration(parser_options={"max_pages": 1}),
                    staging_dir=str(tmp_path / "staging"),
                    snapshot_id="synthetic-fetch",
                )
            )

    with pytest.raises(SchemaError, match="page cap"):
        asyncio.run(run())


@pytest.mark.parametrize(
    "mutation,match",
    [
        (
            lambda data: data["crs"]["properties"].update(name="EPSG:4326"),
            "CRS differs",
        ),
        (
            lambda data: data["features"][0]["geometry"]["coordinates"][0].__setitem__(
                slice(None),
                [
                    [5818000, 390000],
                    [5818200, 390000],
                    [5818200, 390200],
                    [5818000, 390200],
                    [5818000, 390000],
                ],
            ),
            "source_bbox",
        ),
        (
            lambda data: data["features"][0]["properties"].pop("plr_name"),
            "missing expected properties",
        ),
        (
            lambda data: data["features"][0]["properties"].update(plr_id=1011101),
            "ID must be a nonempty string",
        ),
    ],
)
def test_rejects_invalid_crs_axes_schema_and_numeric_ids(tmp_path, mutation, match):
    paths = edited_pages(tmp_path, mutate_first=mutation)
    with pytest.raises(SchemaError, match=match):
        BerlinWFSConnector().normalize(make_bundle(paths), configuration())


def test_expected_count_mismatch_and_unsupported_record_type(tmp_path):
    with pytest.raises(SchemaError, match="expected 4 features"):
        BerlinWFSConnector().normalize(
            make_bundle(edited_pages(tmp_path)),
            configuration(parser_options={"expected_count": 4}),
        )
    with pytest.raises(SchemaError, match="unsupported record_type"):
        BerlinWFSConnector().normalize(
            make_bundle(edited_pages(tmp_path)),
            configuration(parser_options={"record_type": "place"}),
        )


def test_extra_properties_are_reported_as_a_warning(tmp_path):
    paths = edited_pages(
        tmp_path,
        mutate_first=lambda data: data["features"][0]["properties"].update(reviewed_later="extra"),
    )
    batch = BerlinWFSConnector().normalize(make_bundle(paths), configuration())
    assert any("reviewed_later" in warning for warning in batch.warnings)
    assert BerlinWFSConnector().validate(batch).valid


def test_attribute_fields_must_be_strings(tmp_path):
    with pytest.raises(SchemaError, match="attribute finhalt must be a string or null"):
        BerlinWFSConnector().normalize(
            make_bundle(edited_pages(tmp_path)),
            configuration(parser_options={"attribute_fields": ["finhalt"]}),
        )


def test_check_parent_reports_geometry_differences_and_rejects_mismatches(tmp_path):
    batch = BerlinWFSConnector().normalize(make_bundle(edited_pages(tmp_path)), configuration())
    parent_records = list(batch.records)
    parent_records[1] = replace(
        parent_records[1],
        geometry=mapping(translate(shape(parent_records[1].geometry), xoff=0.001)),
    )
    coverage = BerlinWFSConnector().check_parent(
        configuration(), batch.records, tuple(parent_records)
    )
    assert coverage["parent_code_match"] == "exact"
    assert coverage["parent_area_count"] == 3
    assert coverage["geometry_comparison"]["areas_over_threshold"] == 1
    assert coverage["geometry_comparison"]["top_differences"][0]["area_code"] == "01011102"
    assert "area_count" not in coverage

    with pytest.raises(IncompatibleSnapshotError, match="1 not in parent"):
        BerlinWFSConnector().check_parent(configuration(), batch.records, batch.records[:-1])
    with pytest.raises(IncompatibleSnapshotError, match="boundary-edition mismatches"):
        BerlinWFSConnector().check_parent(
            configuration(),
            batch.records,
            tuple(replace(record, boundary_edition="old") for record in batch.records),
        )
