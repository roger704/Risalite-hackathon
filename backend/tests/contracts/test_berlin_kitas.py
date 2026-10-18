"""Offline field, identity, geometry and completeness checks for the Kita WFS mapping."""

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.connectors.berlin_wfs import BerlinWFSConnector
from app.connectors.berlin_wfs_facility import assign_areas
from app.domain import (
    Area,
    DatasetConfig,
    Facility,
    IncompatibleSnapshotError,
    RawAsset,
    RawBundle,
    SchemaError,
    ValidationError,
    record_from_dict,
    record_to_dict,
)

FIXTURES = Path(__file__).parents[1] / "fixtures/berlin_kitas"
CONFIG = Path(__file__).parents[2] / "config/datasets/berlin_kitas.json"


def configuration(**options):
    values = json.loads(CONFIG.read_text())
    values["allowed_hosts"] = tuple(values["allowed_hosts"])
    values["parser_options"].pop("expected_count_range")
    values["parser_options"].update(page_size=2, expected_count=3, **options)
    return DatasetConfig(**values)


def bundle(tmp_path, mutate=None, snapshot_id="synthetic-kitas", revision="2025-12-31"):
    paths = []
    for index in range(2):
        data = json.loads((FIXTURES / f"page-{index:04d}.json").read_text())
        if mutate:
            mutate(index, data)
        path = tmp_path / f"{snapshot_id}-page-{index:04d}.json"
        path.write_text(json.dumps(data))
        paths.append(path)
    if revision is not None:
        path = tmp_path / f"{snapshot_id}-edition-metadata.json"
        path.write_text(
            (FIXTURES / "edition-metadata.json").read_text().replace("2025-12-31", revision)
        )
        paths.append(path)
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


def normalize(tmp_path, mutate=None, **options):
    return BerlinWFSConnector().normalize(bundle(tmp_path, mutate), configuration(**options))


def test_reviewed_config_documents_source_license_and_semantics():
    values = json.loads(CONFIG.read_text())
    assert values["adapter"] == "berlin_wfs"
    assert values["resource_url"].startswith("https://gdi.berlin.de/services/wfs/kita?")
    assert "TYPENAMES=kita:kita" in values["resource_url"]
    assert values["license_id"] == "dl-de-zero-2.0"
    assert values["source_edition"] == "2025-12-31"
    options = values["parser_options"]
    assert options["record_type"] == "facility"
    assert options["id_field"] == "e_nr"
    assert set(options["attribute_fields"]) | set(options["field_map"].values()) | {"e_nr"} == set(
        options["expected_properties"]
    )
    text = " ".join(options["limitations"])
    for phrase in (
        "not occupied places, available places or vacancies",
        "shortage",
        "Kita-Navigator",
    ):
        assert phrase in text


def test_normalizes_points_ids_categories_attributes_and_capacity(tmp_path):
    batch = normalize(tmp_path)
    report = BerlinWFSConnector().validate(batch)
    assert report.valid and report.rejected_rows == 0 and report.output_rows == 3
    records = {record.provider_facility_id: record for record in batch.records}
    assert set(records) == {"01000001", "01000002", "01000003"}

    alpha = records["01000001"]
    assert isinstance(alpha, Facility)
    assert alpha.record_id == "kita:01000001" and alpha.source_record_id == "01000001"
    assert alpha.source_id == "berlin_kitas" and alpha.snapshot_id == "synthetic-kitas"
    assert alpha.facility_type == "kita"
    assert alpha.original_category == "Kindertagesstätte" and alpha.canonical_category == "kita"
    assert alpha.category_mapping_version == "berlin_kita_e_art_v1"
    assert alpha.name == "Kita Synthetisch Alpha"
    assert alpha.capacity == 45
    assert alpha.capacity_definition == "permitted_places"
    assert alpha.capacity_reference_date == "2025-12-31"
    assert alpha.vacancies is None and alpha.vacancies_reference_date is None
    assert alpha.area_code is None
    assert alpha.attributes["e_bez_nr"] == "01"
    assert alpha.attributes["t_art"] == "freie Träger"
    assert "e_zusatz" not in alpha.attributes and "ang_2" not in alpha.attributes
    assert "e_platz" not in alpha.attributes and "e_name" not in alpha.attributes
    assert alpha.geometry["type"] == "Point"
    longitude, latitude = alpha.geometry["coordinates"]
    assert longitude == pytest.approx(13.352, abs=1e-5)
    assert latitude == pytest.approx(52.502, abs=1e-5)

    blank_capacity = records["01000002"]
    assert blank_capacity.capacity is None
    assert blank_capacity.capacity_definition is None
    assert blank_capacity.capacity_reference_date is None
    assert "capacity_missing" in blank_capacity.quality_flags
    assert blank_capacity.attributes["e_zusatz"] == "Hinterhaus"
    assert records["01000003"].canonical_category == "forest_kindergarten"

    warnings = " ".join(report.warnings)
    assert "Kindertagesstätte=1" in warnings and "Waldkindergarten=1" in warnings
    assert "present for 2/3 facilities, missing for 1, known sum 63" in warnings
    assert "Vacancies are not provided" in warnings
    assert "ang_2=3" in warnings
    assert "Dataset revision 2025-12-31" in warnings


