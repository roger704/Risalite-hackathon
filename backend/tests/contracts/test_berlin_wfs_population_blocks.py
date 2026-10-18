"""Offline schema and spatial-assignment contracts for the Umweltatlas block layer."""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from shapely.geometry import shape

from app.connectors.berlin_wfs import BerlinWFSConnector
from app.connectors.berlin_wfs_population_blocks import assign_parent, check_parent
from app.domain import (
    POPULATION_BLOCK_AGE_BANDS,
    Area,
    DatasetConfig,
    IncompatibleSnapshotError,
    PopulationBlock,
    RawAsset,
    RawBundle,
    SchemaError,
    ValidationError,
    ValueStatus,
    record_from_dict,
    record_to_dict,
)
from app.infrastructure.storage import read_records, write_records

BACKEND = Path(__file__).parents[2]
FIXTURES = BACKEND / "tests/fixtures/berlin_wfs_population_blocks"
CONFIG = BACKEND / "config/datasets/berlin_population_density_blocks.json"
LOR_FIXTURE = BACKEND / "tests/fixtures/lor/areas.geojson"


def configuration(**changes):
    values = json.loads(CONFIG.read_text())
    values["allowed_hosts"] = tuple(values["allowed_hosts"])
    options = values["parser_options"]
    options.update(
        page_size=3,
        max_pages=10,
        expected_count_range=[8, 8],
        max_unassigned_share=0.5,
    )
    options.update(changes.pop("parser_options", {}))
    values.update(changes)
    return DatasetConfig(**values)


def fixture_features():
    return [
        feature
        for path in sorted(FIXTURES.glob("page-*.json"))
        for feature in json.loads(path.read_text())["features"]
    ]


def make_bundle(tmp_path, features):
    collection = {
        "type": "FeatureCollection",
        "numberMatched": len(features),
        "timeStamp": "2026-10-18T00:00:00Z",
        "crs": {
            "type": "name",
            "properties": {"name": "urn:ogc:def:crs:EPSG::25833"},
        },
        "features": features,
    }
    path = tmp_path / "blocks.json"
    path.write_text(json.dumps(collection))
    return RawBundle(
        assets=(
            RawAsset(
                path=str(path),
                sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                size_bytes=path.stat().st_size,
                resource_url="https://gdi.berlin.de/synthetic/blocks.json",
            ),
        ),
        retrieved_at=datetime.now(timezone.utc),
        snapshot_id="blocks-synthetic",
        request_fingerprint="synthetic",
    )


def normalized(tmp_path, features=None, **changes):
    features = fixture_features() if features is None else features
    parser_changes = changes.pop("parser_options", {})
    config = configuration(
        parser_options={"expected_count_range": [len(features), len(features)]}
        | parser_changes
        | changes
    )
    connector = BerlinWFSConnector()
    batch = connector.normalize(make_bundle(tmp_path, features), config)
    return connector, config, batch


def parents():
    collection = json.loads(LOR_FIXTURE.read_text())
    return tuple(
        Area(
            source_id="berlin_lor",
            snapshot_id="lor-synthetic",
            source_record_id=feature["properties"]["PLR_ID"],
            record_id=f"planungsraum:{feature['properties']['PLR_ID']}",
            geometry=feature["geometry"],
            area_code=feature["properties"]["PLR_ID"],
            name=feature["properties"]["PLR_NAME"],
            hierarchy="planungsraum",
            boundary_edition="2021",
        )
        for feature in collection["features"]
    )


def block(**changes):
    values = dict(
        source_id="berlin_population_density_blocks",
        snapshot_id="blocks-synthetic",
        source_record_id="2023000000000001",
        record_id="population_block:2023000000000001",
        geometry={
            "type": "MultiPolygon",
            "coordinates": [[[[13.35, 52.5], [13.36, 52.5], [13.36, 52.51], [13.35, 52.5]]]],
        },
        block_id="2023000000000001",
        area_ha=1.0,
        residents=100,
        residents_status=ValueStatus.OBSERVED,
        density_per_ha=100.0,
        reference_period="2023-12-31",
        residents_by_age=dict.fromkeys(POPULATION_BLOCK_AGE_BANDS, 0),
    )
    values.update(changes)
    return PopulationBlock(**values)


