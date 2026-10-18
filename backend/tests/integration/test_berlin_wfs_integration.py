"""WFS and pinned-area ingestion through the real snapshot service."""

import json
from pathlib import Path

import httpx
import pytest

from app.application.area_names import area_names
from app.application.service import DataService
from app.connectors.berlin_wfs import BerlinWFSConnector
from app.connectors.lor import LORConnector
from app.domain import Area, IncompatibleSnapshotError, record_to_dict
from app.infrastructure.http import HTTPFetcher

BACKEND = Path(__file__).parents[2]
WFS_FIXTURES = BACKEND / "tests/fixtures/berlin_wfs"


def setup_service(tmp_path, parent_code="01011103"):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    lor = json.loads((BACKEND / "config/datasets/berlin_lor.json").read_text())
    lor.update(
        resource_url="https://example.org/synthetic-lor",
        allowed_hosts=["example.org"],
        source_crs="EPSG:4326",
    )
    lor["parser_options"].update(expected_rows=3, name_field="PLR_NAME")
    wfs = json.loads((BACKEND / "config/datasets/berlin_lor_names.json").read_text())
    wfs["parser_options"].update(page_size=2, max_pages=10, expected_count=3)
    (config_dir / "berlin_lor.json").write_text(json.dumps(lor))
    (config_dir / "berlin_lor_names.json").write_text(json.dumps(wfs))

    parent_source = json.loads((BACKEND / "tests/fixtures/lor/areas.geojson").read_text())
    third = json.loads(json.dumps(parent_source["features"][1]))
    third["properties"]["PLR_ID"] = parent_code
    third["properties"]["PLR_NAME"] = "Synthetic parent area"
    parent_source["features"].append(third)
    source_path = tmp_path / "parent.geojson"
    source_path.write_text(json.dumps(parent_source))

    async def handler(request):
        start = int(request.url.params["STARTINDEX"])
        fixture = WFS_FIXTURES / f"page-{start // 2:04d}.json"
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=fixture.read_bytes(),
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = DataService(
        tmp_path / "data",
        config_dir,
        connectors={
            "lor": LORConnector(),
            "berlin_wfs": BerlinWFSConnector(HTTPFetcher(client)),
        },
    )
    return service, source_path, client


async def test_ingests_paged_names_against_pinned_lor_and_supports_lookup(tmp_path):
    service, parent_file, client = setup_service(tmp_path)
    async with client:
        parent = await service.ingest("berlin_lor", local_file=parent_file)
        names = await service.ingest("berlin_lor_names", parent_snapshot_id=parent.snapshot_id)

    assert len(names.raw_assets) == 2
    assert all(len(asset.sha256) == 64 for asset in names.raw_assets)
    assert names.parent_snapshot_ids == (parent.snapshot_id,)
    assert names.coverage["geometry_comparison"]["crs"] == "EPSG:25833"
    assert names.coverage["parent_dataset"] == "berlin_lor"
    values = area_names(service, names.snapshot_id)
    assert values["01011101"]["name"] == "Stülerstraße"
    assert values["01011101"]["bzr_name"] == "Synthetic Süd"
    assert values["01011103"]["name"] == "Synthetic Park"


async def test_rejects_wfs_ingestion_with_mismatched_parent_codes(tmp_path):
    service, parent_file, client = setup_service(tmp_path, parent_code="01011104")
    async with client:
        parent = await service.ingest("berlin_lor", local_file=parent_file)
        with pytest.raises(IncompatibleSnapshotError) as error:
            await service.ingest("berlin_lor_names", parent_snapshot_id=parent.snapshot_id)
    assert error.value.code == "SNAPSHOT_INCOMPATIBLE"


def test_area_names_prefers_official_name_over_conflicting_attribute():
    area = Area(
        source_id="berlin_lor_names",
        snapshot_id="names-snapshot",
        source_record_id="01100101",
        record_id="planungsraum:01100101",
        geometry={
            "type": "Polygon",
            "coordinates": [[[13.4, 52.5], [13.4, 52.6], [13.5, 52.5], [13.4, 52.5]]],
        },
        area_code="01100101",
        name="Official name",
        hierarchy="planungsraum",
        boundary_edition="2021",
        attributes={"name": "other"},
    )

    class RecordService:
        def records(self, dataset_id, snapshot_id, limit, cursor):
            return {"records": [record_to_dict(area)], "next_cursor": None}

    assert area_names(RecordService(), area.snapshot_id) == {
        area.area_code: {"name": "Official name"}
    }
