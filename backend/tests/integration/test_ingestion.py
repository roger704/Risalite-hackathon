"""Offline end-to-end checks for publication, cache identity, and boundary joins."""

import json
from dataclasses import replace
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from app.application.service import DataService
from app.domain import ConnectorError, IncompatibleSnapshotError, NotFoundError, SchemaError

BACKEND = Path(__file__).parents[2]


@pytest.fixture
def setup(tmp_path):
    configs = tmp_path / "config"
    configs.mkdir()
    for name in ("berlin_lor", "berlin_population"):
        values = json.loads((BACKEND / f"config/datasets/{name}.json").read_text())
        values.update(
            resource_url=f"https://example.org/synthetic/{name}", allowed_hosts=["example.org"]
        )
        if name == "berlin_lor":
            values["source_crs"] = "EPSG:4326"
            values["parser_options"].update(expected_rows=2, name_field="PLR_NAME")
        else:
            values["parser_options"] = {
                "format": "csv",
                "encoding": "utf-8",
                "delimiter": ";",
                "area_code_columns": ["LOR"],
                "area_code_widths": [8],
                "hierarchy": "planungsraum",
                "expected_area_count": 2,
                "sentinels": {"•": "suppressed"},
                "metrics": [
                    {"column": "Total", "metric_code": "population_total", "unit": "persons"}
                ],
            }
        (configs / f"{name}.json").write_text(json.dumps(values))
    population = tmp_path / "population.csv"
    population.write_text("LOR;Total\n01011101;0\n01011102;•\n")
    lor = tmp_path / "areas.geojson"
    lor.write_bytes((BACKEND / "tests/fixtures/lor/areas.geojson").read_bytes())
    return DataService(tmp_path / "data", configs), lor, population


async def test_publishes_geoparquet_provenance_and_compatible_population(setup):
    service, lor, population = setup
    parent = await service.ingest("berlin_lor", local_file=lor)
    result = await service.ingest(
        "berlin_population", local_file=population, parent_snapshot_id=parent.snapshot_id
    )
    assert result.parent_snapshot_ids == (parent.snapshot_id,)
    assert result.coverage["area_count"] == 2
    assert result.coverage["acquisition_mode"] == "local_file"
    assert result.quality_report.valid
    assert "geo".encode() in pq.read_metadata(service.root / parent.normalized_assets[0]).metadata
    assert all((service.root / asset.path).is_file() for asset in result.raw_assets)
    page = service.records("berlin_population", result.snapshot_id, limit=1)
    assert page["records"][0]["value"] == 0
    assert page["records"][0]["value_status"] == "observed"
    next_page = service.records("berlin_population", result.snapshot_id, cursor=page["next_cursor"])
    assert next_page["records"][0]["value"] is None
    assert next_page["records"][0]["value_status"] == "suppressed"


async def test_identical_input_and_configuration_reuse_snapshot(setup):
    service, lor, _ = setup
    first = await service.ingest("berlin_lor", local_file=lor)
    second = await service.ingest("berlin_lor", local_file=lor)
    assert first.snapshot_id == second.snapshot_id
    assert first.retrieved_at == second.retrieved_at
    with service.catalog.connect() as connection:
        assert connection.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 1
        assert (
            connection.execute("SELECT count(*) FROM jobs WHERE status='succeeded'").fetchone()[0]
            == 2
        )


async def test_failed_refresh_preserves_active_snapshot_and_quarantines_failure(setup):
    service, lor, _ = setup
    first = await service.ingest("berlin_lor", local_file=lor)
    invalid = json.loads(lor.read_text())
    invalid["features"].pop()
    lor.write_text(json.dumps(invalid))
    with pytest.raises(SchemaError):
        await service.ingest("berlin_lor", local_file=lor)
    assert service.catalog.get("berlin_lor").snapshot_id == first.snapshot_id
    assert service.records("berlin_lor", first.snapshot_id)["count"] == 2
    assert list((service.root / "staging").glob("*/failure.json"))
    with service.catalog.connect() as connection:
        assert (
            connection.execute("SELECT count(*) FROM jobs WHERE status='failed'").fetchone()[0] == 1
        )


async def test_population_requires_boundaries_and_exact_area_coverage(setup):
    service, lor, population = setup
    with pytest.raises(NotFoundError):
        await service.ingest("berlin_population", local_file=population)
    await service.ingest("berlin_lor", local_file=lor)
    population.write_text(population.read_text().replace("01011102", "01019999"))
    with pytest.raises(IncompatibleSnapshotError, match="1 missing areas, 1 unmatched"):
        await service.ingest("berlin_population", local_file=population)
    assert service.list_datasets()[1]["availability"] == "unavailable"


