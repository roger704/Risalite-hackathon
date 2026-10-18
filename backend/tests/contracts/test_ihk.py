"""IHK CSV semantics: response sniffing, employee ranges, category mapping, LOR checks."""

import hashlib
import json
import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.connectors.ihk import CategoryMapping, IHKConnector, parse_employees
from app.connectors.lor import LORConnector
from app.domain import (
    ConfigError,
    DatasetConfig,
    IncompatibleSnapshotError,
    RawAsset,
    RawBundle,
    SchemaError,
)

BACKEND = Path(__file__).parents[2]
FIXTURES = BACKEND / "tests" / "fixtures" / "ihk"
CONFIG = BACKEND / "config" / "datasets" / "ihk_businesses.json"


def config(**option_changes):
    values = json.loads(CONFIG.read_text())
    values["parser_options"].update(
        expected_sha256=None,
        min_expected_rows=None,
        max_excluded_row_fraction=0.2,
        parent_check={
            "boundary_tolerance_m": 1.0,
            "max_missing_area_code_fraction": 0.5,
            "max_unmatched_area_code_fraction": 0.5,
            "max_outside_supplied_area_fraction": 0.5,
        },
    )
    values["parser_options"].update(option_changes)
    return DatasetConfig(**values)


def raw(path):
    content = Path(path).read_bytes()
    return RawBundle(
        assets=(
            RawAsset(
                path=str(path),
                sha256=hashlib.sha256(content).hexdigest(),
                size_bytes=len(content),
                resource_url=config().resource_url,
            ),
        ),
        snapshot_id="test-ihk",
        request_fingerprint="test",
        retrieved_at=datetime.now(timezone.utc),
    )


def normalized(**option_changes):
    return IHKConnector().normalize(raw(FIXTURES / "businesses.csv"), config(**option_changes))


def lor_areas():
    lor_config = DatasetConfig(
        **{
            **json.loads((BACKEND / "config/datasets/berlin_lor.json").read_text()),
            "source_crs": "EPSG:4326",
        }
    )
    lor_config = replace(
        lor_config,
        parser_options={**lor_config.parser_options, "expected_rows": 2, "name_field": "PLR_NAME"},
    )
    return LORConnector().normalize(raw(BACKEND / "tests/fixtures/lor/areas.geojson"), lor_config)


def test_reviewed_config_pins_commit_edition_and_mapping():
    values = json.loads(CONFIG.read_text())
    options = values["parser_options"]
    assert options["source_commit"] in values["resource_url"]
    assert "/master/" not in values["resource_url"]
    assert values["source_edition"] == "2026-10"
    assert values["max_download_bytes"] > 125_464_561
    assert options["parent_dataset"] == "berlin_lor"
    assert CategoryMapping.load(options).version == "ihk_v1"


def test_fixture_records_keep_codes_ranges_and_flags():
    batch = normalized()
    assert batch.input_rows == 10 and batch.rejected_rows == 0
    records = {record.record_id: record for record in batch.records}
    assert "1005" not in records
    assert any(
        "1 rows without valid WGS84 coordinates" in w and "1005" in w for w in batch.warnings
    )

    cafe = records["1001"]
    assert cafe.area_code == "01011101"
    assert cafe.canonical_category == "coffee_shop"
    assert (cafe.employees_label, cafe.employees_min, cafe.employees_max) == (
        "1 - 3 Beschäftigte",
        1,
        3,
    )
    assert cafe.industry_code == "56114" and cafe.industry_code_system == "ihk_berlin_branch"
    assert cafe.category_mapping_version == "ihk_v1" and cafe.business_age_label == "3"
    assert cafe.geometry == {"type": "Point", "coordinates": [13.355, 52.505]}

    stehcafe = records["1002"]
    assert stehcafe.canonical_category == "coffee_shop"
    assert stehcafe.employees_label == "unbekannt"
    assert stehcafe.employees_min is None and stehcafe.employees_max is None
    assert {"employees_range_unknown", "business_age_missing"} <= set(stehcafe.quality_flags)

    unmapped = records["1003"]
    assert unmapped.canonical_category is None and "category_unmapped" in unmapped.quality_flags
    assert (unmapped.employees_min, unmapped.employees_max) == (10000, None)
    assert "employees_open_upper_bound" in unmapped.quality_flags

    bar = records["1004"]
    assert bar.canonical_category == "bar"
    assert bar.employees_label is None and bar.employees_min is None
    assert "employees_range_missing" in bar.quality_flags

    assert records["1006"].employees_min == 0 and records["1006"].employees_max == 0
    outside = records["1007"]
    assert outside.area_code is None
    assert {"planungsraum_id_missing", "outside_berlin_bbox"} <= set(outside.quality_flags)
    assert records["1008"].industry_code is None
    assert "industry_code_missing" in records["1008"].quality_flags
    assert (
        records["1009"].area_code == "09999999" and records["1009"].canonical_category == "bakery"
    )
    ambiguous = records["1010"]
    assert ambiguous.canonical_category == "food_service_unspecified"
    assert "employees_range_unrecognized" in ambiguous.quality_flags
    assert IHKConnector().validate(batch).valid


@pytest.mark.parametrize(
    "label, expected",
    [
        ("0 Beschäftigte", (0, 0, None)),
        ("7 - 9 Beschäftigte", (7, 9, None)),
        ("7500 - 9999 Beschäftigte", (7500, 9999, None)),
        ("10000 und mehr Beschäftigte", (10000, None, "employees_open_upper_bound")),
        ("unbekannt", (None, None, "employees_range_unknown")),
        (None, (None, None, "employees_range_missing")),
        ("9 - 7 Beschäftigte", (None, None, "employees_range_unrecognized")),
        ("1-3", (None, None, "employees_range_unrecognized")),
    ],
)
def test_employee_labels(label, expected):
    assert parse_employees(label, {"unbekannt"}) == expected


