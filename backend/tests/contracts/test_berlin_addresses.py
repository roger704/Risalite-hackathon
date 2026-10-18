"""Synthetic Berlin address WFS contracts; no provider data is included."""

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.connectors.berlin_addresses import BerlinAddressConnector
from app.domain import (
    Address,
    Area,
    DatasetConfig,
    IncompatibleSnapshotError,
    RawAsset,
    RawBundle,
    SchemaError,
    record_from_dict,
    record_to_dict,
)

BACKEND = Path(__file__).parents[2]
FIXTURES = BACKEND / "tests/fixtures/berlin_addresses"
CONFIG = BACKEND / "config/datasets/berlin_addresses.json"


def configuration(**option_changes):
    values = json.loads(CONFIG.read_text())
    values["allowed_hosts"] = tuple(values["allowed_hosts"])
    options = values["parser_options"]
    options.pop("expected_count_range")
    options.update(page_size=3, max_pages=10, expected_count=6)
    options.update(option_changes)
    values["parser_options"] = options
    return DatasetConfig(**values)


def bundle(paths, snapshot_id="address-fixture"):
    return RawBundle(
        assets=tuple(
            RawAsset(
                path=str(path),
                sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                size_bytes=path.stat().st_size,
                resource_url=f"https://gdi.berlin.de/synthetic/{path.name}",
            )
            for path in paths
        ),
        retrieved_at=datetime.now(timezone.utc),
        snapshot_id=snapshot_id,
        request_fingerprint="synthetic",
    )


def edited_pages(tmp_path, mutate_first=None, mutate_second=None):
    paths = []
    for index, mutate in enumerate((mutate_first, mutate_second)):
        data = json.loads((FIXTURES / f"page-{index:04d}.json").read_text())
        if mutate:
            mutate(data)
        path = tmp_path / f"page-{index:04d}.json"
        path.write_text(json.dumps(data))
        paths.append(path)
    return paths


def normalized(tmp_path):
    return BerlinAddressConnector().normalize(
        bundle([FIXTURES / "page-0000.json", FIXTURES / "page-0001.json"]),
        configuration(),
    )


def parent_areas():
    geometry = {
        "type": "Polygon",
        "coordinates": [
            [
                [13.34, 52.49],
                [13.38, 52.49],
                [13.38, 52.53],
                [13.34, 52.53],
                [13.34, 52.49],
            ]
        ],
    }
    return tuple(
        Area(
            source_id="berlin_lor",
            snapshot_id="lor-fixture",
            source_record_id=code,
            record_id=f"planungsraum:{code}",
            geometry=geometry,
            area_code=code,
            name=f"Synthetic {code}",
            hierarchy="planungsraum",
            boundary_edition="2021",
        )
        for code in ("01100101", "01100102")
    )


def test_normalizes_address_fields_and_keeps_provider_ids_and_leading_zeroes(tmp_path):
    batch = normalized(tmp_path)
    records = {record.source_record_id: record for record in batch.records}

    assert len(records) == 6
    assert records["1001"].record_id == "address:1001"
    assert records["1001"].street_code == "04786"
    assert records["1001"].area_code == "01100101"
    assert records["1002"].house_number_suffix == "A"
    assert records["S297"].house_number is None
    assert "house_number_missing" in records["S297"].quality_flags
    assert "area_code_missing" in records["S297"].quality_flags
    assert isinstance(record_from_dict(record_to_dict(records["1001"])), Address)

    another_snapshot = BerlinAddressConnector().normalize(
        bundle(
            [FIXTURES / "page-0000.json", FIXTURES / "page-0001.json"],
            snapshot_id="another-snapshot",
        ),
        configuration(),
    )
    assert another_snapshot.records[0].record_id == records["1001"].record_id


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda feature: feature["properties"].pop("plz"), "missing expected properties"),
        (lambda feature: feature["properties"].update(plz=10999), "Invalid plz type"),
        (lambda feature: feature["properties"].update(typ="Unknown"), "Unexpected address type"),
        (lambda feature: feature["properties"].update(plr_nr="wrong"), "Invalid plr_nr pattern"),
        (
            lambda feature: feature["properties"].update(adr_datum="2026-13-01"),
            "Invalid adr_datum calendar date",
        ),
    ],
)
def test_rejects_schema_drift_with_source_id(tmp_path, mutate, message):
    def change(data):
        mutate(data["features"][0])

    paths = edited_pages(tmp_path, mutate_first=change)
    with pytest.raises(SchemaError, match=message) as error:
        BerlinAddressConnector().normalize(bundle(paths), configuration())
    assert "1001" in str(error.value)