def test_normalizes_population_blocks_and_preserves_suppression_and_flags(tmp_path):
    connector, config, batch = normalized(tmp_path)
    assert len(batch.records) == 8
    first = batch.records[0]
    assert isinstance(first, PopulationBlock)
    assert first.block_id == "2023000000000001"
    assert first.record_id == "population_block:2023000000000001"
    assert first.source_record_id == first.block_id
    assert first.source_id == config.dataset_id
    assert first.snapshot_id == "blocks-synthetic"
    assert first.reference_period == "2023-12-31"
    assert first.residents == 100
    assert first.residents_status is ValueStatus.OBSERVED
    assert first.residents_by_age == dict(
        zip(POPULATION_BLOCK_AGE_BANDS, [10, 10, 20, 49, 5, 3, None, 3])
    )
    assert "age_bands_suppressed_0_to_2" in first.quality_flags
    assert first.geometry["type"] == "MultiPolygon"
    west, south, east, north = shape(first.geometry).bounds
    assert 13.0 < west < east < 13.9
    assert 52.3 < south < north < 52.8
    assert batch.records[5].residents_status is ValueStatus.SUPPRESSED
    assert batch.records[5].residents is batch.records[5].density_per_ha is None
    assert batch.records[5].residents_by_age == {}
    assert "residents_suppressed_0_to_2" in batch.records[5].quality_flags
    assert batch.records[6].residents_by_age == {}
    assert any("6 blocks" in warning and "lower bound" in warning for warning in batch.warnings)
    assert any(
        "3 age-band cells published as 0 (= 0–2) in 3 observed blocks" in warning
        for warning in batch.warnings
    )
    assert connector.validate(batch).valid


def test_normalization_flags_density_and_missing_or_inconsistent_ages(tmp_path):
    features = fixture_features()[:1]
    properties = features[0]["properties"]
    properties["ew_ha_2023"] = 1.0
    properties["alter_u6"] = None
    _, _, batch = normalized(tmp_path, features)
    assert batch.records[0].quality_flags == ("density_inconsistent", "age_bands_missing")
    assert batch.records[0].residents_by_age == {}
    assert any("1 blocks flagged density_inconsistent" in warning for warning in batch.warnings)

    features[0]["properties"]["alter_u6"] = 11
    _, _, batch = normalized(tmp_path, features)
    assert "age_bands_sum_mismatch" in batch.records[0].quality_flags


@pytest.mark.parametrize("residents", [1, 2, True, "100"])
def test_rejects_invalid_resident_contract(tmp_path, residents):
    features = fixture_features()[:1]
    features[0]["properties"]["ew2023"] = residents
    with pytest.raises(SchemaError):
        normalized(tmp_path, features)


@pytest.mark.parametrize("age_value", [1, 2])
def test_rejects_published_age_band_values_within_suppression_range(tmp_path, age_value):
    features = fixture_features()[:1]
    features[0]["properties"]["alter_u6"] = age_value
    with pytest.raises(SchemaError, match="age-band count"):
        normalized(tmp_path, features)


@pytest.mark.parametrize(
    ("age_field", "age_value"),
    [("alter_u6", 11), ("alter_18_u65", 46)],
)
def test_flags_age_band_bounded_sum_mismatches(tmp_path, age_field, age_value):
    features = fixture_features()[:1]
    features[0]["properties"][age_field] = age_value
    _, _, batch = normalized(tmp_path, features)
    assert "age_bands_sum_mismatch" in batch.records[0].quality_flags


def test_rejects_suppressed_nonzero_density_nonpositive_area_and_duplicate_ids(tmp_path):
    features = fixture_features()[:1]
    features[0]["properties"].update(ew2023=0, ew_ha_2023=1.0)
    with pytest.raises(SchemaError, match="zero density"):
        normalized(tmp_path, features)

    features = fixture_features()[:1]
    features[0]["properties"]["ha"] = 0
    with pytest.raises(SchemaError, match="area_ha"):
        normalized(tmp_path, features)

    features = fixture_features()[:2]
    features[1]["properties"]["schluessel"] = features[0]["properties"]["schluessel"]
    with pytest.raises(SchemaError, match="Duplicate WFS ID"):
        normalized(tmp_path, features)


