"""Fixture-backed snapshot ingestion and building API contract."""

import json
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from fastapi.testclient import TestClient
from pyproj import Transformer
from shapely.geometry import shape

from app.api.buildings import _metadata
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
    snapshot_bbox = list(values["parser_options"]["extent"]["bbox_25833"])
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
        assert at_body["coverage_partial"] is False

        metric_to_wgs84 = Transformer.from_crs("EPSG:25833", "EPSG:4326", always_xy=True)
        edge_lon, edge_lat = metric_to_wgs84.transform(
            snapshot_bbox[0] + 10,
            (snapshot_bbox[1] + snapshot_bbox[3]) / 2,
        )
        edge_at = client.get(
            "/api/v1/datasets/berlin_alkis_buildings/at",
            params={
                "snapshot_id": manifest.snapshot_id,
                "lon": edge_lon,
                "lat": edge_lat,
                "radius_m": 25,
            },
        )
        assert edge_at.status_code == 200, edge_at.text
        assert edge_at.json()["coverage_partial"] is True
        assert (
            "search radius extends beyond imported extent; buildings outside it are missing"
            in edge_at.json()["warnings"]
        )

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
        by_address_body = by_address.json()
        assert by_address_body["status"] == "ambiguous"
        assert by_address_body["own_address_match_count"] == 1
        assert by_address_body["pseudonumber_match_count"] == 1
        assert len(by_address_body["matches"]) == 2
        assert by_address_body["coverage_partial"] is False

        outside_lon, outside_lat = metric_to_wgs84.transform(
            snapshot_bbox[0] - 1,
            (snapshot_bbox[1] + snapshot_bbox[3]) / 2,
        )
        by_address_outside = client.get(
            "/api/v1/datasets/berlin_alkis_buildings/by-address",
            params={
                "snapshot_id": manifest.snapshot_id,
                "street": street,
                "house_number": number,
                "lon": outside_lon,
                "lat": outside_lat,
            },
        )
        assert by_address_outside.status_code == 200, by_address_outside.text
        assert by_address_outside.json()["coverage_partial"] is True
        assert (
            "outside imported extent; absence of a building is not evidence"
            in by_address_outside.json()["warnings"]
        )

        invalid_number = client.get(
            "/api/v1/datasets/berlin_alkis_buildings/by-address",
            params={
                "snapshot_id": manifest.snapshot_id,
                "street": street,
                "house_number": "abc",
            },
        )
        assert invalid_number.status_code == 400
        assert invalid_number.json()["error"]["code"] == "INVALID_FILTER"
        invalid_suffix = client.get(
            "/api/v1/datasets/berlin_alkis_buildings/by-address",
            params={
                "snapshot_id": manifest.snapshot_id,
                "street": street,
                "house_number": number,
                "house_number_suffix": "!",
            },
        )
        assert invalid_suffix.status_code == 400
        assert invalid_suffix.json()["error"]["code"] == "INVALID_FILTER"

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
        assert unpaired.status_code == 400

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

    changed_bbox = [
        snapshot_bbox[0] + 100,
        snapshot_bbox[1] + 100,
        snapshot_bbox[2] + 100,
        snapshot_bbox[3] + 100,
    ]
    values["parser_options"]["extent"]["name"] = "current_config_extent"
    values["parser_options"]["extent"]["bbox_25833"] = changed_bbox
    parts = urlsplit(values["resource_url"])
    params = parse_qsl(parts.query, keep_blank_values=True)
    changed_filter = (
        f"bezeich='AX_Gebaeude' AND BBOX(geom,{','.join(str(value) for value in changed_bbox)})"
    )
    params = [
        (key, changed_filter if key.upper() == "CQL_FILTER" else value) for key, value in params
    ]
    values["resource_url"] = urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(params), parts.fragment)
    )
    values["source_edition"] = "current-config-edition"
    values["license_id"] = "CURRENT-CONFIG-LICENSE"
    values["attribution"] = "Current config attribution"
    (config_dir / "berlin_alkis_buildings.json").write_text(json.dumps(values))
    changed_service = DataService(tmp_path / "data", config_dir)
    with TestClient(create_app(service=changed_service)) as client:
        historical = client.get(
            "/api/v1/datasets/berlin_alkis_buildings/at",
            params={
                "snapshot_id": manifest.snapshot_id,
                "lon": lon,
                "lat": lat,
                "radius_m": 0,
            },
        )
        assert historical.status_code == 200, historical.text
        historical_body = historical.json()
        assert historical_body["extent_coverage"]["bbox_25833"] == snapshot_bbox
        assert historical_body["extent_coverage"]["name"] is None
        assert historical_body["source_edition"] == manifest.source_edition
        assert historical_body["license_id"] == manifest.license_id
        assert historical_body["attribution"] == manifest.attribution

        unreconstructable_manifest = replace(
            manifest,
            raw_assets=tuple(
                replace(asset, resource_url="https://gdi.berlin.de/services/wfs/alkis_gebaeude")
                for asset in manifest.raw_assets
            ),
        )
        unreconstructable = _metadata(
            changed_service._config("berlin_alkis_buildings"), unreconstructable_manifest
        )
        assert unreconstructable["extent_coverage"] is None
        assert unreconstructable["complete"] is False
        assert "extent of this snapshot cannot be reconstructed" in unreconstructable["warnings"]