def test_extra_property_is_warned_but_not_rejected(tmp_path):
    paths = edited_pages(
        tmp_path,
        mutate_first=lambda data: data["features"][0]["properties"].update(new_field="future"),
    )
    batch = BerlinAddressConnector().normalize(bundle(paths), configuration())
    assert len(batch.records) == 6
    assert any("new_field" in warning for warning in batch.warnings)


@pytest.mark.parametrize(
    ("geometry", "message"),
    [
        ({"type": "Polygon", "coordinates": []}, "Invalid Point geometry"),
        ({"type": "Point", "coordinates": [500000, 5819800]}, "outside source_bbox"),
        ({"type": "Point", "coordinates": [float("nan"), 5819800]}, "Invalid Point coordinates"),
    ],
)
def test_rejects_invalid_or_out_of_bounds_coordinates(tmp_path, geometry, message):
    paths = edited_pages(
        tmp_path,
        mutate_first=lambda data: data["features"][0].update(geometry=geometry),
    )
    with pytest.raises(SchemaError, match=message):
        BerlinAddressConnector().normalize(bundle(paths), configuration())


def test_rejects_wrong_crs_and_number_matched_and_feature_count_mismatches(tmp_path):
    wrong_crs = edited_pages(
        tmp_path,
        mutate_first=lambda data: data["crs"]["properties"].update(name="EPSG:4326"),
    )
    with pytest.raises(SchemaError, match="CRS differs"):
        BerlinAddressConnector().normalize(bundle(wrong_crs), configuration())

    changed_pages = edited_pages(
        tmp_path,
        mutate_second=lambda data: data.update(numberMatched=7),
    )
    with pytest.raises(SchemaError, match="numberMatched changed across pages"):
        BerlinAddressConnector().normalize(bundle(changed_pages), configuration())

    count_pages = edited_pages(
        tmp_path,
        mutate_first=lambda data: data.update(numberMatched=5),
        mutate_second=lambda data: data.update(numberMatched=5),
    )
    with pytest.raises(SchemaError, match="count mismatch"):
        BerlinAddressConnector().normalize(bundle(count_pages), configuration())


def test_check_parent_reports_unknown_codes_and_outside_points(tmp_path):
    connector = BerlinAddressConnector()
    records = normalized(tmp_path).records
    unknown = list(records)
    unknown[0] = replace(unknown[0], area_code="99999999")
    with pytest.raises(IncompatibleSnapshotError, match="absent from the pinned LOR"):
        connector.check_parent(configuration(), tuple(unknown), parent_areas())

    outside = list(records)
    outside[0] = replace(outside[0], geometry={"type": "Point", "coordinates": [13.8, 52.7]})
    result = connector.check_parent(configuration(), tuple(outside), parent_areas())
    assert result["outside_supplied_area_count"] == 1
    assert result["outside_supplied_area_record_ids"] == ["address:1001"]
    assert result["area_code_missing_count"] == 1
    assert result["parent_area_count"] == 2
    strict_options = configuration().parser_options.copy()
    strict_options["parent_check"] = {
        **strict_options["parent_check"],
        "max_outside_supplied_area_count": 0,
    }
    with pytest.raises(IncompatibleSnapshotError, match="outside their supplied LOR areas"):
        connector.check_parent(
            replace(configuration(), parser_options=strict_options),
            tuple(outside),
            parent_areas(),
        )
