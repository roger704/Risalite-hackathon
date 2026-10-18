"""ALKIS snapshot publication and local parcel APIs through DataService."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pyproj import Transformer
from shapely.geometry import Point, box, mapping, shape
from shapely.ops import transform, unary_union

from app.api.main import create_app
from app.application.service import DataService
from app.connectors.berlin_alkis_parcels import BerlinALKISParcelsConnector
from app.connectors.lor import LORConnector
from app.connectors.osm import OSMConnector
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
    osm = json.loads((BACKEND / "config/datasets/osm_pois_overpass.json").read_text())
    osm.update(
        resource_url="https://example.org/synthetic/osm",
        allowed_hosts=["example.org"],
        access_validation="validated",
    )
    osm["parser_options"].update(
        allowed_bbox=[13.0, 52.3, 13.9, 52.8],
        node_bbox=[13.0, 52.3, 13.9, 52.8],
        min_expected_counts={},
    )
    (configs / "berlin_lor.json").write_text(json.dumps(lor))
    (configs / "berlin_alkis_parcels.json").write_text(json.dumps(alkis))
    (configs / "osm_pois_overpass.json").write_text(json.dumps(osm))

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
            "osm": OSMConnector(),
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
            first["parcel_id"] in summary["parcel_ids"]
            for summary in body["summaries_by_related"].values()
        )


async def test_relation_summaries_include_all_intersected_parcels(tmp_path):
    service, parent_file, http_client = service_fixture(tmp_path)
    async with http_client:
        parent = await service.ingest("berlin_lor", local_file=parent_file)
        parcels = await service.ingest(
            "berlin_alkis_parcels", parent_snapshot_id=parent.snapshot_id
        )

    parcel_records = service.records("berlin_alkis_parcels", parcels.snapshot_id, limit=1000)[
        "records"
    ]
    parcel_by_id = {record["parcel_id"]: record for record in parcel_records}
    parcel_ids = ("11000192100498____", "11000192100499____")
    spanning_geometry = unary_union(
        [shape(parcel_by_id[parcel_id]["geometry"]) for parcel_id in parcel_ids]
    )
    related_source = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"PLR_ID": "01011101", "PLR_NAME": "Spanning fixture parcels"},
                "geometry": mapping(spanning_geometry),
            },
            {
                "type": "Feature",
                "properties": {"PLR_ID": "01011102", "PLR_NAME": "Synthetic elsewhere"},
                "geometry": mapping(box(13.7, 52.7, 13.71, 52.71)),
            },
        ],
    }
    related_file = tmp_path / "related-areas.geojson"
    related_file.write_text(json.dumps(related_source))
    related_manifest = await service.ingest("berlin_lor", local_file=related_file)

    api = TestClient(create_app(service=service))
    with api:
        response = api.get(
            f"/api/v1/parcels/{parcel_ids[0]}/relations",
            params={
                "snapshot_id": parcels.snapshot_id,
                "related_dataset_id": "berlin_lor",
                "related_snapshot_id": related_manifest.snapshot_id,
            },
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert [relation["parcel_id"] for relation in body["relations"]] == [parcel_ids[0]]
    related_record = service.records("berlin_lor", related_manifest.snapshot_id, limit=1000)[
        "records"
    ][0]["record_id"]
    assert body["summaries_by_related"][related_record] == {
        "parcel_ids": sorted(parcel_ids),
        "multiple_parcels": True,
    }
    assert body["touches_only"] == 0


async def test_relation_prefilter_keeps_points_just_outside_parcel_boundary(tmp_path):
    service, parent_file, http_client = service_fixture(tmp_path)
    async with http_client:
        parent = await service.ingest("berlin_lor", local_file=parent_file)
        parcels = await service.ingest(
            "berlin_alkis_parcels", parent_snapshot_id=parent.snapshot_id
        )

    parcel = next(
        record
        for record in service.records("berlin_alkis_parcels", parcels.snapshot_id, limit=1000)[
            "records"
        ]
        if record["parcel_id"] == "11000192100498____"
    )
    geometry_wgs84 = shape(parcel["geometry"])
    west, south, east, north = geometry_wgs84.bounds
    to_metric = Transformer.from_crs("EPSG:4326", "EPSG:25833", always_xy=True)
    to_wgs84 = Transformer.from_crs("EPSG:25833", "EPSG:4326", always_xy=True)
    geometry_metric = transform(to_metric.transform, geometry_wgs84)
    rightmost_wgs84 = max(
        (
            coordinate
            for polygon in (
                tuple(geometry_wgs84.geoms)
                if geometry_wgs84.geom_type == "MultiPolygon"
                else (geometry_wgs84,)
            )
            for coordinate in polygon.exterior.coords
        ),
        key=lambda coordinate: coordinate[0],
    )
    rightmost_metric = transform(to_metric.transform, Point(*rightmost_wgs84))
    point_metric = Point(rightmost_metric.x + 0.02, rightmost_metric.y)
    assert geometry_metric.distance(point_metric) == pytest.approx(0.02, abs=0.001)
    point_wgs84 = transform(to_wgs84.transform, point_metric)
    longitude, latitude = point_wgs84.x, point_wgs84.y
    assert not (west <= longitude <= east and south <= latitude <= north)

    related_file = tmp_path / "related-point.json"
    related_file.write_text(
        json.dumps(
            {
                "elements": [
                    {
                        "type": "node",
                        "id": 987654321,
                        "lon": longitude,
                        "lat": latitude,
                        "tags": {"amenity": "cafe", "name": "Boundary fixture"},
                    }
                ]
            }
        )
    )
    related_manifest = await service.ingest("osm_pois_overpass", local_file=related_file)

    api = TestClient(create_app(service=service))
    with api:
        response = api.get(
            f"/api/v1/parcels/{parcel['parcel_id']}/relations",
            params={
                "snapshot_id": parcels.snapshot_id,
                "related_dataset_id": "osm_pois_overpass",
                "related_snapshot_id": related_manifest.snapshot_id,
            },
        )
    assert response.status_code == 200, response.text
    relation_rows = response.json()["relations"]
    assert len(relation_rows) == 1
    assert relation_rows[0]["parcel_id"] == parcel["parcel_id"]
    assert relation_rows[0]["related_record_id"] == "node/987654321"
    assert relation_rows[0]["relation"] == "on_boundary"


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
