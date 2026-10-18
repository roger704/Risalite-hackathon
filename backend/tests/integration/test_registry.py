"""Lazy adapter loading and generic parent-snapshot plumbing."""

import json
from pathlib import Path

import pytest

from app.application.service import DataService
from app.connectors.registry import ADAPTERS, load_connector
from app.domain import ConfigError

BACKEND = Path(__file__).parents[2]


def test_registry_loads_implemented_and_rejects_missing_adapters(monkeypatch):
    assert type(load_connector("lor")).__name__ == "LORConnector"
    monkeypatch.setitem(ADAPTERS, "planned", "app.connectors.does_not_exist:Missing")
    with pytest.raises(ConfigError, match="not implemented"):
        load_connector("planned")
    with pytest.raises(ConfigError, match="not implemented"):
        load_connector("unknown")


def _config_dir(tmp_path, **lor_changes):
    configs = tmp_path / "config"
    configs.mkdir()
    values = json.loads((BACKEND / "config/datasets/berlin_lor.json").read_text())
    values.update(resource_url="https://example.org/a", allowed_hosts=["example.org"])
    values["parser_options"].update(lor_changes)
    (configs / "berlin_lor.json").write_text(json.dumps(values))
    return configs


async def test_parent_snapshot_rejected_without_parent_dataset(tmp_path):
    service = DataService(tmp_path / "data", _config_dir(tmp_path))
    with pytest.raises(ConfigError, match="parent snapshot"):
        await service.ingest("berlin_lor", parent_snapshot_id="x")


async def test_parent_dataset_requires_connector_hook(tmp_path):
    service = DataService(tmp_path / "data", _config_dir(tmp_path, parent_dataset="berlin_lor"))
    with pytest.raises(ConfigError, match="check_parent"):
        await service.ingest("berlin_lor")


class PointConnector:
    adapter_version = "test"

    async def fetch(self, request):  # pragma: no cover - local files only
        raise AssertionError

    def normalize(self, raw, config):
        from app.domain import NormalizedBatch, Place

        records = tuple(
            Place(
                source_id=config.dataset_id,
                snapshot_id=raw.snapshot_id,
                source_record_id=f"node/{index}",
                record_id=f"node/{index}",
                geometry={"type": "Point", "coordinates": [13.0 + index, 52.5]},
                source_geometry_type="node",
                original_category="amenity=cafe",
                category_mapping_version="test-1",
                area_code="01100101" if index else None,
            )
            for index in range(2)
        )
        return NormalizedBatch(records=records, input_rows=2)

    def validate(self, batch):
        from app.domain import QualityReport

        return QualityReport(valid=True, input_rows=2, output_rows=len(batch.records))


async def test_point_records_publish_as_geoparquet_with_bbox_and_area_filter(tmp_path):
    import pyarrow.parquet as pq

    configs = _config_dir(tmp_path)
    values = json.loads((configs / "berlin_lor.json").read_text())
    values.update(dataset_id="points", adapter="points", parser_options={})
    (configs / "berlin_lor.json").unlink()
    (configs / "points.json").write_text(json.dumps(values))
    source = tmp_path / "points.json"
    source.write_text("{}")
    service = DataService(tmp_path / "data", configs, connectors={"points": PointConnector()})
    manifest = await service.ingest("points", local_file=source)
    assert manifest.coverage["bbox"] == [13.0, 52.5, 14.0, 52.5]
    assert b"geo" in pq.read_metadata(service.root / manifest.normalized_assets[0]).metadata
    page = service.records("points", manifest.snapshot_id, area_code="01100101")
    assert [record["record_id"] for record in page["records"]] == ["node/1"]
