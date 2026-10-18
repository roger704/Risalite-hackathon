"""Protect meanings that must survive parser, disk, and API boundaries."""

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.domain import (
    Area,
    AreaObservation,
    ConfigError,
    DatasetConfig,
    QualityReport,
    RawAsset,
    RawBundle,
    SnapshotManifest,
    ValidationError,
    ValueStatus,
    manifest_from_dict,
    manifest_to_dict,
    record_from_dict,
    record_to_dict,
)


def observation(**changes):
    defaults = dict(
        source_id="berlin_population",
        snapshot_id="population-2025",
        source_record_id="01010101/total",
        record_id="01010101/population/total",
        area_code="01010101",
        hierarchy="planungsraum",
        boundary_edition="2021",
        metric_code="population_total",
        value=0,
        unit="persons",
        reference_period="2025-12-31",
        value_status="observed",
    )
    return AreaObservation(**(defaults | changes))


def test_zero_and_suppression_survive_json_round_trip_without_losing_leading_zero():
    zero = observation()
    suppressed = observation(value=None, value_status="suppressed")
    for record in (zero, suppressed):
        encoded = json.loads(json.dumps(record_to_dict(record), allow_nan=False))
        decoded = record_from_dict(encoded)
        assert decoded == record
        assert decoded.area_code == "01010101"
    assert record_to_dict(zero)["value"] == 0
    assert record_to_dict(suppressed)["value"] is None
    assert suppressed.value_status is ValueStatus.SUPPRESSED


@pytest.mark.parametrize(
    "status,value",
    [
        ("suppressed", 0),
        ("missing", 42),
        ("not_applicable", 0),
        ("observed", None),
        ("estimated", float("nan")),
        ("forecast", float("inf")),
        ("observed", True),
    ],
)
def test_unknown_or_suppressed_values_cannot_become_counts(status, value):
    with pytest.raises(ValidationError):
        observation(value_status=status, value=value)


@pytest.mark.parametrize(
    "changes",
    [
        {"area_code": 1010101},
        {"boundary_edition": ""},
        {"hierarchy": ""},
        {"age_min": 6, "age_max_exclusive": 6},
        {"age_min": -1},
        {"age_max_exclusive": 0},
        {"age_min": 1.5},
        {"reference_period": "2025-02-30"},
        {"schema_version": "2.0"},
    ],
)
def test_identity_period_and_age_semantics_are_checked(changes):
    with pytest.raises(ValidationError):
        observation(**changes)


def test_open_ended_age_band_is_preserved():
    oldest = observation(age_min=80, age_max_exclusive=None)
    assert record_from_dict(record_to_dict(oldest)) == oldest


def test_polygon_and_its_hierarchy_edition_survive_serialization():
    area = Area(
        source_id="berlin_lor",
        snapshot_id="lor-2021",
        source_record_id="01010101",
        record_id="01010101",
        area_code="01010101",
        name="Synthetic area",
        hierarchy="planungsraum",
        boundary_edition="2021",
        geometry={
            "type": "Polygon",
            "coordinates": [[[13.0, 52.0], [13.1, 52.0], [13.0, 52.1], [13.0, 52.0]]],
        },
    )
    assert record_from_dict(record_to_dict(area)) == area
    with pytest.raises(ValidationError, match="Polygon"):
        replace(area, geometry={"type": "Point", "coordinates": [13.0, 52.0]})