def test_ids_are_stable_across_snapshots_and_round_trip(tmp_path):
    first = normalize(tmp_path)
    second = BerlinWFSConnector().normalize(
        bundle(tmp_path, snapshot_id="later-kitas"), configuration()
    )
    assert [record.record_id for record in first.records] == [
        record.record_id for record in second.records
    ]
    assert {record.snapshot_id for record in second.records} == {"later-kitas"}
    for record in first.records:
        assert record_from_dict(json.loads(json.dumps(record_to_dict(record)))) == record


def test_unmapped_category_is_kept_and_flagged(tmp_path):
    def mutate(index, data):
        if index == 1:
            data["features"][0]["properties"]["e_art"] = "Neue Einrichtungsart"

    batch = normalize(tmp_path, mutate)
    record = batch.records[-1]
    assert record.original_category == "Neue Einrichtungsart"
    assert record.canonical_category is None
    assert "category_unmapped" in record.quality_flags
    assert any("Neue Einrichtungsart=1" in warning for warning in batch.warnings)


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (
            lambda i, d: i == 1 and d["features"][0]["properties"].update(e_nr="01000001"),
            "Duplicate",
        ),
        (lambda i, d: i == 0 and d["features"][0]["properties"].update(e_nr="1"), "id_pattern"),
        (lambda i, d: i == 0 and d["features"][0]["properties"].pop("t_name"), "missing expected"),
        (
            lambda i, d: i == 0 and d["features"][0].update(geometry={"type": "Polygon"}),
            "must be a Point",
        ),
        (
            lambda i, d: i == 0 and d["features"][0]["geometry"].update(coordinates=[13.4, 52.5]),
            "outside source_bbox",
        ),
        (
            lambda i, d: i == 0 and d["features"][0]["geometry"].update(coordinates=["x", 1]),
            "Invalid WFS facility coordinates",
        ),
        (lambda i, d: i == 0 and d["features"][0]["properties"].update(e_platz="4.5"), "whole"),
        (lambda i, d: i == 0 and d["features"][0]["properties"].update(e_art=" "), "category"),
        (lambda i, d: i == 0 and d.update(numberMatched=4), "numberMatched"),
    ],
)
def test_rejects_identity_schema_geometry_and_completeness_errors(tmp_path, mutate, match):
    with pytest.raises(SchemaError, match=match):
        normalize(tmp_path, mutate)


def test_source_edition_is_verified_against_provider_metadata(tmp_path):
    batch = normalize(tmp_path)
    assert any("Source edition 2025-12-31 verified" in warning for warning in batch.warnings)
    with pytest.raises(SchemaError, match="differs from the reviewed source_edition"):
        BerlinWFSConnector().normalize(bundle(tmp_path, revision="2026-12-31"), configuration())
    with pytest.raises(SchemaError, match="Local --file imports are not supported"):
        BerlinWFSConnector().normalize(bundle(tmp_path, revision=None), configuration())


def test_completeness_requires_all_pages(tmp_path):
    raw = bundle(tmp_path)
    with pytest.raises(SchemaError):
        BerlinWFSConnector().normalize(
            replace(raw, assets=(raw.assets[0], raw.assets[2])), configuration()
        )


@pytest.mark.parametrize(
    "options",
    [
        {"category_mapping": None},
        {"field_map": {"name": "e_name"}},
        {"capacity_reference_date": None},
        {"capacity_reference_date": "31.12.2025"},
        {"geometry_types": ["Polygon"]},
        {"edition_metadata_url": "http://datenregister.berlin.de/x"},
        {"max_unassigned_ratio": 2},
        {"district_field": "unknown"},
    ],
)
def test_rejects_unreviewed_facility_options(tmp_path, options):
    with pytest.raises(SchemaError):
        normalize(tmp_path, **options)


