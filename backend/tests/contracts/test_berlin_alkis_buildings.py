"""Offline schema, geometry, address, and paging contracts for ALKIS buildings."""

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from shapely.geometry import shape

from app.connectors.berlin_alkis_buildings import BerlinALKISBuildingsConnector
from app.connectors.berlin_wfs import page_url
from app.domain import (
    ConfigError,
    DatasetConfig,
    FetchRequest,
    RawAsset,
    RawBundle,
    SchemaError,
    ValidationError,
    record_from_dict,
    record_to_dict,
)
from app.infrastructure.http import HTTPFetcher

BACKEND = Path(__file__).parents[2]
FIXTURES = BACKEND / "tests/fixtures/berlin_alkis_buildings"
CONFIG = BACKEND / "config/datasets/berlin_alkis_buildings.json"


def configuration(**changes):
    values = json.loads(CONFIG.read_text())
    values["allowed_hosts"] = tuple(values["allowed_hosts"])
    options = values["parser_options"]
    options.update(page_size=2, max_pages=10, expected_count_range=[4, 4])
    options.update(changes.pop("parser_options", {}))
    values.update(changes)
    return DatasetConfig(**values)


def page_url_for(config, page_number):
    return page_url(config.resource_url, page_number * 2, 2)


def make_bundle(paths, config=None, local=False):
    config = config or configuration()
    return RawBundle(
        assets=tuple(
            RawAsset(
                path=str(path),
                sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                size_bytes=path.stat().st_size,
                resource_url=config.resource_url if local else page_url_for(config, page_number),
            )
            for page_number, path in enumerate(paths)
        ),
        retrieved_at=datetime.now(timezone.utc),
        snapshot_id="alkis-contract",
        request_fingerprint="fixture",
    )


def edited_pages(tmp_path, mutate_first=None, mutate_second=None):
    paths = []
    for index, mutation in enumerate((mutate_first, mutate_second)):
        data = json.loads((FIXTURES / f"page-{index:04d}.json").read_text())
        if mutation:
            mutation(data)
        path = tmp_path / f"page-{index:04d}.json"
        path.write_text(json.dumps(data))
        paths.append(path)
    return paths


def normalize(paths=None, config=None):
    config = config or configuration()
    paths = paths or [FIXTURES / "page-0000.json", FIXTURES / "page-0001.json"]
    return BerlinALKISBuildingsConnector().normalize(make_bundle(paths, config), config)


def test_maps_pages_preserves_polygon_types_nulls_attributes_and_roundtrip():
    batch = normalize()
    first, pseudonumber, third, fourth = batch.records

    assert [record.geometry["type"] for record in batch.records] == [
        "MultiPolygon",
        "MultiPolygon",
        "Polygon",
        "MultiPolygon",
    ]
    assert first.record_id == f"alkis_building:{first.source_record_id}"
    assert first.function_code == "1010"
    assert first.function_label == "Wohnhaus"
    assert first.storeys_above_ground == 5
    assert first.address_labels == ("Bürknerstraße 27", "Bürknerstraße 28")
    assert first.street_codes == ("00796",)
    assert first.attributes["baw"] == "2500"
    assert first.source_url and "RESOURCEID=gebaeude." in first.source_url
    assert pseudonumber.storeys_above_ground is None
    assert pseudonumber.storeys_below_ground == 1
    assert pseudonumber.address_labels == ("Bürknerstraße 27",)
    assert pseudonumber.address_reference_kind == "pseudonumber"
    assert pseudonumber.attributes["pnr"] == "27"
    assert pseudonumber.storeys_above_ground != 0
    assert third.geometry["type"] == "Polygon"
    assert third.address_reference_kind == "pseudonumber"
    assert fourth.address_reference_kind == "house_number"
    payload = record_to_dict(pseudonumber)
    payload["street_codes"] = list(payload["street_codes"])
    payload["address_labels"] = list(payload["address_labels"])
    assert record_to_dict(record_from_dict(payload)) == payload
    assert "ofl" not in first.attributes
    assert "bezofl" not in first.attributes
    assert normalize().warnings[0].startswith("Footprints come from the ALKIS live layer")