async def test_rejects_boundary_edition_mismatch(setup):
    service, lor, population = setup
    await service.ingest("berlin_lor", local_file=lor)
    service.configs["berlin_population"] = replace(
        service.configs["berlin_population"], boundary_edition="old"
    )
    with pytest.raises(IncompatibleSnapshotError, match="editions differ"):
        await service.ingest("berlin_population", local_file=population)


async def test_old_snapshot_queries_stay_pinned_and_new_parents_invalidate_cache(setup):
    service, lor, population = setup
    old_parent = await service.ingest("berlin_lor", local_file=lor)
    old_population = await service.ingest("berlin_population", local_file=population)
    source = json.loads(lor.read_text())
    source["features"][0]["properties"]["PLR_NAME"] = "Synthetic revised name"
    lor.write_text(json.dumps(source))
    new_parent = await service.ingest("berlin_lor", local_file=lor)
    new_population = await service.ingest("berlin_population", local_file=population)
    assert old_parent.snapshot_id != new_parent.snapshot_id
    assert old_population.snapshot_id != new_population.snapshot_id
    assert new_population.parent_snapshot_ids == (new_parent.snapshot_id,)
    old = service.manifest("berlin_population", old_population.snapshot_id)
    assert old["parent_snapshot_ids"] == [old_parent.snapshot_id]


async def test_query_cursors_cannot_cross_filters_or_snapshots(setup):
    service, lor, _ = setup
    snapshot = await service.ingest("berlin_lor", local_file=lor)
    cursor = service.records("berlin_lor", snapshot.snapshot_id, limit=1)["next_cursor"]
    with pytest.raises(ConnectorError, match="Invalid cursor"):
        service.records("berlin_lor", snapshot.snapshot_id, cursor=cursor, area_code="01011101")
    with pytest.raises(ConnectorError, match="Invalid cursor"):
        service.records("berlin_lor", snapshot.snapshot_id, cursor="invalid!")
    with pytest.raises(ConnectorError, match="unsupported"):
        service.records("berlin_lor", snapshot.snapshot_id, metric_code="population_total")


async def test_per_dataset_lock_rejects_concurrent_publication(setup):
    service, lor, _ = setup
    with service._lock("berlin_lor"):
        with pytest.raises(ConnectorError, match="already being ingested"):
            await service.ingest("berlin_lor", local_file=lor)


async def test_cleanup_failure_cannot_relabel_published_or_cached_import(setup, monkeypatch):
    service, lor, _ = setup
    real_cleanup = __import__("shutil").rmtree

    def fail_staging(path, *args, **kwargs):
        if Path(path).parent == service.root / "staging":
            raise OSError("simulated staging cleanup failure")
        return real_cleanup(path, *args, **kwargs)

    monkeypatch.setattr("app.application.service.shutil.rmtree", fail_staging)
    first = await service.ingest("berlin_lor", local_file=lor)
    second = await service.ingest("berlin_lor", local_file=lor)
    assert first.snapshot_id == second.snapshot_id
    assert service.catalog.get("berlin_lor").snapshot_id == first.snapshot_id
    with service.catalog.connect() as connection:
        jobs = connection.execute("SELECT status,snapshot_id FROM jobs").fetchall()
    assert all(
        row["status"] == "succeeded" and row["snapshot_id"] == first.snapshot_id for row in jobs
    )


async def test_missing_snapshot_assets_report_unavailable_but_manifest_survives(setup):
    service, lor, _ = setup
    snapshot = await service.ingest("berlin_lor", local_file=lor)
    (service.root / snapshot.normalized_assets[0]).unlink()
    assert service.list_datasets()[0]["availability"] == "unavailable"
    assert (
        service.manifest("berlin_lor", snapshot.snapshot_id)["snapshot_id"] == snapshot.snapshot_id
    )
    with pytest.raises(ConnectorError, match="asset is missing"):
        service.records("berlin_lor", snapshot.snapshot_id)


async def test_job_completion_failure_rolls_back_publication_transaction(setup, monkeypatch):
    import sqlite3

    service, lor, _ = setup
    original = await service.ingest("berlin_lor", local_file=lor)
    source = json.loads(lor.read_text())
    source["features"][0]["properties"]["PLR_NAME"] = "New synthetic name"
    lor.write_text(json.dumps(source))

    def fail_job_completion(connection, job_id, snapshot_id):
        raise sqlite3.OperationalError("simulated transaction failure")

    monkeypatch.setattr(service.catalog, "_complete_job", fail_job_completion)
    with pytest.raises(sqlite3.OperationalError):
        await service.ingest("berlin_lor", local_file=lor)
    assert service.catalog.get("berlin_lor").snapshot_id == original.snapshot_id
    with service.catalog.connect() as connection:
        assert connection.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 1
        assert (
            connection.execute("SELECT count(*) FROM jobs WHERE status='failed'").fetchone()[0] == 1
        )
