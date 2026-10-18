"""ALKIS snapshot publication and local parcel APIs through DataService."""

import asyncio
import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from shapely.geometry import box, mapping, shape

from app.api.main import create_app
from app.application.service import DataService
from app.connectors.berlin_alkis_parcels import BerlinALKISParcelsConnector
from app.connectors.lor import LORConnector
from app.infrastructure.http import HTTPFetcher

BACKEND = Path(__file__).parents[2]
FIXTURES = BACKEND / "tests/fixtures/berlin_alkis_parcels"


def service_fixture(tmp_path):
    configs = tmp_path / "config"
    configs.mkdir()
    lor = json.loads((BACKEND / "config/datasets/berlin_lor.json").read_text())
    lor.update(
        resource_url="https://example.org/synthetic/lor",
        allowed_hosts=["example.org"],
        source_crs="EPSG:4326",
    )
    lor["parser_options"].update(expected_rows=2, name_field="PLR_NAME")
    alkis = json.loads((BACKEND / "config/datasets/berlin_alkis_parcels.json").read_text())
    alkis["parser_options"].update(
        page_size=6,
        max_pages=10,
        expected_count_range=[12, 12],
    )
    (configs / "berlin_lor.json").write_text(json.dumps(lor))
    (configs / "berlin_alkis_parcels.json").write_text(json.dumps(alkis))

    parent_geometry = mapping(box(13.38, 52.45, 13.46, 52.55))
    parent_source = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"PLR_ID": "01011101", "PLR_NAME": "Synthetic central"},
                "geometry": parent_geometry,
            },
            {
                "type": "Feature",
                "properties": {"PLR_ID": "01011102", "PLR_NAME": "Synthetic elsewhere"},
                "geometry": mapping(box(13.7, 52.7, 13.71, 52.71)),
            },
        ],
    }
    parent_file = tmp_path / "parent.geojson"
    parent_file.write_text(json.dumps(parent_source))

    async def handler(request):
        start = int(request.url.params["STARTINDEX"])
        fixture = FIXTURES / f"page-{start // 6:04d}.json"
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=fixture.read_bytes(),
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = DataService(
        tmp_path / "data",
        configs,
        connectors={
            "lor": LORConnector(),
            "berlin_alkis_parcels": BerlinALKISParcelsConnector(HTTPFetcher(client)),
        },
    )
    return service, parent_file, client


async def test_publishes_parcels_and_serves_pinned_spatial_apis(tmp_path):
    service, parent_file, http_client = service_fixture(tmp_path)
    async with http_client:
        parent = await service.ingest("berlin_lor", local_file=parent_file)
        parcels = await service.ingest(
            "berlin_alkis_parcels", parent_snapshot_id=parent.snapshot_id
        )

    assert parcels.parent_snapshot_ids == (parent.snapshot_id,)
    assert parcels.coverage["number_matched"] == 12
    assert parcels.coverage["lor_snapshot"] == parent.snapshot_id
    assert parcels.coverage["extent_complete"] is True
    records_page = service.records("berlin_alkis_parcels", parcels.snapshot_id, limit=1)
    assert records_page["records"][0]["record_type"] == "parcel"

    first = records_page["records"][0]
    polygon = shape(first["geometry"])
    point = polygon.representative_point()
    west, south, east, north = polygon.bounds
    api = TestClient(create_app(service=service))
    with api:
        response = api.get(
            "/api/v1/datasets/berlin_alkis_parcels/records",
            params={"snapshot_id": parcels.snapshot_id, "limit": 1},
        )
        assert response.status_code == 200, response.text
        assert response.json()["records"][0]["record_type"] == "parcel"

        at = api.get(
            "/api/v1/parcels/at",
            params={"snapshot_id": parcels.snapshot_id, "lon": point.x, "lat": point.y},
        )
        assert at.status_code == 200, at.text
        assert at.json()["match_count"] >= 1
        assert at.json()["meta"]["provenance"]["raw_asset_hashes_count"] == 2
        assert "do not imply development permission" in at.json()["meta"]["interpretation_guard"]

        bbox = api.get(
            "/api/v1/parcels/in-bbox",
            params={
                "snapshot_id": parcels.snapshot_id,
                "bbox": f"{west},{south},{east},{north}",
                "limit": 10,
            },
        )
        assert bbox.status_code == 200, bbox.text
        assert bbox.json()["type"] == "FeatureCollection"
        assert any(feature["id"] == first["record_id"] for feature in bbox.json()["features"])

        detail = api.get(
            f"/api/v1/parcels/{first['parcel_id']}",
            params={"snapshot_id": parcels.snapshot_id},
        )
        assert detail.status_code == 200, detail.text
        assert detail.json()["parcel"]["record_type"] == "parcel"
        assert detail.json()["parcel"]["cadastral_district_code"] == "0001"

        missing = api.get(
            "/api/v1/parcels/not-a-parcel",
            params={"snapshot_id": parcels.snapshot_id},
        )
        assert missing.status_code == 404
        unsupported = api.get(
            "/api/v1/parcels/at",
            params={
                "snapshot_id": parcels.snapshot_id,
                "lon": point.x,
                "lat": point.y,
                "bad_filter": "1",
            },
        )
        assert unsupported.status_code == 422

        relations = api.get(
            f"/api/v1/parcels/{first['parcel_id']}/relations",
            params={
                "snapshot_id": parcels.snapshot_id,
                "related_dataset_id": "berlin_lor",
                "related_snapshot_id": parent.snapshot_id,
            },
        )
        assert relations.status_code == 200, relations.text
        body = relations.json()
        assert body["pinned_inputs"]["parcels"] == parcels.snapshot_id
        assert body["pinned_inputs"]["related"]["snapshot_id"] == parent.snapshot_id
        assert body["relations"]
        assert body["relations"][0]["related_dataset_id"] == "berlin_lor"
        assert body["relations"][0]["crs"] == "EPSG:25833"
        assert all(relation["parcel_id"] == first["parcel_id"] for relation in body["relations"])
        assert all(
            summary["parcel_ids"] == [first["parcel_id"]]
            for summary in body["summaries_by_related"].values()
        )


def test_parcel_api_does_not_claim_legal_relationships_or_source_area(tmp_path):
    service, parent_file, http_client = service_fixture(tmp_path)

    async def publish():
        async with http_client:
            parent = await service.ingest("berlin_lor", local_file=parent_file)
            return await service.ingest(
                "berlin_alkis_parcels", parent_snapshot_id=parent.snapshot_id
            )

    manifest = asyncio.run(publish())
    api = TestClient(create_app(service=service))
    record = service.records("berlin_alkis_parcels", manifest.snapshot_id, limit=1)["records"][0]
    with api:
        response = api.get(
            f"/api/v1/parcels/{record['parcel_id']}",
            params={"snapshot_id": manifest.snapshot_id, "unsupported": "no"},
        )
    assert response.status_code == 422
