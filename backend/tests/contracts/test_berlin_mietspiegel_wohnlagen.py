"""Offline contracts for the address-level Mietspiegel Wohnlage connector."""

import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import geopandas as gpd
import httpx
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient
from shapely.geometry import Point

from app.api.main import create_app
from app.application.service import DataService
from app.application.wohnlage_lookup import address_join_key, address_join_report, lookup_wohnlage
from app.connectors.berlin_mietspiegel import BerlinWohnlageConnector
from app.domain import (
    Area,
    ConfigError,
    ConnectorError,
    DatasetConfig,
    FetchRequest,
    IncompatibleSnapshotError,
    NotFoundError,
    RawAsset,
    RawBundle,
    SchemaError,
)
from app.infrastructure.http import HTTPFetcher
from app.infrastructure.storage import write_records

BACKEND = Path(__file__).parents[2]
FIXTURES = BACKEND / "tests/fixtures/berlin_mietspiegel_wohnlagen"
CONFIG_PATH = BACKEND / "config/datasets/berlin_mietspiegel_wohnlagen.json"


def configuration(**option_changes):
    values = json.loads(CONFIG_PATH.read_text())
    values["allowed_hosts"] = tuple(values["allowed_hosts"])
    options = values["parser_options"]
    options.update(page_size=3, max_pages=5, expected_count_range=[5, 5])
    options.update(option_changes)
    return DatasetConfig(**values)


def make_bundle(paths, snapshot_id="wohnlagen-test"):
    return RawBundle(
        assets=tuple(
            RawAsset(
                path=str(path),
                sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                size_bytes=Path(path).stat().st_size,
                resource_url=f"https://gdi.berlin.de/synthetic/{Path(path).name}",
            )
            for path in paths
        ),
        retrieved_at=datetime.now(timezone.utc),
        snapshot_id=snapshot_id,
        request_fingerprint="fixture",
    )


def edited_pages(tmp_path, edit_first=None, edit_second=None):
    paths = []
    for index, edit in enumerate((edit_first, edit_second)):
        value = json.loads((FIXTURES / f"page-{index:04d}.json").read_text())
        if edit:
            edit(value)
        path = tmp_path / f"edited-{index}.json"
        path.write_text(json.dumps(value))
        paths.append(path)
    return paths


def normalized_records(**option_changes):
    config = configuration(**option_changes)
    raw = make_bundle([FIXTURES / "page-0000.json", FIXTURES / "page-0001.json"])
    connector = BerlinWohnlageConnector()
    batch = connector.normalize(raw, config)
    return connector, config, batch


def test_normalizes_record_contracts_and_parent_coverage():
    connector, config, batch = normalized_records()

    assert len(batch.records) == 5
    assert connector.validate(batch).valid
    by_key = {record.address_key: record for record in batch.records}
    assert by_key["00001004A"].house_number == 4
    assert by_key["00001004A"].house_number_label == "004A"
    assert by_key["00001004A"].house_number_suffix == "A"
    assert by_key["00001004A"].record_id == "wohnlage:2026:00001004A"
    assert by_key["00001004A"].source_record_id == "00001004A"
    later_edition = replace(
        by_key["00001004A"],
        record_id="wohnlage:2027:00001004A",
        edition="Berliner Mietspiegel 2027",
        valid_from="2027-05-28",
        valid_to="2029-05-28",
    )
    assert later_edition.record_id == "wohnlage:2027:00001004A"
    assert by_key["00001004A"].geometry["type"] == "Point"
    assert {record.wohnlage_class for record in batch.records} == {"einfach", "mittel", "gut"}
    assert {record.east_west_marker for record in batch.records} == {"Ost", "West"}
    quality = connector.validate(batch)
    assert any("Wohnlage class counts:" in warning for warning in quality.warnings)
    assert any("Ost/West marker counts: Ost=" in warning for warning in quality.warnings)
    assert any("not household income" in warning for warning in quality.warnings)

    parent = Area(
        source_id="berlin_lor",
        snapshot_id="lor-test",
        source_record_id="01000001",
        record_id="planungsraum:01000001",
        geometry={
            "type": "Polygon",
            "coordinates": [[[12.0, 52.0], [14.0, 52.0], [14.0, 53.0], [12.0, 53.0], [12.0, 52.0]]],
        },
        area_code="01000001",
        name="Test LOR",
        hierarchy="planungsraum",
        boundary_edition=config.parser_options["parent_check"]["boundary_edition"],
    )
    coverage = connector.check_parent(config, batch.records, (parent,))
    assert coverage["points_outside_all_lor_areas"] == 0
    assert coverage["lor_areas_without_wohnlage_addresses"] == []
    assert coverage["join_method"].startswith("point-in-polygon in EPSG:25833")


