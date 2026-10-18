"""Paged WFS ingestion with a pinned synthetic LOR parent snapshot."""

import json
from pathlib import Path

import httpx

from app.application.population_blocks import block_aggregates
from app.application.service import DataService
from app.connectors.berlin_wfs import BerlinWFSConnector
from app.connectors.lor import LORConnector
from app.infrastructure.http import HTTPFetcher

BACKEND = Path(__file__).parents[2]
WFS_FIXTURES = BACKEND / "tests/fixtures/berlin_wfs_population_blocks"


def setup_service(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    lor = json.loads((BACKEND / "config/datasets/berlin_lor.json").read_text())
    lor.update(source_crs="EPSG:4326")
    lor["parser_options"].update(expected_rows=2, name_field="PLR_NAME")
    blocks = json.loads(
        (BACKEND / "config/datasets/berlin_population_density_blocks.json").read_text()
    )
    blocks["parser_options"].update(
        page_size=3,
        max_pages=10,
        expected_count_range=[8, 8],
        max_unassigned_share=0.5,
    )
    (config_dir / "berlin_lor.json").write_text(json.dumps(lor))
    (config_dir / "berlin_population_density_blocks.json").write_text(json.dumps(blocks))

    async def handler(request):
        start = int(request.url.params["STARTINDEX"])
        fixture = WFS_FIXTURES / f"page-{start // 3:04d}.json"
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
    return service, BACKEND / "tests/fixtures/lor/areas.geojson", client


async def test_ingests_population_blocks_with_parent_coverage_and_aggregation(tmp_path):
    service, parent_file, client = setup_service(tmp_path)
    async with client:
        parent = await service.ingest("berlin_lor", local_file=parent_file)
        snapshot = await service.ingest(
            "berlin_population_density_blocks", parent_snapshot_id=parent.snapshot_id
        )

    assert len(snapshot.raw_assets) == 3
    assert snapshot.parent_snapshot_ids == (parent.snapshot_id,)
    assert snapshot.coverage["parent_dataset"] == "berlin_lor"
    assignment = snapshot.coverage["assignment"]
    assert assignment["assigned"] == 7
    assert assignment["unassigned"] == 1
    assert assignment["ambiguous"] == 1
    assert assignment["partial"] == 2
    records = service.records(
        "berlin_population_density_blocks",
        snapshot.snapshot_id,
        area_code="01011101",
        limit=1000,
    )["records"]
    assert {record["block_id"] for record in records} == {
        "2023000000000001",
        "2023000000000003",
        "2023000000000004",
        "2023000000000006",
        "2023000000000008",
    }
    aggregates = block_aggregates(service, snapshot.snapshot_id)
    alpha = aggregates.areas["01011101"]
    assert alpha.block_count == 5
    assert alpha.residents_observed == 270
    assert alpha.residents_upper_bound == 272
    assert aggregates.unassigned_block_count == 1
    assert aggregates.unassigned_residents_observed == 50
