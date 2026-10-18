"""Address ingestion, parent validation, and read API with synthetic fixtures."""

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.application.service import DataService
from app.connectors.berlin_addresses import BerlinAddressConnector
from app.connectors.berlin_wfs import page_url
from app.connectors.lor import LORConnector
from app.infrastructure.http import HTTPFetcher

BACKEND = Path(__file__).parents[2]
FIXTURES = BACKEND / "tests/fixtures/berlin_addresses"


@pytest.mark.asyncio
async def test_ingests_and_searches_addresses_with_pinned_lor(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    lor = json.loads((BACKEND / "config/datasets/berlin_lor.json").read_text())
    lor.update(
        resource_url="https://example.org/synthetic-lor",
        allowed_hosts=["example.org"],
        source_crs="EPSG:4326",
    )
    lor["parser_options"].update(expected_rows=2, name_field="PLR_NAME")
    addresses = json.loads((BACKEND / "config/datasets/berlin_addresses.json").read_text())
    address_options = addresses["parser_options"]
    address_options.pop("expected_count_range")
    address_options.update(page_size=3, max_pages=10, expected_count=6)
    (config_dir / "berlin_lor.json").write_text(json.dumps(lor))
    (config_dir / "berlin_addresses.json").write_text(json.dumps(addresses))

    parent_source = BACKEND / "tests/fixtures/lor/areas.geojson"

    async def handler(request):
        start = int(request.url.params["STARTINDEX"])
        data = json.loads((FIXTURES / f"page-{start // 3:04d}.json").read_text())
        for feature in data["features"]:
            feature["properties"]["plr_nr"] = "01011101"
            if feature["properties"]["adressid"] == "1005":
                feature["geometry"]["coordinates"] = [400000, 5819800]
            else:
                feature["geometry"]["coordinates"] = [388348.98, 5818479.98]
        if start < 3:
            data["links"] = [
                {
                    "rel": "next",
                    "href": page_url(addresses["resource_url"], start + 3, 3),
                }
            ]
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=json.dumps(data).encode(),
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = DataService(
        tmp_path / "data",
        config_dir,
        connectors={
            "lor": LORConnector(),
            "berlin_wfs_addresses": BerlinAddressConnector(HTTPFetcher(client)),
        },
    )
    async with client:
        parent = await service.ingest("berlin_lor", local_file=parent_source)
        snapshot = await service.ingest("berlin_addresses", parent_snapshot_id=parent.snapshot_id)

    assert snapshot.parent_snapshot_ids == (parent.snapshot_id,)
    assert snapshot.coverage["outside_supplied_area_count"] == 1
    with TestClient(create_app(service=service)) as api:
        exact = api.get(
            "/api/v1/datasets/berlin_addresses/address-search",
            params={"snapshot_id": snapshot.snapshot_id, "q": "Wiener Straße 12"},
        )
        ambiguous = api.get(
            "/api/v1/datasets/berlin_addresses/address-search",
            params={"snapshot_id": snapshot.snapshot_id, "q": "Lindenstraße 10"},
        )
        first_page = api.get(
            "/api/v1/datasets/berlin_addresses/address-search",
            params={"snapshot_id": snapshot.snapshot_id, "q": "Wiener Straße", "limit": 1},
        )
        second_page = api.get(
            "/api/v1/datasets/berlin_addresses/address-search",
            params={
                "snapshot_id": snapshot.snapshot_id,
                "q": "Wiener Straße",
                "limit": 1,
                "cursor": first_page.json()["next_cursor"],
            },
        )
        outside = api.get(
            "/api/v1/datasets/berlin_addresses/address-search",
            params={"snapshot_id": snapshot.snapshot_id, "q": "Müllerstraße 1"},
        )
        unknown_param = api.get(
            "/api/v1/datasets/berlin_addresses/address-search",
            params={"snapshot_id": snapshot.snapshot_id, "q": "Wiener Straße", "ignored": "x"},
        )
        bad_postcode = api.get(
            "/api/v1/datasets/berlin_addresses/address-search",
            params={
                "snapshot_id": snapshot.snapshot_id,
                "q": "Wiener Straße",
                "postcode": "12",
            },
        )
        non_address = api.get(
            "/api/v1/datasets/berlin_lor/address-search",
            params={"snapshot_id": parent.snapshot_id, "q": "Wiener Straße"},
        )
        missing_snapshot = api.get(
            "/api/v1/datasets/berlin_addresses/address-search",
            params={"snapshot_id": "unknown", "q": "Wiener Straße"},
        )

    assert exact.status_code == 200
    assert exact.json()["results"][0]["stable_id"] == "address:1001"
    assert ambiguous.status_code == 200
    assert ambiguous.json()["ambiguous"] is True
    assert ambiguous.json()["total"] == 2
    assert first_page.json()["next_cursor"]
    assert second_page.json()["results"][0]["source_record_id"] == "1001"
    assert outside.json()["results"][0]["area_link"]["validated"] is False
    assert unknown_param.status_code == 422
    assert unknown_param.json()["error"]["code"] == "UNSUPPORTED_FILTER"
    assert bad_postcode.status_code == 422
    assert non_address.status_code == 422
    assert missing_snapshot.status_code == 404
