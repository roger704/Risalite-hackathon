"""IHK fixture through DataService and the HTTP records endpoint."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.application.service import DataService
from app.domain import SchemaError

BACKEND = Path(__file__).parents[2]
FIXTURES = BACKEND / "tests" / "fixtures"


@pytest.fixture
def service(tmp_path):
    configs = tmp_path / "config"
    configs.mkdir()
    lor = json.loads((BACKEND / "config/datasets/berlin_lor.json").read_text())
    lor.update(
        resource_url="https://example.org/synthetic/lor",
        allowed_hosts=["example.org"],
        source_crs="EPSG:4326",
    )
    lor["parser_options"].update(expected_rows=2, name_field="PLR_NAME")
    ihk = json.loads((BACKEND / "config/datasets/ihk_businesses.json").read_text())
    ihk["parser_options"].update(
        expected_sha256=None,
        min_expected_rows=None,
        max_excluded_row_fraction=0.2,
        parent_check={
            "max_missing_area_code_fraction": 0.5,
            "max_unmatched_area_code_fraction": 0.5,
            "max_outside_supplied_area_fraction": 0.5,
        },
    )
    (configs / "berlin_lor.json").write_text(json.dumps(lor))
    (configs / "ihk_businesses.json").write_text(json.dumps(ihk))
    return DataService(tmp_path / "data", configs)


async def test_business_pages_filter_by_area_and_serve_over_http(service):
    parent = await service.ingest("berlin_lor", local_file=FIXTURES / "lor/areas.geojson")
    snapshot = await service.ingest("ihk_businesses", local_file=FIXTURES / "ihk/businesses.csv")
    assert snapshot.parent_snapshot_ids == (parent.snapshot_id,)
    assert snapshot.coverage["outside_supplied_area_count"] == 1
    url = "/api/v1/datasets/ihk_businesses/records"
    query = {"snapshot_id": snapshot.snapshot_id, "area_code": "01011101", "limit": 2}
    with TestClient(create_app(service=service)) as client:
        first = client.get(url, params=query)
        assert first.status_code == 200, first.text
        body = first.json()
        assert [record["record_id"] for record in body["records"]] == ["1001", "1003"]
        assert body["records"][0]["record_type"] == "business"
        assert body["records"][0]["canonical_category"] == "coffee_shop"
        assert body["records"][0]["employees_min"] == 1
        assert body["records"][0]["area_code"] == "01011101"
        ids = [record["record_id"] for record in body["records"]]
        while body["next_cursor"]:
            body = client.get(url, params={**query, "cursor": body["next_cursor"]}).json()
            ids += [record["record_id"] for record in body["records"]]
    assert ids == ["1001", "1003", "1006", "1008", "1010"]


async def test_lfs_pointer_is_never_published(service):
    await service.ingest("berlin_lor", local_file=FIXTURES / "lor/areas.geojson")
    with pytest.raises(SchemaError, match="Git LFS pointer"):
        await service.ingest("ihk_businesses", local_file=FIXTURES / "ihk/lfs_pointer.csv")
    with service.catalog.connect() as connection:
        query = "SELECT count(*) FROM snapshots WHERE dataset_id='ihk_businesses'"
        assert connection.execute(query).fetchone()[0] == 0
