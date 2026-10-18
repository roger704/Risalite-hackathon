"""Fixture-backed snapshot ingestion and building API contract."""

import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from shapely.geometry import shape

from app.api.main import create_app
from app.application.service import DataService
from app.connectors.berlin_alkis_buildings import BerlinALKISBuildingsConnector
from app.infrastructure.http import HTTPFetcher

BACKEND = Path(__file__).parents[2]
FIXTURES = BACKEND / "tests/fixtures/berlin_alkis_buildings"


async def test_ingests_fixture_pages_and_serves_building_endpoints(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    values = json.loads((BACKEND / "config/datasets/berlin_alkis_buildings.json").read_text())
    values["parser_options"].update(
        page_size=2,
        max_pages=10,
        expected_count_range=[4, 4],
    )
    (config_dir / "berlin_alkis_buildings.json").write_text(json.dumps(values))

    async def handler(request):
        start = int(request.url.params["STARTINDEX"])
        fixture = FIXTURES / f"page-{start // 2:04d}.json"
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=fixture.read_bytes(),
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = DataService(
            tmp_path / "data",
            config_dir,
            connectors={
                "berlin_alkis_buildings": BerlinALKISBuildingsConnector(HTTPFetcher(client))
            },
        )
        manifest = await service.ingest("berlin_alkis_buildings")

    with TestClient(create_app(service=service)) as client:
        records_response = client.get(
            "/api/v1/datasets/berlin_alkis_buildings/records",
            params={"snapshot_id": manifest.snapshot_id},
        )
        assert records_response.status_code == 200, records_response.text
        records_body = records_response.json()
        assert records_body["count"] == 4
        assert records_body["records"][0]["record_type"] == "building"
        assert records_body["records"][0]["geometry"]["type"] == "MultiPolygon"

        first = records_body["records"][0]
        point = shape(first["geometry"]).representative_point()
        lon, lat = point.x, point.y
        query = {
            "snapshot_id": manifest.snapshot_id,
            "lon": lon,
            "lat": lat,
            "radius_m": 20,
        }
        at_response = client.get("/api/v1/datasets/berlin_alkis_buildings/at", params=query)
        assert at_response.status_code == 200, at_response.text
        at_body = at_response.json()
        assert any(item["record_id"] == first["record_id"] for item in at_body["records"])
        assert at_body["dataset_id"] == "berlin_alkis_buildings"
        assert at_body["source_edition"] == "2026-09-25"
        assert "current use" in at_body["notice"]
        assert at_body["extent_coverage"]["name"] == "kreuzberg_kottbusser_tor"

        by_id = client.get(
            f"/api/v1/datasets/berlin_alkis_buildings/buildings/{first['record_id']}",
            params={"snapshot_id": manifest.snapshot_id},
        )
        assert by_id.status_code == 200
        assert by_id.json()["building"]["record_type"] == "building"
        missing = client.get(
            "/api/v1/datasets/berlin_alkis_buildings/buildings/alkis_building:missing",
            params={"snapshot_id": manifest.snapshot_id},
        )
        assert missing.status_code == 404

        address = first["address_labels"][0]
        street, number = address.rsplit(" ", 1)
        by_address = client.get(
            "/api/v1/datasets/berlin_alkis_buildings/by-address",
            params={
                "snapshot_id": manifest.snapshot_id,
                "street": street,
                "house_number": number,
                "street_code": first["street_codes"][0],
                "lon": lon,
                "lat": lat,
            },
        )
        assert by_address.status_code == 200, by_address.text
        assert by_address.json()["status"] in {"unique", "ambiguous"}
        assert by_address.json()["matches"]

        unsupported = client.get(
            "/api/v1/datasets/berlin_alkis_buildings/at",
            params={**query, "unexpected": "true"},
        )
        assert unsupported.status_code == 422
        assert unsupported.json()["error"]["code"] == "UNSUPPORTED_FILTER"
        unpaired = client.get(
            "/api/v1/datasets/berlin_alkis_buildings/by-address",
            params={
                "snapshot_id": manifest.snapshot_id,
                "street": street,
                "house_number": number,
                "lon": lon,
            },
        )
        assert unpaired.status_code == 422

        outside = client.get(
            "/api/v1/datasets/berlin_alkis_buildings/at",
            params={
                "snapshot_id": manifest.snapshot_id,
                "lon": 13.3,
                "lat": 52.5,
                "radius_m": 0,
            },
        )
        assert outside.status_code == 200
        assert (
            "outside imported extent; absence of a building is not evidence"
            in (outside.json()["warnings"])
        )