@pytest.mark.parametrize(
    "name, message", [("lfs_pointer.csv", "Git LFS pointer"), ("html_response.csv", "HTML")]
)
def test_pointer_and_html_responses_fail_before_parsing(name, message):
    with pytest.raises(SchemaError, match=message):
        IHKConnector().normalize(raw(FIXTURES / name), config())


def test_changed_header_and_row_width_fail(tmp_path):
    text = (FIXTURES / "businesses.csv").read_text(encoding="utf-8-sig")
    renamed = tmp_path / "renamed.csv"
    renamed.write_text(text.replace("planungsraum_id", "plr_id", 1), encoding="utf-8-sig")
    with pytest.raises(SchemaError, match="header changed"):
        IHKConnector().normalize(raw(renamed), config())
    short = tmp_path / "short.csv"
    short.write_text(text + "1011,Berlin\r\n", encoding="utf-8-sig")
    with pytest.raises(SchemaError, match="row width"):
        IHKConnector().normalize(raw(short), config())
    semicolon = tmp_path / "semicolon.csv"
    semicolon.write_text(text.replace(",", ";"), encoding="utf-8-sig")
    with pytest.raises(SchemaError, match="header changed"):
        IHKConnector().normalize(raw(semicolon), config())


def test_duplicate_ids_reject_and_pinned_hash_is_enforced(tmp_path):
    text = (FIXTURES / "businesses.csv").read_text(encoding="utf-8-sig")
    duplicate = tmp_path / "duplicate.csv"
    duplicate.write_text(text + text.splitlines()[1] + "\r\n", encoding="utf-8-sig")
    batch = IHKConnector().normalize(raw(duplicate), config())
    assert batch.rejected_rows == 1 and not IHKConnector().validate(batch).valid
    with pytest.raises(SchemaError, match="SHA-256"):
        normalized(expected_sha256="0" * 64)


def test_row_and_coordinate_thresholds_fail_validation():
    assert not IHKConnector().validate(normalized(max_excluded_row_fraction=0.05)).valid
    report = IHKConnector().validate(normalized(min_expected_rows=300_000))
    assert any("at least 300000 rows" in error for error in report.errors)


def test_mapping_hash_must_match_reviewed_file():
    with pytest.raises(ConfigError, match="SHA-256"):
        normalized(category_mapping_sha256="0" * 64)


def test_mapping_precedence_and_separate_hospitality_categories():
    mapping = CategoryMapping.load(config().parser_options)
    assert mapping.category("56114") == mapping.category("561141") == "coffee_shop"
    assert mapping.category("5630") == mapping.category("563031") == "bar"
    assert mapping.category("56302") == "nightclub"
    assert mapping.category("5611") == mapping.category("56") == "food_service_unspecified"
    assert mapping.category("56111") == "restaurant"
    assert mapping.category("472401") == mapping.category("10710") == "bakery"
    assert mapping.category("472701") == "coffee_retail"
    assert mapping.category("471121") == "kiosk" and mapping.category("47111") == "grocery"
    assert mapping.category("47710") == "retail_other"
    assert mapping.category("5640") is None and mapping.category("62100") is None
    assert mapping.category(None) is None
    with pytest.raises(ConfigError, match="twice"):
        CategoryMapping(
            {
                "mapping_version": "x",
                "code_system": "y",
                "rules": [
                    {"canonical_category": "a", "prefixes": ["56"]},
                    {"canonical_category": "b", "prefixes": ["56"]},
                ],
            }
        )


def test_check_parent_reports_unmatched_missing_and_outside_points():
    batch, areas = normalized(), lor_areas()
    coverage = IHKConnector().check_parent(config(), batch.records, areas.records)
    assert coverage["business_count"] == 9
    assert coverage["area_code_missing_count"] == 1
    assert coverage["area_code_unmatched_count"] == 1
    assert coverage["area_code_unmatched_codes"] == ["09999999"]
    assert coverage["outside_supplied_area_count"] == 1
    assert coverage["outside_supplied_area_sample"] == ["1006"]
    assert coverage["category_counts"]["coffee_shop"] == 2
    assert coverage["category_counts"]["unmapped"] == 2
    assert "not deduplicated against OSM" in coverage["inventory"]
    records = {record.record_id: record for record in batch.records}
    assert records["1006"].area_code == "01011101"


def test_check_parent_fails_on_systemic_mismatch_and_wrong_edition():
    batch, areas = normalized(), lor_areas()
    strict = config(parent_check={"max_outside_supplied_area_fraction": 0.05})
    with pytest.raises(IncompatibleSnapshotError, match="Systemic"):
        IHKConnector().check_parent(strict, batch.records, areas.records)
    other_edition = replace(config(), boundary_edition="2019")
    with pytest.raises(IncompatibleSnapshotError, match="boundary edition"):
        IHKConnector().check_parent(other_edition, batch.records, areas.records)


@pytest.mark.live
@pytest.mark.skipif(os.environ.get("RISALITE_LIVE_TESTS") != "1", reason="opt-in live test")
def test_live_pinned_file_header():
    import httpx

    values = json.loads(CONFIG.read_text())
    response = httpx.get(values["resource_url"], headers={"Range": "bytes=0-4095"}, timeout=30)
    assert response.status_code in {200, 206}
    prefix = response.content[:4096]
    assert not prefix.startswith(b"version https://git-lfs")
    header = prefix.decode("utf-8-sig", errors="replace").splitlines()[0]
    assert header.split(",") == values["parser_options"]["expected_columns"]
