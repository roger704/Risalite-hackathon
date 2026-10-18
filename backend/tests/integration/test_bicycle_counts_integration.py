"""Publication and API coverage for the synthetic bicycle-counts workbook."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.application.service import DataService
from app.domain import SchemaError

BACKEND = Path(__file__).parents[2]
CONFIGS = (
    "berlin_bicycle_counts",
    "berlin_population",
)
FIXTURE = BACKEND / "tests/fixtures/bicycle_counts/counts.xlsx"


def _service(tmp_path):
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    for dataset_id in CONFIGS:
        source = BACKEND / f"config/datasets/{dataset_id}.json"
        (config_dir / source.name).write_text(source.read_text())
    return DataService(tmp_path / "data", config_dir)


async def test_ingestion_records_and_counter_layer_api(tmp_path):
    service = _service(tmp_path)
    manifest = await service.ingest("berlin_bicycle_counts", local_file=FIXTURE)
    assert manifest.quality_report.valid
    assert manifest.quality_report.rejected_rows == 0
    assert manifest.coverage["record_count"] == manifest.quality_report.output_rows

    with TestClient(create_app(service=service)) as client:
        records = client.get(
            "/api/v1/datasets/berlin_bicycle_counts/records",
            params={"snapshot_id": manifest.snapshot_id, "limit": 1},
        )
        assert records.status_code == 200
        assert records.json()["count"] == 1
        assert records.json()["records"][0]["record_type"] == "mobility_observation"

        layer = client.get(
            "/api/v1/datasets/berlin_bicycle_counts/layers/counters",
            params={
                "snapshot_id": manifest.snapshot_id,
                "window_id": "all_hours",
                "year": 2025,
            },
        )
        assert layer.status_code == 200
        feature_collection = layer.json()
        assert feature_collection["snapshot_id"] == manifest.snapshot_id
        assert feature_collection["attribution"] == manifest.attribution
        assert len(feature_collection["features"]) == 5
        properties = feature_collection["features"][0]["properties"]
        assert properties["unit"] == "bicycles_per_hour"
        assert properties["window_label"] == "All hours"
        assert "not footfall or customers" in properties["label"]

        service.configs["berlin_bicycle_counts"].parser_options["windows"] = []
        historical_layer = client.get(
            "/api/v1/datasets/berlin_bicycle_counts/layers/counters",
            params={
                "snapshot_id": manifest.snapshot_id,
                "window_id": "all_hours",
                "year": 2025,
            },
        )
        assert historical_layer.status_code == 200
        assert historical_layer.json()["features"][0]["properties"]["window_label"] == "All hours"

        invalid_window = client.get(
            "/api/v1/datasets/berlin_bicycle_counts/layers/counters",
            params={
                "snapshot_id": manifest.snapshot_id,
                "window_id": "unknown",
                "year": 2025,
            },
        )
        assert invalid_window.status_code == 422
        assert invalid_window.json()["error"]["code"] == "INVALID_FILTER"

        invalid_dataset = client.get(
            "/api/v1/datasets/berlin_population/layers/counters",
            params={
                "snapshot_id": manifest.snapshot_id,
                "window_id": "all_hours",
                "year": 2025,
            },
        )
        assert invalid_dataset.status_code == 422
        assert invalid_dataset.json()["error"]["code"] == "INVALID_FILTER"

        unknown_filter = client.get(
            "/api/v1/datasets/berlin_bicycle_counts/layers/counters",
            params={
                "snapshot_id": manifest.snapshot_id,
                "window_id": "all_hours",
                "year": 2025,
                "unexpected": "value",
            },
        )
        assert unknown_filter.status_code == 422
        assert unknown_filter.json()["error"]["code"] == "INVALID_FILTER"

        missing_snapshot = client.get(
            "/api/v1/datasets/berlin_bicycle_counts/layers/counters",
            params={
                "snapshot_id": "missing-snapshot",
                "window_id": "all_hours",
                "year": 2025,
            },
        )
        assert missing_snapshot.status_code == 404


async def test_failed_refresh_keeps_previous_bicycle_snapshot_active(tmp_path):
    service = _service(tmp_path)
    first = await service.ingest("berlin_bicycle_counts", local_file=FIXTURE)
    broken = tmp_path / "broken.xlsx"
    broken.write_text("<html>not a workbook</html>")

    with pytest.raises(SchemaError):
        await service.ingest("berlin_bicycle_counts", local_file=broken)

    assert service.catalog.get("berlin_bicycle_counts").snapshot_id == first.snapshot_id
    assert service.catalog.get("berlin_bicycle_counts", first.snapshot_id).snapshot_id == (
        first.snapshot_id
    )
