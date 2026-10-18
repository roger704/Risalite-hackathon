"""GTFS publication through the shared snapshot service."""

import json
import zipfile
from pathlib import Path

from app.application.service import DataService

FIXTURES = Path(__file__).parents[1] / "fixtures" / "gtfs"
CONFIG_PATH = Path(__file__).parents[2] / "config" / "datasets" / "vbb_gtfs.json"


async def test_data_service_publishes_gtfs_records_manifest_dates_and_cache(tmp_path):
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    values = json.loads(CONFIG_PATH.read_text())
    values["parser_options"].update(valid_from="2026-11-01", valid_to="2026-11-30")
    (config_dir / "vbb_gtfs.json").write_text(json.dumps(values))
    source = tmp_path / "synthetic-gtfs.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for fixture in FIXTURES.glob("*.txt"):
            archive.write(fixture, fixture.name)
    service = DataService(tmp_path / "data", config_dir)

    manifest = await service.ingest("vbb_gtfs", local_file=source)
    assert (manifest.valid_from, manifest.valid_to) == ("2026-11-01", "2026-11-30")
    page = service.records("vbb_gtfs", manifest.snapshot_id, limit=100)
    assert {record["record_type"] for record in page["records"]} == {
        "transit_stop",
        "stop_service",
    }
    cached = await service.ingest("vbb_gtfs", local_file=source)
    assert cached.snapshot_id == manifest.snapshot_id