def test_building_record_rejects_invalid_values_and_roundtrips_lists():
    record = normalize().records[0]
    with pytest.raises(ValidationError, match="nonnegative integer"):
        record_from_dict({**record_to_dict(record), "storeys_above_ground": True})
    with pytest.raises(ValidationError, match="finite and nonnegative"):
        record_from_dict({**record_to_dict(record), "footprint_area_m2": float("inf")})
    with pytest.raises(ValidationError, match="valid nonempty polygon"):
        record_from_dict(
            {
                **record_to_dict(record),
                "geometry": {"type": "Polygon", "coordinates": []},
            }
        )


def test_missing_property_is_an_error_and_extra_property_warns(tmp_path):
    paths = edited_pages(
        tmp_path,
        mutate_first=lambda page: page["features"][0]["properties"].pop("gfk"),
    )
    with pytest.raises(SchemaError, match="missing expected properties.*gfk"):
        normalize(paths)
    paths = edited_pages(
        tmp_path,
        mutate_first=lambda page: page["features"][0]["properties"].update(new_field="value"),
    )
    batch = normalize(paths)
    assert any("extra properties: new_field" in warning for warning in batch.warnings)


def test_integer_string_and_duplicate_uuid_are_rejected(tmp_path):
    paths = edited_pages(
        tmp_path,
        mutate_first=lambda page: page["features"][0]["properties"].update(aog="5"),
    )
    with pytest.raises(SchemaError, match="aog must be a nonnegative integer"):
        normalize(paths)
    paths = edited_pages(
        tmp_path,
        mutate_second=lambda page: page["features"][0]["properties"].update(
            uuid="DEBE00YY11A0002j"
        ),
    )
    with pytest.raises(SchemaError, match="Duplicate ALKIS uuid"):
        normalize(paths)


def test_invalid_geometry_is_made_valid_and_nonpolygon_is_rejected(tmp_path):
    def bowtie(page):
        feature = page["features"][0]
        bounds = shape(feature["geometry"]).bounds
        left, bottom, right, top = bounds
        feature["geometry"] = {
            "type": "Polygon",
            "coordinates": [
                [
                    [left, bottom],
                    [right, top],
                    [left, top],
                    [right, bottom],
                    [left, bottom],
                ]
            ],
        }

    paths = edited_pages(tmp_path, mutate_first=bowtie)
    batch = normalize(paths)
    assert "geometry_made_valid" in batch.records[0].quality_flags
    assert shape(batch.records[0].geometry).is_valid
    assert BerlinALKISBuildingsConnector().validate(batch).valid
    paths = edited_pages(
        tmp_path,
        mutate_first=lambda page: page["features"][0].update(
            geometry={"type": "LineString", "coordinates": [[391800, 5818000], [391801, 5818001]]}
        ),
    )
    with pytest.raises(SchemaError, match="must be polygonal"):
        normalize(paths)


@pytest.mark.parametrize("crs", ["EPSG:4326", "urn:ogc:def:crs:EPSG::4326"])
def test_crs_mismatch_and_northing_first_crs_are_rejected(tmp_path, crs):
    paths = edited_pages(
        tmp_path,
        mutate_first=lambda page: page.update(crs={"type": "name", "properties": {"name": crs}}),
    )
    with pytest.raises(SchemaError, match="WFS CRS differs"):
        normalize(paths)