@pytest.mark.parametrize(
    "edit,match",
    [
        (lambda data: data["features"][0]["properties"].pop("plz"), "property schema changed"),
        (
            lambda data: data["features"][0]["properties"].update(unexpected="value"),
            "property schema changed",
        ),
        (lambda data: data["features"][0]["properties"].update(plz=13585), "strings or null"),
    ],
)
def test_rejects_schema_drift(tmp_path, edit, match):
    paths = edited_pages(tmp_path, edit_first=edit)
    with pytest.raises(SchemaError, match=match):
        BerlinWohnlageConnector().normalize(make_bundle(paths), configuration())


def test_null_and_blank_values_remain_missing_and_are_flagged(tmp_path):
    def edit(data):
        data["features"][0]["properties"]["plz"] = " "
        data["features"][0]["properties"]["bezname"] = None

    paths = edited_pages(tmp_path, edit_first=edit)
    batch = BerlinWohnlageConnector().normalize(make_bundle(paths), configuration())
    record = batch.records[0]
    assert record.postcode is None
    assert record.district_name is None
    assert "field_missing:plz" in record.quality_flags
    assert "field_missing:bezname" in record.quality_flags


def test_unassigned_and_unmapped_labels_fail_the_configured_share_limit(tmp_path):
    def edit_first(data):
        data["features"][0]["properties"]["wol"] = None

    def edit_second(data):
        data["features"][0]["properties"]["wol"] = "luxury"

    paths = edited_pages(tmp_path, edit_first, edit_second)
    connector = BerlinWohnlageConnector()
    batch = connector.normalize(make_bundle(paths), configuration())
    assert batch.records[0].class_status == "unassigned"
    assert batch.records[0].wohnlage_class is None
    assert batch.records[3].class_status == "unmapped"
    assert batch.records[3].wohnlage_class is None
    assert batch.records[3].source_label == "luxury"
    report = connector.validate(batch)
    assert not report.valid
    assert any("exceeds configured maximum" in error for error in report.errors)


def test_rejects_key_house_number_mismatch_duplicate_and_wrong_feature_id(tmp_path):
    def mismatch(data):
        data["features"][0]["properties"]["hnr"] = "006"

    with pytest.raises(SchemaError, match="key/house-number identity"):
        BerlinWohnlageConnector().normalize(
            make_bundle(edited_pages(tmp_path, edit_first=mismatch)), configuration()
        )

    def duplicate(data):
        data["features"][0] = json.loads((FIXTURES / "page-0000.json").read_text())["features"][0]

    with pytest.raises(SchemaError, match="Duplicate Wohnlage address key"):
        BerlinWohnlageConnector().normalize(
            make_bundle(edited_pages(tmp_path, edit_second=duplicate)), configuration()
        )

    def wrong_id(data):
        data["features"][0]["id"] = "wohnlagenadr2026.00000000"

    with pytest.raises(SchemaError, match="feature id does not match"):
        BerlinWohnlageConnector().normalize(
            make_bundle(edited_pages(tmp_path, edit_first=wrong_id)), configuration()
        )


