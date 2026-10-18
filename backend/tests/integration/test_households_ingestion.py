"""Offline publication, parent-join, query, and refresh checks."""

import json
from pathlib import Path

import pytest

from app.application.service import DataService
from app.domain import IncompatibleSnapshotError, NotFoundError, SchemaError
from tests.households_workbook import build_workbook

BACKEND = Path(__file__).parents[2]
LOR_CODES = ("01011101", "01011102")


@pytest.fixture
def setup(tmp_path):
    configs = tmp_path / "config"
    configs.mkdir()
    lor = json.loads((BACKEND / "config/datasets/berlin_lor.json").read_text())
    lor.update(
        resource_url="https://example.org/synthetic/berlin_lor",
        allowed_hosts=["example.org"],
        source_crs="EPSG:4326",
    )
    lor["parser_options"].update(expected_rows=2, name_field="PLR_NAME")
    (configs / "berlin_lor.json").write_text(json.dumps(lor))

    households = json.loads((BACKEND / "config/datasets/berlin_households.json").read_text())
    households.update(
        resource_url="https://example.org/synthetic/berlin_households",
        allowed_hosts=["example.org"],
    )
    households["parser_options"]["expected_area_count"] = len(LOR_CODES)
    (configs / "berlin_households.json").write_text(json.dumps(households))

    boundaries = tmp_path / "areas.geojson"
    boundaries.write_bytes((BACKEND / "tests/fixtures/lor/areas.geojson").read_bytes())
    workbook = build_workbook(tmp_path, codes=LOR_CODES)
    return DataService(tmp_path / "data", configs), boundaries, workbook


async def test_households_publishes_with_lor_parent_and_metric_filter(setup):
    service, boundaries, workbook = setup
    parent = await service.ingest("berlin_lor", local_file=boundaries)
    result = await service.ingest(
        "berlin_households", local_file=workbook, parent_snapshot_id=parent.snapshot_id
    )
    assert result.parent_snapshot_ids == (parent.snapshot_id,)
    assert result.quality_report.valid
    assert result.coverage["area_count"] == 2
    assert result.coverage["join_method"] == (
        "exact hierarchy, boundary scheme, and complete area-code set"
    )
    assert result.coverage["reference_period"] == "2025-12-31"
    assert result.coverage["limitations"] == [
        "Code compatibility does not certify unchanged polygon boundaries between source editions."
    ]
    assert any(
        "Reference date is 31 December 2025" in warning
        for warning in result.quality_report.warnings
    )
    page = service.records(
        "berlin_households",
        result.snapshot_id,
        metric_code="households_with_children_share",
    )
    assert page["count"] == 2
    assert all(record["reference_period"] == "2025-12-31" for record in page["records"])
    assert page["records"][0]["metric_code"] == "households_with_children_share"


async def test_households_ingest_requires_lor_parent(setup):
    service, _, workbook = setup
    with pytest.raises(NotFoundError):
        await service.ingest("berlin_households", local_file=workbook)


async def test_corrupt_household_refresh_preserves_active_snapshot(setup, tmp_path):
    service, boundaries, workbook = setup
    parent = await service.ingest("berlin_lor", local_file=boundaries)
    first = await service.ingest(
        "berlin_households", local_file=workbook, parent_snapshot_id=parent.snapshot_id
    )
    bad_workbook = build_workbook(
        tmp_path / "bad",
        codes=LOR_CODES,
        edits={"T1.2": {"B3": "Changed header"}},
    )
    with pytest.raises(SchemaError, match="T1.2!B3"):
        await service.ingest(
            "berlin_households",
            local_file=bad_workbook,
            parent_snapshot_id=parent.snapshot_id,
        )
    assert service.catalog.get("berlin_households").snapshot_id == first.snapshot_id
    assert service.records("berlin_households", first.snapshot_id)["count"] == 2 * 27


async def test_households_rejects_parent_area_set_mismatch(setup):
    service, boundaries, workbook = setup
    parent = await service.ingest("berlin_lor", local_file=boundaries)
    changed = build_workbook(
        workbook.parent,
        codes=("01011101", "01019999"),
    )
    with pytest.raises(IncompatibleSnapshotError, match="missing areas"):
        await service.ingest(
            "berlin_households",
            local_file=changed,
            parent_snapshot_id=parent.snapshot_id,
        )
