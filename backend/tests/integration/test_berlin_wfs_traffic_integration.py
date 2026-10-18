"""Mocked multi-page road traffic ingestion through the snapshot service."""

import json
from pathlib import Path

import httpx

from app.application.service import DataService
from app.connectors.berlin_wfs import BerlinWFSConnector
from app.infrastructure.http import HTTPFetcher
from app.infrastructure.storage import read_records

BACKEND = Path(__file__).parents[2]
FIXTURES = BACKEND / "tests/fixtures/berlin_wfs_traffic"
CONFIG = BACKEND / "config/datasets/berlin_traffic_volumes_2019.json"


async def test_publishes_paged_traffic_snapshot_and_reads_records(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    values = json.loads(CONFIG.read_text())
    values["parser_options"].pop("expected_count_range")
    values["parser_options"].update(page_size=2, max_pages=3, expected_count=3)
    (config_dir / "berlin_traffic_volumes_2019.json").write_text(json.dumps(values))
    requests = []

    async def handler(request):
        requests.append(request)
        start = int(request.url.params["STARTINDEX"])
        fixture = FIXTURES / f"page-{start // 2:04d}.json"
        return httpx.Response(
            200,
            headers={"content-type": "application/geo+json"},
            content=fixture.read_bytes(),
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = DataService(
            tmp_path / "data",
            config_dir,
            connectors={"berlin_wfs": BerlinWFSConnector(HTTPFetcher(client))},
        )
        manifest = await service.ingest("berlin_traffic_volumes_2019")

    assert [request.url.params["STARTINDEX"] for request in requests] == ["0", "2"]
    assert len(manifest.raw_assets) == 2
    assert manifest.parent_snapshot_ids == ()
    assert manifest.quality_report.valid
    assert manifest.quality_report.output_rows == 3
    assert manifest.quality_report.rejected_rows == 0
    assert any(
        "Total network length: 0.4 km" in warning for warning in manifest.quality_report.warnings
    )
    records = read_records(service.root / manifest.normalized_assets[0])
    assert len(records) == 3
    assert records[0].record_type == "road_traffic_segment"
    assert records[0].record_id == "road_segment:00000001_00000002"
    assert service.records("berlin_traffic_volumes_2019", manifest.snapshot_id)["count"] == 3