def test_edition_check_and_mixed_record_editions():
    values = json.loads(CONFIG_PATH.read_text())
    values["allowed_hosts"] = tuple(values["allowed_hosts"])
    values["parser_options"].update(page_size=3, max_pages=5, expected_count_range=[5, 5])
    values["parser_options"]["typename"] = "wohnlagenadr2025:wohnlagenadr2025"
    values["resource_url"] = values["resource_url"].replace(
        "wohnlagenadr2026:wohnlagenadr2026", "wohnlagenadr2025:wohnlagenadr2025"
    )
    config = DatasetConfig(**values)
    with pytest.raises(ConfigError, match="edition_year"):
        BerlinWohnlageConnector().normalize(
            make_bundle([FIXTURES / "page-0000.json", FIXTURES / "page-0001.json"]), config
        )

    connector, _, batch = normalized_records()
    mixed = replace(batch.records[0], edition="Berliner Mietspiegel 2025")
    report = connector.validate(replace(batch, records=(mixed, *batch.records[1:])))
    assert not report.valid
    assert any("mixes editions" in error for error in report.errors)


def test_detects_short_pages_and_numbermatched_mismatch(tmp_path):
    def shorten(data):
        data["features"].pop()

    paths = edited_pages(tmp_path, edit_second=shorten)
    connector = BerlinWohnlageConnector()
    batch = connector.normalize(make_bundle(paths), configuration())
    report = connector.validate(batch)
    assert not report.valid
    assert any("count mismatch" in error for error in report.errors)

    def change_matched(data):
        data["numberMatched"] = 6

    paths = edited_pages(tmp_path, edit_second=change_matched)
    with pytest.raises(SchemaError, match="numberMatched changed"):
        BerlinWohnlageConnector().normalize(make_bundle(paths), configuration())


def test_fetch_uses_shared_wfs_paging_and_rejects_http_200_exceptionreport(tmp_path):
    async def handler(request):
        start = int(request.url.params["STARTINDEX"])
        page = FIXTURES / f"page-{start // 3:04d}.json"
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=page.read_bytes(),
            request=request,
        )

    async def fetch():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            connector = BerlinWohnlageConnector(HTTPFetcher(client))
            return await connector.fetch(
                FetchRequest(
                    config=configuration(),
                    staging_dir=str(tmp_path / "downloads"),
                    snapshot_id="fetched",
                )
            )

    bundle = asyncio.run(fetch())
    assert len(bundle.assets) == 2
    assert [asset.size_bytes for asset in bundle.assets] == [
        Path(asset.path).stat().st_size for asset in bundle.assets
    ]

    async def exception_handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/xml"},
            content=(
                b'<ExceptionReport><Exception exceptionCode="InvalidParameterValue">'
                b"<ExceptionText>bad typename</ExceptionText></Exception></ExceptionReport>"
            ),
            request=request,
        )

    async def fetch_exception():
        async with httpx.AsyncClient(transport=httpx.MockTransport(exception_handler)) as client:
            connector = BerlinWohnlageConnector(HTTPFetcher(client))
            await connector.fetch(
                FetchRequest(
                    config=configuration(),
                    staging_dir=str(tmp_path / "exception"),
                    snapshot_id="exception",
                )
            )

    with pytest.raises(SchemaError, match="OGC ExceptionReport"):
        asyncio.run(fetch_exception())


def test_address_join_key_padding_suffix_and_bounds():
    assert address_join_key("04786", 4) == "04786004"
    assert address_join_key("00001", 4, "a") == "00001004A"
    assert address_join_key("00001", 1000) is None
    assert address_join_key("1234", 4) is None
    assert address_join_key("00001", None) is None
    assert address_join_key("00001", 4, "ß") is None