def test_rejects_wrong_age_mapping_and_unsupported_record_type(tmp_path):
    with pytest.raises(SchemaError, match="age_fields"):
        normalized(tmp_path, parser_options={"age_fields": {"0-6": "alter_u6"}})
    with pytest.raises(SchemaError, match="unsupported record_type"):
        normalized(tmp_path, parser_options={"record_type": "unsupported"})


def test_assigns_largest_overlap_flags_and_lowest_code_for_ties(tmp_path):
    connector, config, batch = normalized(tmp_path)
    assigned = assign_parent(config, batch, parents())
    blocks = {record.block_id: record for record in assigned.records}
    assert blocks["2023000000000001"].area_code == "01011101"
    assert blocks["2023000000000002"].area_code == "01011102"
    assert blocks["2023000000000003"].area_code == "01011101"
    assert "plr_assignment_partial" in blocks["2023000000000003"].quality_flags
    assert blocks["2023000000000004"].area_code == "01011101"
    assert "plr_assignment_ambiguous" in blocks["2023000000000004"].quality_flags
    assert blocks["2023000000000005"].area_code is None
    assert "plr_unassigned" in blocks["2023000000000005"].quality_flags
    assert blocks["2023000000000008"].area_code == "01011101"
    assert blocks["2023000000000008"].area_overlap_share == 0.5
    assert connector.validate(assigned).valid


def test_parent_coverage_and_assignment_requirements(tmp_path):
    _, config, batch = normalized(tmp_path)
    with pytest.raises(IncompatibleSnapshotError, match="not been assigned"):
        check_parent(config, batch.records, parents())
    assigned = assign_parent(config, batch, parents())
    coverage = check_parent(config, assigned.records, parents())
    assert coverage["parent_area_count"] == 2
    assert coverage["assignment"]["assigned"] == 7
    assert coverage["assignment"]["unassigned"] == 1
    assert coverage["assignment"]["ambiguous"] == 1
    assert coverage["assignment"]["partial"] == 2
    assert coverage["areas_with_blocks"] == 2
    assert coverage["areas_without_blocks"] == 0

    strict = configuration(parser_options={"max_unassigned_share": 0.1})
    with pytest.raises(IncompatibleSnapshotError, match="max_unassigned_share"):
        check_parent(strict, assigned.records, parents())
    with pytest.raises(IncompatibleSnapshotError, match="non-block"):
        check_parent(config, (parents()[0],), parents())


def test_population_block_model_validation_and_storage_round_trip(tmp_path):
    age_bands = dict.fromkeys(POPULATION_BLOCK_AGE_BANDS, 3)
    age_bands["0-6"] = None
    record = block(
        area_code="01011101",
        hierarchy="planungsraum",
        boundary_edition="2021",
        area_overlap_share=0.75,
        residents_by_age=age_bands,
    )
    encoded = record_to_dict(record)
    assert record_from_dict(encoded) == record
    assert record_from_dict(json.loads(json.dumps(encoded))) == record
    path = tmp_path / "population-block.parquet"
    write_records((record,), path)
    assert read_records(path) == (record,)
    assert record.residents_status is ValueStatus.OBSERVED
    with pytest.raises(ValidationError):
        block(block_id="")
    with pytest.raises(ValidationError):
        block(area_ha=True)
    with pytest.raises(ValidationError):
        block(residents=True)
    with pytest.raises(ValidationError):
        block(residents_status="estimated")
    with pytest.raises(ValidationError):
        block(residents_by_age={"0-6": 1})
    with pytest.raises(ValidationError):
        block(area_code="01011101")
    with pytest.raises(ValidationError):
        block(
            residents=None,
            residents_status=ValueStatus.SUPPRESSED,
            density_per_ha=None,
            residents_by_age={"0-6": 0},
        )
