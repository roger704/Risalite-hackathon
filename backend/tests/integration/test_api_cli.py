"""Offline HTTP/CLI boundary tests: snapshot pinning, filters and failures."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.cli import main as cli
from app.domain import (
    AreaObservation,
    ConnectorError,
    QualityReport,
    RawAsset,
    SnapshotManifest,
    manifest_to_dict,
    record_to_dict,
)


def sample_manifest():
    return SnapshotManifest(
        dataset_id="berlin_population",
        snapshot_id="population-example",
        adapter_version="1.0",
        landing_url="https://example.org",
        resource_url="https://example.org/population.csv",
        retrieved_at=datetime(2026, 10, 18, tzinfo=timezone.utc),
        raw_assets=(
            RawAsset(
                path="raw/example.csv",
                sha256="a" * 64,
                size_bytes=5,
                resource_url="https://example.org/population.csv",
            ),
        ),
        request_fingerprint="b" * 64,
        parser_config_hash="c" * 64,
        quality_report=QualityReport(valid=True, input_rows=1, output_rows=1),
        license_id="CC0-1.0",
        license_url="https://example.org/license",
        attribution="Fixture",
        reference_period="2025-12-31",
        boundary_edition="2021",
    )


class LocalService:
    """Fail if a public route unexpectedly acquires a source."""

    def __init__(self, **kwargs):
        self.options = kwargs
        self.reads = []
        self.ingestions = []

    def list_datasets(self):
        return [
            {
                "dataset_id": "berlin_population",
                "availability": "ready",
                "access_validation": "unvalidated",
            }
        ]

    def manifest(self, dataset_id, snapshot_id):
        if snapshot_id != "population-example":
            raise ConnectorError("SNAPSHOT_NOT_FOUND", "Unknown snapshot")
        return manifest_to_dict(sample_manifest())

    def records(self, dataset_id, snapshot_id, **filters):
        self.reads.append((dataset_id, snapshot_id, filters))
        record = AreaObservation(
            source_id=dataset_id,
            snapshot_id=snapshot_id,
            source_record_id="01010101",
            record_id="01010101/population_total",
            area_code="01010101",
            hierarchy="planungsraum",
            boundary_edition="2021",
            metric_code="population_total",
            value=None,
            value_status="suppressed",
            unit="persons",
            reference_period="2025-12-31",
        )
        return {
            "dataset_id": dataset_id,
            "snapshot_id": snapshot_id,
            "records": [record_to_dict(record)],
            "count": 1,
            "next_cursor": None,
            "coverage": {"limitations": ["Synthetic fixture"]},
        }

    async def ingest(self, dataset_id, **kwargs):
        self.ingestions.append((dataset_id, kwargs))
        return sample_manifest()


def test_http_reads_preserve_pinned_snapshot_and_suppression():
    service = LocalService()
    with TestClient(create_app(service=service)) as client:
        assert client.get("/health").json() == {"status": "ok"}
        datasets = client.get("/api/v1/datasets").json()
        assert datasets[0]["access_validation"] == "unvalidated"
        response = client.get(
            "/api/v1/datasets/berlin_population/records",
            params={
                "snapshot_id": "population-example",
                "area_code": "01010101",
                "limit": 1,
            },
        )
        assert response.status_code == 200
        record = response.json()["records"][0]
        assert record["area_code"] == "01010101"
        assert record["value"] is None
        assert record["value_status"] == "suppressed"
        assert service.reads[0][1] == "population-example"
        manifest = client.get("/api/v1/datasets/berlin_population/snapshots/population-example")
        assert manifest.status_code == 200
        assert manifest.json()["reference_period"] == "2025-12-31"
        assert service.ingestions == []


@pytest.mark.parametrize(
    "query",
    [
        {},
        {"snapshot_id": "s", "limit": "0"},
        {"snapshot_id": "s", "limit": "1001"},
        {"snapshot_id": "s", "unsupported": "1"},
    ],
)
def test_unsupported_or_unbounded_queries_never_reach_storage(query):
    service = LocalService()
    with TestClient(create_app(service=service)) as client:
        response = client.get("/api/v1/datasets/berlin_population/records", params=query)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["request_id"] == response.headers["X-Request-ID"]
    assert error["retryable"] is False
    assert service.reads == []


@pytest.mark.parametrize(
    "code,status",
    [
        ("SNAPSHOT_NOT_FOUND", 404),
        ("UNKNOWN_DATASET", 404),
        ("SNAPSHOT_INCOMPATIBLE", 409),
        ("SOURCE_UNAVAILABLE", 503),
    ],
)
def test_domain_failures_use_stable_http_error_envelopes(code, status):
    class FailingService(LocalService):
        def records(self, *args, **kwargs):
            raise ConnectorError(code, "Example failure", retryable=code == "SOURCE_UNAVAILABLE")

    with TestClient(create_app(service=FailingService())) as client:
        response = client.get(
            "/api/v1/datasets/berlin_population/records", params={"snapshot_id": "s"}
        )
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert response.json()["error"]["dataset_id"] == "berlin_population"


def test_cli_ingestion_passes_explicit_local_file_and_boundary_pin(monkeypatch, tmp_path, capsys):
    service = LocalService()
    monkeypatch.setattr(cli, "DataService", lambda **kwargs: service)
    result = cli.main(
        [
            "--data-dir",
            str(tmp_path),
            "ingest",
            "berlin_population",
            "--file",
            "fixture.csv",
            "--lor-snapshot",
            "lor-pinned",
        ]
    )
    assert result == 0
    assert service.ingestions == [
        (
            "berlin_population",
            {
                "local_file": Path("fixture.csv"),
                "parent_snapshot_id": "lor-pinned",
            },
        )
    ]
    output = json.loads(capsys.readouterr().out)
    assert output["snapshot_id"] == "population-example"


def test_cli_failures_are_machine_readable_and_do_not_print_tracebacks(monkeypatch, capsys):
    monkeypatch.setattr(cli, "DataService", LocalService)
    assert cli.main(["manifest", "berlin_population", "missing"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["error"]["code"] == "SNAPSHOT_NOT_FOUND"
    assert "Traceback" not in captured.err


def test_cli_environment_directory_and_record_filters(monkeypatch, tmp_path, capsys):
    services = []

    def service_factory(**kwargs):
        service = LocalService(**kwargs)
        services.append(service)
        return service

    monkeypatch.setattr(cli, "DataService", service_factory)
    monkeypatch.setenv("LOCATION_DATA_DIR", str(tmp_path))
    assert (
        cli.main(
            [
                "records",
                "berlin_population",
                "--snapshot",
                "population-example",
                "--area-code",
                "01010101",
                "--metric-code",
                "population_total",
                "--limit",
                "2",
            ]
        )
        == 0
    )
    assert services[0].options["data_dir"] == tmp_path
    assert services[0].reads[0][2]["area_code"] == "01010101"
    assert json.loads(capsys.readouterr().out)["count"] == 1


def test_real_offline_cli_ingestion_is_readable_as_typed_http_polygon(tmp_path, capsys):
    backend = Path(__file__).parents[2]
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config = json.loads((backend / "config/datasets/berlin_lor.json").read_text())
    config["source_crs"] = "EPSG:4326"
    config["source_edition"] = "synthetic-http-test"
    config["parser_options"].update(expected_rows=2, name_field="PLR_NAME")
    (config_dir / "berlin_lor.json").write_text(json.dumps(config))
    data_dir = tmp_path / "data"
    fixture = backend / "tests/fixtures/lor/areas.geojson"
    assert (
        cli.main(
            [
                "--data-dir",
                str(data_dir),
                "--config-dir",
                str(config_dir),
                "ingest",
                "berlin_lor",
                "--file",
                str(fixture),
            ]
        )
        == 0
    )
    manifest = json.loads(capsys.readouterr().out)
    snapshot = manifest["snapshot_id"]
    with TestClient(create_app(data_dir=data_dir, config_dir=config_dir)) as client:
        result = client.get(
            "/api/v1/datasets/berlin_lor/records",
            params={
                "snapshot_id": snapshot,
                "area_code": "01011101",
            },
        )
        assert result.status_code == 200
        payload = result.json()
        assert payload["snapshot_id"] == snapshot
        assert payload["count"] == 1
        assert payload["records"][0]["record_type"] == "area"
        assert payload["records"][0]["geometry"]["type"] == "Polygon"
        assert payload["coverage"]["extraction_complete"] is True
        assert payload["attribution"][0]["license_id"] == config["license_id"]
        read_manifest = client.get(f"/api/v1/datasets/berlin_lor/snapshots/{snapshot}")
        assert read_manifest.status_code == 200
        assert (
            read_manifest.json()["raw_assets"][0]["sha256"] == manifest["raw_assets"][0]["sha256"]
        )