def _fake_service(tmp_path, wohnlage_record, address_rows=()):
    config = configuration()
    wohnlage_path = tmp_path / "wohnlagen.parquet"
    write_records((wohnlage_record,), wohnlage_path)
    address_path = tmp_path / "addresses.parquet"
    if address_rows:
        address_frame = gpd.GeoDataFrame(
            {
                "record_id": [row["record_id"] for row in address_rows],
                "record_type": ["address"] * len(address_rows),
                "area_code": [None] * len(address_rows),
                "record_json": [
                    json.dumps(row, ensure_ascii=False, sort_keys=True) for row in address_rows
                ],
                "geometry": [Point(13.0, 52.5) for _ in address_rows],
            },
            geometry="geometry",
            crs="EPSG:4326",
        )
        address_frame.to_parquet(address_path, index=False)
    else:
        pq.write_table(
            pa.table(
                {
                    "record_id": [],
                    "record_type": [],
                    "area_code": [],
                    "record_json": [],
                }
            ),
            address_path,
        )
    manifest = SimpleNamespace(
        snapshot_id="wohnlagen-snapshot",
        retrieved_at=datetime(2026, 10, 18, tzinfo=timezone.utc),
        source_edition="2026-05-28",
        valid_from="2026-05-28",
        valid_to="2028-05-28",
        attribution="Berlin provider attribution",
        license_id="dl-de-zero-2.0",
        license_url="https://www.govdata.de/dl-de/zero-2-0",
        raw_assets=(SimpleNamespace(sha256="a" * 64),),
        quality_report=SimpleNamespace(warnings=("class counts",)),
    )
    address_manifest = SimpleNamespace(snapshot_id="address-snapshot")
    snapshots = {
        ("berlin_mietspiegel_wohnlagen", "wohnlagen-snapshot"): manifest,
        ("berlin_addresses", "address-snapshot"): address_manifest,
    }
    paths = {"wohnlagen-snapshot": wohnlage_path, "address-snapshot": address_path}

    class Catalog:
        def get(self, dataset_id, snapshot_id):
            try:
                return snapshots[(dataset_id, snapshot_id)]
            except KeyError as exc:
                raise NotFoundError("missing test snapshot") from exc

    class Service:
        catalog = Catalog()

        @staticmethod
        def _config(dataset_id):
            assert dataset_id == "berlin_mietspiegel_wohnlagen"
            return config

        @staticmethod
        def _snapshot_path(snapshot):
            return paths[snapshot.snapshot_id]

    service = Service()
    service.manifest = manifest
    return service, config, wohnlage_path, address_path


def test_lookup_matched_unclassified_not_found_and_suffix_candidates(tmp_path):
    _, _, batch = normalized_records()
    suffix_record = next(row for row in batch.records if row.address_key == "00001004A")
    service, _, _, _ = _fake_service(tmp_path, suffix_record)

    matched = lookup_wohnlage(
        service, "berlin_mietspiegel_wohnlagen", "wohnlagen-snapshot", address_key="00001004A"
    )
    assert matched["join_status"] == "matched"
    assert matched["wohnlage_class"] == "mittel"
    assert matched["evidence"]["schluessel"] == "00001004A"
    assert matched["evidence"]["raw_file_hashes"] == ["a" * 64]
    street_mode = lookup_wohnlage(
        service,
        "berlin_mietspiegel_wohnlagen",
        "wohnlagen-snapshot",
        street_code="00001",
        house_number=4,
        house_number_suffix="a",
    )
    assert street_mode["join_status"] == "matched"

    unclassified_record = replace(
        suffix_record,
        class_status="unassigned",
        wohnlage_class=None,
        source_label=None,
    )
    service, _, _, _ = _fake_service(tmp_path / "unclassified", unclassified_record)
    unclassified = lookup_wohnlage(
        service, "berlin_mietspiegel_wohnlagen", "wohnlagen-snapshot", address_key="00001004A"
    )
    assert unclassified["join_status"] == "matched_unclassified"
    assert unclassified["wohnlage_class"] is None

    service, _, _, _ = _fake_service(tmp_path / "missing", suffix_record)
    not_found = lookup_wohnlage(
        service, "berlin_mietspiegel_wohnlagen", "wohnlagen-snapshot", address_key="00001004"
    )
    assert not_found["join_status"] == "not_found"
    assert [row["address_key"] for row in not_found["candidates"]] == ["00001004A"]
    assert not_found["evidence"]["lookup_key"] == "00001004"
    assert "schluessel" not in not_found["evidence"]
    assert "provider_feature_id" not in not_found["evidence"]