@pytest.mark.parametrize(
    "case,match",
    [
        ("missing_next", "omitted the next link"),
        ("matched_change", "numberMatched changed"),
        ("page_cap", "page cap"),
        ("exception", "ExceptionReport"),
    ],
)
def test_fetch_rejects_partial_or_invalid_wfs_bundles(tmp_path, case, match):
    config = configuration(parser_options={"max_pages": 1 if case == "page_cap" else 10})

    async def handler(request):
        start = int(request.url.params["STARTINDEX"])
        data = json.loads((FIXTURES / f"page-{start // 2:04d}.json").read_text())
        if case == "exception":
            return httpx.Response(
                200,
                content=(BACKEND / "tests/fixtures/berlin_wfs/exception_report.xml").read_bytes(),
                request=request,
            )
        if case == "missing_next" and start == 0:
            data["links"] = []
        if case == "matched_change" and start == 2:
            data["numberMatched"] = 3
        return httpx.Response(200, json=data, request=request)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            connector = BerlinALKISBuildingsConnector(HTTPFetcher(client))
            await connector.fetch(
                FetchRequest(
                    config=config,
                    staging_dir=str(tmp_path / "staging"),
                    snapshot_id="fetch-fixture",
                )
            )

    with pytest.raises(SchemaError, match=match):
        asyncio.run(run())


def test_normalize_rejects_incomplete_page_bundle():
    config = configuration()
    bundle = make_bundle([FIXTURES / "page-0000.json", FIXTURES / "page-0001.json"], config)
    incomplete = RawBundle(
        assets=bundle.assets[:1],
        retrieved_at=bundle.retrieved_at,
        snapshot_id=bundle.snapshot_id,
        request_fingerprint=bundle.request_fingerprint,
    )
    with pytest.raises(SchemaError, match="count mismatch"):
        BerlinALKISBuildingsConnector().normalize(incomplete, config)


def test_cql_must_match_the_extent_and_bauteil_is_rejected(tmp_path):
    config = configuration(resource_url=configuration().resource_url.replace("391600", "391601"))
    with pytest.raises(ConfigError, match="CQL_FILTER must match"):
        normalize(config=config)
    paths = edited_pages(
        tmp_path,
        mutate_first=lambda page: page["features"][0]["properties"].update(bezeich="AX_Bauteil"),
    )
    with pytest.raises(SchemaError, match="bezeich must be AX_Gebaeude"):
        normalize(paths)


def test_feature_id_mismatch_is_flagged(tmp_path):
    paths = edited_pages(
        tmp_path,
        mutate_first=lambda page: page["features"][0].update(id="wrong-id"),
    )
    batch = normalize(paths)
    assert "feature_id_mismatch" in batch.records[0].quality_flags


def test_namlag_parsing_suffix_multistreet_and_area_warning(tmp_path):
    def update(page):
        properties = page["features"][0]["properties"]
        properties["namlag"] = "Jerusalemer Straße 43, 44; Krausenstraße 21, 22"
        properties["shape_area"] = 1.0

    paths = edited_pages(tmp_path, mutate_first=update)
    batch = normalize(paths)
    first = batch.records[0]
    assert first.address_labels == (
        "Jerusalemer Straße 43",
        "Jerusalemer Straße 44",
        "Krausenstraße 21",
        "Krausenstraße 22",
    )
    assert first.address_reference_kind == "house_number"
    assert "footprint_area_differs_from_provider" in first.quality_flags
    paths = edited_pages(
        tmp_path,
        mutate_first=lambda page: page["features"][0]["properties"].update(
            namlag="Krausenstraße 19 a"
        ),
    )
    assert normalize(paths).records[0].address_labels == ("Krausenstraße 19A",)
    record = normalize().records[1]
    assert record.address_reference_kind == "pseudonumber"
    assert record.address_labels == ("Bürknerstraße 27",)
    paths = edited_pages(
        tmp_path,
        mutate_second=lambda page: page["features"][0]["properties"].update(
            pnr=None, namlag="Ritterstraße"
        ),
    )
    street_only = normalize(paths).records[2]
    assert street_only.address_reference_kind == "street_only"
    assert street_only.address_labels == ("Ritterstraße",)


def test_local_single_file_mode_is_rejected(tmp_path):
    config = configuration()
    bundle = make_bundle([FIXTURES / "page-0000.json"], config=config, local=True)
    with pytest.raises(ConfigError, match="cannot normalize a local file"):
        BerlinALKISBuildingsConnector().normalize(bundle, config)