def test_facility_contract_keeps_capacity_and_vacancies_nullable_unless_dated():
    values = {
        "source_id": "berlin_kitas",
        "snapshot_id": "s",
        "source_record_id": "01000001",
        "record_id": "kita:01000001",
        "geometry": {"type": "Point", "coordinates": [13.4, 52.5]},
        "facility_type": "kita",
        "provider_facility_id": "01000001",
        "original_category": "Kindertagesstätte",
        "category_mapping_version": "v1",
    }
    facility = Facility(**values)
    assert facility.capacity is None and facility.vacancies is None
    for changes in (
        {"capacity": 10, "capacity_definition": "permitted_places"},
        {"capacity": 10, "capacity_reference_date": "2025-12-31"},
        {"vacancies": 2},
        {"capacity": -1, "capacity_definition": "x", "capacity_reference_date": "2025-12-31"},
        {"vacancies": 1, "vacancies_reference_date": "Dezember"},
        {"geometry": {"type": "Polygon", "coordinates": []}},
        {"attributes": {"e_platz": 5}},
    ):
        with pytest.raises(ValidationError):
            Facility(**values | changes)


def area(code, west, south, size=0.01, hierarchy="planungsraum"):
    ring = [
        [west, south],
        [west + size, south],
        [west + size, south + size],
        [west, south + size],
        [west, south],
    ]
    return Area(
        source_id="berlin_lor",
        snapshot_id="lor",
        source_record_id=code,
        record_id=f"{hierarchy}:{code}",
        area_code=code,
        hierarchy=hierarchy,
        name=code,
        boundary_edition="2021",
        geometry={"type": "Polygon", "coordinates": [ring]},
    )


def test_assigns_facilities_to_areas_in_metric_crs_with_boundary_ties(tmp_path):
    facilities = normalize(tmp_path).records
    areas = (area("01011101", 13.35, 52.5), area("01011102", 13.37, 52.5))
    assigned, ties = assign_areas(facilities, areas)
    assert assigned == {
        "kita:01000001": "01011101",
        "kita:01000002": "01011101",
        "kita:01000003": "01011102",
    }
    assert ties == 0
    edge = replace(
        facilities[0],
        record_id="kita:edge",
        geometry={"type": "Point", "coordinates": [13.36, 52.505]},
    )
    overlapping = (area("01011101", 13.35, 52.5), area("01011100", 13.355, 52.5))
    assigned, ties = assign_areas((edge,), overlapping)
    assert ties == 1 and assigned["kita:edge"] == "01011100"
    outside = replace(edge, geometry={"type": "Point", "coordinates": [13.5, 52.6]})
    assert assign_areas((outside,), overlapping) == ({"kita:edge": None}, 0)


def test_parent_check_reports_area_counts_and_rejects_mismatches(tmp_path):
    connector = BerlinWFSConnector()
    config = configuration()
    facilities = normalize(tmp_path).records
    areas = (area("01011101", 13.35, 52.5), area("01011102", 13.37, 52.5))
    coverage = connector.check_parent(config, facilities, areas)
    join = coverage["area_join"]
    assert join["crs"] == "EPSG:25833"
    assert join["assigned_facility_count"] == 3 and join["unassigned_facility_count"] == 0
    assert join["areas_with_facilities"] == 2 and join["areas_without_facilities"] == 0
    assert join["district_mismatch_count"] == 0

    with pytest.raises(IncompatibleSnapshotError, match="outside every planungsraum"):
        connector.check_parent(config, facilities, areas[:1])
    with pytest.raises(IncompatibleSnapshotError, match="district mismatches"):
        connector.check_parent(
            config, facilities, (area("02011101", 13.35, 52.5), area("02011102", 13.37, 52.5))
        )
    with pytest.raises(IncompatibleSnapshotError, match="no planungsraum"):
        connector.check_parent(config, facilities, (area("01", 13.35, 52.5, hierarchy="bezirk"),))


async def test_fetch_counts_metadata_against_total_byte_limit(tmp_path):
    import httpx

    from app.domain import FetchRequest
    from app.infrastructure.http import HTTPFetcher

    async def handler(request):
        if request.url.host == "datenregister.berlin.de":
            body = (FIXTURES / "edition-metadata.json").read_bytes()
        else:
            start = int(request.url.params["STARTINDEX"])
            body = (FIXTURES / f"page-{start // 2:04d}.json").read_bytes()
        return httpx.Response(
            200, headers={"content-type": "application/json"}, content=body, request=request
        )

    pages = sum((FIXTURES / f"page-{index:04d}.json").stat().st_size for index in range(2))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        connector = BerlinWFSConnector(HTTPFetcher(client))
        raw = await connector.fetch(
            FetchRequest(config=configuration(), staging_dir=str(tmp_path), snapshot_id="s")
        )
        assert raw.assets[-1].path.endswith("edition-metadata.json")
        with pytest.raises(SchemaError, match="max_total_bytes"):
            await connector.fetch(
                FetchRequest(
                    config=configuration(max_total_bytes=pages + 10),
                    staging_dir=str(tmp_path),
                    snapshot_id="s",
                )
            )