@pytest.mark.parametrize("valid_from", [None, "2026", "2026-02-30"])
def test_lookup_rejects_missing_or_malformed_snapshot_validity(tmp_path, valid_from):
    _, _, batch = normalized_records()
    record = batch.records[0]
    service, _, _, _ = _fake_service(tmp_path, record)
    service.manifest.valid_from = valid_from

    with pytest.raises(ConnectorError, match="missing or malformed valid_from") as error:
        lookup_wohnlage(
            service,
            "berlin_mietspiegel_wohnlagen",
            "wohnlagen-snapshot",
            address_key=record.address_key,
        )

    assert error.value.code == "SNAPSHOT_INCOMPATIBLE"


def test_lookup_address_mode_ambiguous_and_not_joinable(tmp_path):
    _, _, batch = normalized_records()
    record = next(row for row in batch.records if row.address_key == "00001004A")
    address = {
        "record_type": "address",
        "record_id": "address:addr-1",
        "source_record_id": "addr-1",
        "street_name": "Different Street",
        "street_code": "00001",
        "house_number": 4,
        "house_number_suffix": "A",
        "postcode": "00000",
        "address_type": "Adresse",
    }
    matching_address = {
        **address,
        "street_name": "Aachener Straße",
        "postcode": "10713",
    }
    service, _, _, _ = _fake_service(tmp_path / "matching", record, [matching_address])
    matched = lookup_wohnlage(
        service,
        "berlin_mietspiegel_wohnlagen",
        "wohnlagen-snapshot",
        address_snapshot_id="address-snapshot",
        address_id="addr-1",
    )
    assert matched["join_status"] == "matched"
    assert matched["address"]["address_id"] == "addr-1"

    service, _, _, _ = _fake_service(tmp_path, record, [address])
    ambiguous = lookup_wohnlage(
        service,
        "berlin_mietspiegel_wohnlagen",
        "wohnlagen-snapshot",
        address_snapshot_id="address-snapshot",
        address_id="addr-1",
    )
    assert ambiguous["join_status"] == "ambiguous"
    assert ambiguous["wohnlage_class"] is None
    assert ambiguous["record"] is None
    assert ambiguous["candidates"][0]["address_key"] == record.address_key

    address["house_number"] = None
    service, _, _, _ = _fake_service(tmp_path / "no-number", record, [address])
    result = lookup_wohnlage(
        service,
        "berlin_mietspiegel_wohnlagen",
        "wohnlagen-snapshot",
        address_snapshot_id="address-snapshot",
        address_id="addr-1",
    )
    assert result["join_status"] == "not_joinable"

    with pytest.raises(NotFoundError, match="No published berlin_addresses snapshot"):
        lookup_wohnlage(
            service,
            "berlin_mietspiegel_wohnlagen",
            "wohnlagen-snapshot",
            address_snapshot_id="missing",
            address_id="addr-1",
        )


def test_join_report_counts_streamed_address_and_wohnlage_rows(tmp_path):
    _, _, batch = normalized_records()
    wohnlage_path = tmp_path / "wohnlagen.parquet"
    write_records(batch.records, wohnlage_path)
    addresses = [
        {
            "record_id": "address:a",
            "record_type": "address",
            "area_code": None,
            "record_json": json.dumps(
                {
                    "record_type": "address",
                    "street_code": "00001",
                    "house_number": 4,
                    "house_number_suffix": "A",
                    "street_name": "Aachener Straße",
                    "postcode": "10713",
                },
                ensure_ascii=False,
            ),
        },
        {
            "record_id": "address:b",
            "record_type": "address",
            "area_code": None,
            "record_json": json.dumps(
                {
                    "record_type": "address",
                    "street_code": "99999",
                    "house_number": 1,
                    "house_number_suffix": None,
                    "street_name": "Unknown",
                    "postcode": "00000",
                }
            ),
        },
        {
            "record_id": "address:conflict",
            "record_type": "address",
            "area_code": None,
            "record_json": json.dumps(
                {
                    "record_type": "address",
                    "street_code": "00001",
                    "house_number": 4,
                    "house_number_suffix": "A",
                    "street_name": "Wrong Street",
                    "postcode": "10713",
                }
            ),
        },
        {
            "record_id": "address:c",
            "record_type": "address",
            "area_code": None,
            "record_json": json.dumps(
                {
                    "record_type": "address",
                    "street_code": "00001",
                    "house_number": None,
                    "house_number_suffix": None,
                    "street_name": "Platz/Straße ohne HNR",
                    "postcode": None,
                }
            ),
        },
    ]
    address_path = tmp_path / "addresses.parquet"
    pq.write_table(pa.Table.from_pylist(addresses), address_path)
    report = address_join_report(wohnlage_path, address_path)
    assert report == {
        "addresses_total": 4,
        "joinable": 3,
        "matched": 1,
        "not_found": 1,
        "ambiguous": 1,
        "not_joinable": 1,
        "wohnlage_rows_without_any_address": 4,
        "wohnlage_rows_total": 5,
    }


