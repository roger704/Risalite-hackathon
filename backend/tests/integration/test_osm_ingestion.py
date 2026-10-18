"""Offline OSM ingestion publication check using a generated synthetic PBF."""

import json
from pathlib import Path

import osmium
import pyarrow.parquet as pq

from app.application.service import DataService

BACKEND = Path(__file__).parents[2]
FIXTURE = BACKEND / "tests/fixtures/osm/pois.osm"


def generated_pbf(tmp_path):
    target = tmp_path / "fixture.osm.pbf"
    writer = osmium.SimpleWriter(str(target))
    try:
        for obj in osmium.FileProcessor(FIXTURE):
            writer.add(obj)
    finally:
        writer.close()
    return target


async def test_publishes_osm_geoparquet_and_attribution(tmp_path):
    configs = tmp_path / "config"
    configs.mkdir()
    values = json.loads((BACKEND / "config/datasets/osm_pois.json").read_text())
    values.update(
        resource_url="https://example.org/synthetic/osm_pois",
        allowed_hosts=["example.org"],
    )
    values["parser_options"].update(
        min_expected_counts={"coffee_shop": 2},
        max_unlocatable_ratio=0.2,
    )
    (configs / "osm_pois.json").write_text(json.dumps(values))
    service = DataService(tmp_path / "data", configs)

    manifest = await service.ingest("osm_pois", local_file=generated_pbf(tmp_path))

    assert manifest.quality_report.valid
    assert manifest.quality_report.rejected_rows == 0
    assert manifest.coverage["record_count"] == 11
    assert manifest.coverage["acquisition_mode"] == "local_file"
    assert "OpenStreetMap contributors" in manifest.attribution
    parquet = service.root / manifest.normalized_assets[0]
    assert pq.read_table(parquet).num_rows == 11