def test_manifest_preserves_retrieval_time_separately_from_source_period():
    retrieved = datetime(2026, 10, 18, 9, 30, tzinfo=timezone.utc)
    raw_asset = RawAsset(
        path="raw/population.csv",
        sha256="a" * 64,
        size_bytes=123,
        resource_url="https://example.org/population.csv",
        media_type="text/csv",
        etag='"revision-1"',
        last_modified="Mon, 01 Jun 2026 12:00:00 GMT",
    )
    manifest = SnapshotManifest(
        snapshot_id="population-2025",
        dataset_id="berlin_population",
        adapter_version="1.0",
        landing_url="https://example.org/",
        resource_url=raw_asset.resource_url,
        retrieved_at=retrieved,
        raw_assets=(raw_asset,),
        request_fingerprint="b" * 64,
        parser_config_hash="c" * 64,
        quality_report=QualityReport(valid=True, input_rows=1, output_rows=6),
        license_id="CC0-1.0",
        license_url="https://example.org/license",
        attribution="Example",
        boundary_edition="2021",
        reference_period="2025-12-31",
        normalized_assets=("normalized/population.parquet",),
        parent_snapshot_ids=("lor-2021",),
        coverage={"limitations": ["Synthetic fixture"]},
    )
    encoded = json.loads(json.dumps(manifest_to_dict(manifest), allow_nan=False))
    assert encoded["retrieved_at"] == "2026-10-18T09:30:00Z"
    assert encoded["reference_period"] == "2025-12-31"
    assert manifest_from_dict(encoded) == manifest
    with pytest.raises(ValidationError, match="valid_from"):
        replace(manifest, valid_from="2026-02-02", valid_to="2026-01-01")


@pytest.mark.parametrize(
    "timestamp",
    [
        datetime(2026, 10, 18),
        datetime(2026, 10, 18, tzinfo=timezone(timedelta(hours=2))),
    ],
)
def test_system_timestamps_must_be_aware_utc(timestamp):
    asset = RawAsset(
        path="raw.csv", sha256="a" * 64, size_bytes=0, resource_url="https://example.org/raw.csv"
    )
    with pytest.raises(ValidationError, match="UTC"):
        RawBundle(
            assets=(asset,),
            retrieved_at=timestamp,
            snapshot_id="example",
            request_fingerprint="hash",
        )


def test_unknown_record_types_and_errorful_success_reports_are_rejected():
    with pytest.raises(ValidationError, match="record_type"):
        record_from_dict({"record_type": "future_record"})
    with pytest.raises(ValidationError, match="errors"):
        QualityReport(valid=True, input_rows=1, output_rows=0, errors=("Geometry invalid",))


def dataset_config(**changes):
    values = dict(
        dataset_id="berlin_lor",
        provider="Fixture",
        adapter="lor",
        landing_url="https://example.org",
        resource_url="https://example.org/areas.geojson",
        allowed_hosts=("example.org",),
        license_id="CC0-1.0",
        license_url="https://example.org/license",
        attribution="Synthetic",
        boundary_edition="2021",
    )
    return DatasetConfig(**(values | changes))


@pytest.mark.parametrize(
    "limit", [float("nan"), float("inf"), float("-inf"), True, False, 1.5, 0, -1, "5000"]
)
def test_download_budget_cannot_be_disabled_by_noninteger_registry_values(limit):
    with pytest.raises(ConfigError, match="max_download_bytes"):
        dataset_config(max_download_bytes=limit)


@pytest.mark.parametrize("value", ["false", "true", 0, 1, None])
def test_registry_enabled_flag_requires_a_real_boolean(value):
    with pytest.raises(ConfigError, match="enabled"):
        dataset_config(enabled=value)


@pytest.mark.parametrize("hosts", ["example.org", [], (), [None], [42], [""], ["  "]])
def test_registry_hosts_require_explicit_nonempty_host_strings(hosts):
    with pytest.raises(ConfigError, match="allowed_hosts"):
        dataset_config(allowed_hosts=hosts)


def test_registry_normalizes_host_lists_without_reinterpreting_strings():
    hosts = ["example.org", "download.example.org"]
    config = dataset_config(allowed_hosts=hosts)
    hosts.append("unreviewed.example.org")
    assert config.allowed_hosts == ("example.org", "download.example.org")


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, "30", 0])
def test_registry_timeout_is_a_finite_positive_number(value):
    with pytest.raises(ConfigError, match="timeout_seconds"):
        dataset_config(timeout_seconds=value)
