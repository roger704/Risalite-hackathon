"""Green-space ingestion, record reads and straight-line proximity through the real service."""

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.application.service import DataService
from app.connectors.berlin_green_spaces import BerlinGreenSpacesConnector
from app.connectors.lor import LORConnector
from app.domain import ConnectorError
from app.infrastructure.http import HTTPFetcher

BACKEND = Path(__file__).parents[2]
FIXTURES = BACKEND / "tests/fixtures/berlin_parks"


def setup_service(tmp_path, state):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    lor = json.loads((BACKEND / "config/datasets/berlin_lor.json").read_text())
    lor.update(
        resource_url="https://example.org/synthetic-lor",
        allowed_hosts=["example.org"],
        source_crs="EPSG:4326",
    )
    lor["parser_options"].update(expected_rows=2, name_field="PLR_NAME")
    parks = json.loads((BACKEND / "config/datasets/berlin_parks.json").read_text())
    parks["parser_options"]["page_size"] = 2
    for layer in parks["parser_options"]["layers"]:
        layer["expected_count_range"] = [1, 10]
    (config_dir / "berlin_lor.json").write_text(json.dumps(lor))
    (config_dir / "berlin_parks.json").write_text(json.dumps(parks))

    async def handler(request):
        layer = request.url.params["TYPENAMES"].split(":")[1]
        if state.get("fail") == layer:
            body = (BACKEND / "tests/fixtures/berlin_wfs/exception_report.xml").read_bytes()
        else:
            start = int(request.url.params["STARTINDEX"])
            body = (FIXTURES / f"{layer}-{start // 2:04d}.json").read_bytes()
        return httpx.Response(200, content=body, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = DataService(
        tmp_path / "data",
        config_dir,
        connectors={
            "lor": LORConnector(),
            "berlin_green_spaces": BerlinGreenSpacesConnector(HTTPFetcher(client)),
        },
    )
    return service, client


async def test_ingests_parks_against_pinned_lor_and_serves_records_and_proximity(tmp_path):
    state = {}
    service, client = setup_service(tmp_path, state)
    async with client:
        parent = await service.ingest(
            "berlin_lor", local_file=BACKEND / "tests/fixtures/lor/areas.geojson"
        )
        parks = await service.ingest("berlin_parks", parent_snapshot_id=parent.snapshot_id)

        assert parks.parent_snapshot_ids == (parent.snapshot_id,)
        assert len(parks.raw_assets) == 3
        assert parks.coverage["record_count"] == 5
        assert parks.coverage["feature_counts"] == {"green_space": 3, "playground": 2}
        assert parks.coverage["area_code_unmatched_codes"] == ["09999999"]
        assert parks.license_id == "DL-DE-ZERO-2.0" and parks.source_edition == "2026-04-09"
        assert any("not walking distance" in w for w in parks.quality_report.warnings)

        state["fail"] = "spielplaetze"
        with pytest.raises(ConnectorError):
            await service.ingest("berlin_parks", parent_snapshot_id=parent.snapshot_id)
    assert service.catalog.get("berlin_parks").snapshot_id == parks.snapshot_id

    with TestClient(create_app(service=service)) as api:
        records = api.get(
            "/api/v1/datasets/berlin_parks/records",
            params={"snapshot_id": parks.snapshot_id, "area_code": "01011101"},
        )
        assert records.status_code == 200, records.text
        records = records.json()
        assert {r["feature_type"] for r in records["records"]} == {"green_space", "playground"}
        assert all(r["geometry"]["type"] in {"Polygon", "MultiPolygon"} for r in records["records"])
        assert all(r["display_point"]["type"] == "Point" for r in records["records"])

        nearby = api.get(
            "/api/v1/datasets/berlin_parks/green-spaces/nearby",
            params={"snapshot_id": parks.snapshot_id, "lon": 13.353, "lat": 52.503},
        )
        assert nearby.status_code == 200
        body = nearby.json()
        assert body["distance_method"] == "straight_line_to_polygon_edge_epsg25833"
        assert "walking" not in json.dumps(body["results"]).lower()
        first = body["results"][0]
        assert first["record_id"] == "green_space:0000a001:00000001"
        assert first["contains_origin"] is True and first["straight_line_distance_m"] == 0
        assert first["adjacent_record_ids"] == ["playground:0000b001:00000001"]
        playground = next(r for r in body["results"] if r["feature_type"] == "playground")
        assert 100 < playground["straight_line_distance_m"] < 200
        assert body["counts_within_radius"] == {"green_space": 2, "playground": 1}
        assert "OSM" in body["source_scope"]
        assert body["attribution"][0]["license_id"] == "DL-DE-ZERO-2.0"

        only = api.get(
            "/api/v1/datasets/berlin_parks/green-spaces/nearby",
            params={
                "snapshot_id": parks.snapshot_id,
                "lon": 13.353,
                "lat": 52.503,
                "feature_type": "playground",
            },
        ).json()
        assert only["counts_within_radius"] == {"playground": 1}

        outside = api.get(
            "/api/v1/datasets/berlin_parks/green-spaces/nearby",
            params={"snapshot_id": parks.snapshot_id, "lon": 11.0, "lat": 48.0},
        )
        assert outside.status_code == 422
        misspelled = api.get(
            "/api/v1/datasets/berlin_parks/green-spaces/nearby",
            params={
                "snapshot_id": parks.snapshot_id,
                "lon": 13.353,
                "lat": 52.503,
                "feature_typ": "playground",
            },
        )
        assert misspelled.status_code == 422
        assert misspelled.json()["error"]["code"] == "UNSUPPORTED_FILTER"
        wrong = api.get(
            "/api/v1/datasets/berlin_lor/green-spaces/nearby",
            params={"snapshot_id": parent.snapshot_id, "lon": 13.353, "lat": 52.503},
        )
        assert wrong.status_code == 422