def test_service_ingest_preserves_previous_snapshot_and_api_records(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    values = json.loads(CONFIG_PATH.read_text())
    values["parser_options"].update(page_size=3, max_pages=5, expected_count_range=[5, 5])
    values["parser_options"].pop("parent_dataset")
    (config_dir / "berlin_mietspiegel_wohnlagen.json").write_text(json.dumps(values))
    response_state = {"unclassified": False}

    async def handler(request):
        start = int(request.url.params["STARTINDEX"])
        data = json.loads((FIXTURES / f"page-{start // 3:04d}.json").read_text())
        if response_state["unclassified"] and start == 0:
            data["features"][0]["properties"]["wol"] = None
            data["features"][1]["properties"]["wol"] = "unmapped"
        return httpx.Response(200, json=data, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    connector = BerlinWohnlageConnector(HTTPFetcher(client))
    service = DataService(
        data_dir=tmp_path / "data",
        config_dir=config_dir,
        connectors={"berlin_wfs_wohnlagen": connector},
    )

    async def ingest():
        async with client:
            first_snapshot = await service.ingest("berlin_mietspiegel_wohnlagen")
            response_state["unclassified"] = True
            with pytest.raises(ConnectorError, match="Unassigned and unmapped share"):
                await service.ingest("berlin_mietspiegel_wohnlagen")
            assert (
                service.catalog.get("berlin_mietspiegel_wohnlagen").snapshot_id
                == first_snapshot.snapshot_id
            )
            return first_snapshot

    first = asyncio.run(ingest())
    assert first.quality_report.output_rows == 5
    assert len(first.raw_assets) == 2
    assert all(len(asset.sha256) == 64 for asset in first.raw_assets)
    assert first.coverage["record_count"] == 5

    app = create_app(service=service)
    with TestClient(app) as api:
        records = api.get(
            "/api/v1/datasets/berlin_mietspiegel_wohnlagen/records",
            params={"snapshot_id": first.snapshot_id, "limit": 10},
        )
        assert records.status_code == 200
        assert records.json()["count"] == 5
        assert records.json()["records"][0]["record_type"] == "residential_location_class"
        assert records.json()["records"][0]["geometry"]["type"] == "Point"

        matched = api.get(
            "/api/v1/datasets/berlin_mietspiegel_wohnlagen/wohnlage",
            params={"snapshot_id": first.snapshot_id, "address_key": "00001004A"},
        )
        assert matched.status_code == 200
        assert matched.json()["join_status"] == "matched"
        invalid = api.get(
            "/api/v1/datasets/berlin_mietspiegel_wohnlagen/wohnlage",
            params={
                "snapshot_id": first.snapshot_id,
                "address_key": "00001004A",
                "street_code": "00001",
                "house_number": 4,
            },
        )
        assert invalid.status_code == 422
        assert (
            api.get(
                "/api/v1/datasets/berlin_mietspiegel_wohnlagen/wohnlage",
                params={"snapshot_id": first.snapshot_id, "street_code": "00001"},
            ).status_code
            == 422
        )

        current_config = service.configs["berlin_mietspiegel_wohnlagen"]
        moved_url = current_config.resource_url.replace("wohnlagenadr2026", "wohnlagenadr2028")
        moved_landing_url = current_config.landing_url.replace("2026", "2028")
        moved_options = {
            **current_config.parser_options,
            "edition_year": 2028,
            "typename": "wohnlagenadr2028:wohnlagenadr2028",
            "edition": "Berliner Mietspiegel 2028",
            "source_links": [moved_landing_url],
            "limitations": ["2028 registry limitation"],
        }
        service.configs["berlin_mietspiegel_wohnlagen"] = replace(
            current_config,
            resource_url=moved_url,
            landing_url=moved_landing_url,
            parser_options=moved_options,
        )

        historical = api.get(
            "/api/v1/datasets/berlin_mietspiegel_wohnlagen/wohnlage",
            params={"snapshot_id": first.snapshot_id, "address_key": "00001004A"},
        )
        assert historical.status_code == 200
        historical_body = historical.json()
        assert historical_body["join_status"] == "matched"
        assert historical_body["edition"] == "Berliner Mietspiegel 2026"
        assert historical_body["evidence"]["typename"] == "wohnlagenadr2026:wohnlagenadr2026"
        assert historical_body["evidence"]["provider_feature_id"] == "wohnlagenadr2026.00001004A"
        assert historical_body["source_links"] == [first.landing_url]
        assert historical_body["limitations"] == list(first.quality_report.warnings)
        moved_warning = (
            "Registry configuration has moved to a different edition; response metadata is "
            "taken from the pinned snapshot."
        )
        assert moved_warning in historical_body["warnings"]

        historical_not_found = api.get(
            "/api/v1/datasets/berlin_mietspiegel_wohnlagen/wohnlage",
            params={"snapshot_id": first.snapshot_id, "address_key": "00001004"},
        )
        assert historical_not_found.status_code == 200
        historical_not_found_body = historical_not_found.json()
        assert historical_not_found_body["join_status"] == "not_found"
        assert historical_not_found_body["edition"] == "Berliner Mietspiegel 2026"
        assert [row["address_key"] for row in historical_not_found_body["candidates"]] == [
            "00001004A"
        ]
        assert moved_warning in historical_not_found_body["warnings"]


def test_wohnlage_endpoint_rejects_published_non_wohnlage_dataset(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    values = json.loads((BACKEND / "config/datasets/berlin_lor.json").read_text())
    values.update(
        resource_url="https://example.org/synthetic/berlin_lor",
        allowed_hosts=["example.org"],
        source_crs="EPSG:4326",
    )
    values["parser_options"].update(expected_rows=2, name_field="PLR_NAME")
    (config_dir / "berlin_lor.json").write_text(json.dumps(values))
    service = DataService(data_dir=tmp_path / "data", config_dir=config_dir)
    snapshot = asyncio.run(
        service.ingest("berlin_lor", local_file=BACKEND / "tests/fixtures/lor/areas.geojson")
    )

    with TestClient(create_app(service=service)) as api:
        response = api.get(
            "/api/v1/datasets/berlin_lor/wohnlage",
            params={"snapshot_id": snapshot.snapshot_id, "address_key": "00001004A"},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


def test_rejects_parent_outside_threshold():
    connector, config, batch = normalized_records()
    far_parent = Area(
        source_id="berlin_lor",
        snapshot_id="lor-test",
        source_record_id="01000001",
        record_id="planungsraum:01000001",
        geometry={
            "type": "Polygon",
            "coordinates": [[[10.0, 50.0], [11.0, 50.0], [11.0, 51.0], [10.0, 51.0], [10.0, 50.0]]],
        },
        area_code="01000001",
        name="Outside",
        hierarchy="planungsraum",
        boundary_edition="2021",
    )
    with pytest.raises(IncompatibleSnapshotError, match="outside LOR areas share"):
        connector.check_parent(config, batch.records, (far_parent,))


def test_rejects_invalid_lookup_selector_and_key(tmp_path):
    _, _, batch = normalized_records()
    record = batch.records[0]
    service, _, _, _ = _fake_service(tmp_path, record)
    with pytest.raises(ConnectorError, match="invalid format"):
        lookup_wohnlage(
            service,
            "berlin_mietspiegel_wohnlagen",
            "wohnlagen-snapshot",
            address_key="123",
        )
    with pytest.raises(ConnectorError, match="exactly one"):
        lookup_wohnlage(service, "berlin_mietspiegel_wohnlagen", "wohnlagen-snapshot")
