"""Berlin centers ingestion, parent coverage and local API behavior."""

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.application.service import DataService
from app.connectors.berlin_centers import BerlinCentersConnector
from app.connectors.lor import LORConnector
from app.domain import SchemaError
from app.infrastructure.http import HTTPFetcher

BACKEND = Path(__file__).parents[2]
FIXTURES = BACKEND / "tests/fixtures/berlin_centers"


def setup_service(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    lor = json.loads((BACKEND / "config/datasets/berlin_lor.json").read_text())
    lor["source_crs"] = "EPSG:4326"
    lor["parser_options"].update(expected_rows=3, name_field="PLR_NAME")
    centers = json.loads((BACKEND / "config/datasets/berlin_centers.json").read_text())
    centers["parser_options"]["page_size"] = 2
    centers["parser_options"]["layers"]["zh"]["expected_count_range"] = [3, 3]
    centers["parser_options"]["layers"]["fma"]["expected_count_range"] = [2, 2]
    (config_dir / "berlin_lor.json").write_text(json.dumps(lor))
    (config_dir / "berlin_centers.json").write_text(json.dumps(centers))

    polygon = [
        [13.0, 52.3],
        [13.9, 52.3],
        [13.9, 52.8],
        [13.0, 52.8],
        [13.0, 52.3],
    ]
    parent_path = tmp_path / "synthetic-lor.geojson"
    parent_path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {
                            "PLR_ID": f"0101110{index}",
                            "PLR_NAME": f"Synthetic PLR {index}",
                        },
                        "geometry": {"type": "Polygon", "coordinates": [polygon]},
                    }
                    for index in range(1, 4)
                ],
            }
        )
    )
    state = {"fail": False}

    async def handler(request):
        if state["fail"]:
            return httpx.Response(
                200,
                content=(
                    b'<ExceptionReport><Exception exceptionCode="InvalidParameterValue">'
                    b"<ExceptionText>synthetic source failure</ExceptionText></Exception></ExceptionReport>"
                ),
                request=request,
            )
        typename = request.url.params["TYPENAMES"]
        start = int(request.url.params["STARTINDEX"])
        name = (
            f"zh-page-{start // 2:04d}.json" if typename.endswith("_zh") else "fma-page-0000.json"
        )
        return httpx.Response(200, content=(FIXTURES / name).read_bytes(), request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = DataService(
        tmp_path / "data",
        config_dir,
        connectors={
            "lor": LORConnector(),
            "berlin_centers": BerlinCentersConnector(HTTPFetcher(client)),
        },
    )
    return service, parent_path, client, state


async def test_ingests_centers_checks_parent_and_serves_records_and_nearby(tmp_path):
    service, parent_path, client, _ = setup_service(tmp_path)
    async with client:
        parent = await service.ingest("berlin_lor", local_file=parent_path)
        centers = await service.ingest("berlin_centers", parent_snapshot_id=parent.snapshot_id)

    assert centers.parent_snapshot_ids == (parent.snapshot_id,)
    assert centers.coverage["parent_dataset"] == "berlin_lor"
    assert centers.coverage["centers_located_in_parent"] == 5
    assert centers.coverage["layer_counts"] == {
        "step_zen_2040:step_zen_2040_fma": 2,
        "step_zen_2040:step_zen_2040_zh": 3,
    }
    assert len(centers.raw_assets) == 3
    assert len({Path(asset.path).name for asset in centers.raw_assets}) == 3

    with TestClient(create_app(service=service)) as http:
        records = http.get(
            "/api/v1/datasets/berlin_centers/records",
            params={"snapshot_id": centers.snapshot_id, "limit": 100},
        )
        assert records.status_code == 200, records.text
        body = records.json()
        assert body["count"] == 5
        assert body["records"][0]["record_type"] == "retail_center"
        assert "center_category" in body["records"][0]
        assert "wfs_feature_id" in body["records"][0]

        nearby = http.get(
            "/api/v1/datasets/berlin_centers/nearby",
            params={
                "snapshot_id": centers.snapshot_id,
                "lon": 13.4132,
                "lat": 52.5219,
                "radius_m": 1500,
            },
        )
        assert nearby.status_code == 200, nearby.text
        result = nearby.json()
        assert result["candidate"] == {"lon": 13.4132, "lat": 52.5219}
        assert "Z40F1" in result["inside_boundary_ids"]
        assert result["context_notice"]
        assert result["attribution"]
        assert result["license"] == "dl-de-zero-2.0"
        assert all("distance_m" in item for item in result["centers"])

        for params in (
            {"snapshot_id": centers.snapshot_id, "lon": 12.0, "lat": 52.5},
            {"snapshot_id": centers.snapshot_id, "lon": 13.4, "lat": 53.0},
            {"snapshot_id": centers.snapshot_id, "lon": 13.4, "lat": 52.5, "radius_m": 0},
            {
                "snapshot_id": centers.snapshot_id,
                "lon": 13.4,
                "lat": 52.5,
                "unknown": "1",
            },
        ):
            response = http.get("/api/v1/datasets/berlin_centers/nearby", params=params)
            assert 400 <= response.status_code < 500


async def test_failed_refresh_keeps_previous_snapshot_latest_and_readable(tmp_path):
    service, parent_path, client, state = setup_service(tmp_path)
    async with client:
        parent = await service.ingest("berlin_lor", local_file=parent_path)
        centers = await service.ingest("berlin_centers", parent_snapshot_id=parent.snapshot_id)
        state["fail"] = True
        with pytest.raises(SchemaError, match="ExceptionReport"):
            await service.ingest("berlin_centers")

    latest = service.catalog.get("berlin_centers")
    assert latest.snapshot_id == centers.snapshot_id
    page = service.records("berlin_centers", centers.snapshot_id, limit=100)
    assert page["count"] == 5
    assert page["records"][0]["record_type"] == "retail_center"
