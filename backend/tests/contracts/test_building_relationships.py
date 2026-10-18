"""Synthetic-only spatial association tests for ALKIS buildings and future parcels."""

from shapely.geometry import Point, box, mapping

from app.domain import Address, Building
from app.geo.buildings import (
    BuildingIndex,
    address_to_buildings,
    address_to_buildings_for_record,
    building_to_parcels,
    buildings_at_point,
)


def building(record_id, bounds, **changes):
    values = {
        "source_id": "berlin_alkis_buildings",
        "snapshot_id": "synthetic",
        "source_record_id": record_id,
        "record_id": f"alkis_building:{record_id}",
        "geometry": mapping(box(*bounds)),
        "alkis_object_type": "AX_Gebaeude",
        "address_labels": (),
        "street_codes": (),
    }
    values.update(changes)
    return Building(**values)


def parcel(record_id, bounds):
    return type(
        "Parcel",
        (),
        {"record_id": record_id, "geometry": mapping(box(*bounds))},
    )()


def address(record_id, point, street_name, house_number, suffix=None, street_code=None):
    return Address(
        source_id="berlin_addresses",
        snapshot_id="synthetic-addresses",
        source_record_id=record_id,
        record_id=f"address:{record_id}",
        geometry=mapping(Point(*point)),
        street_name=street_name,
        address_type="house_number",
        street_code=street_code,
        house_number=house_number,
        house_number_suffix=suffix,
    )


def test_point_query_returns_all_overlapping_containers():
    first = building("one", (13.4, 52.5, 13.401, 52.501))
    second = building("two", (13.4, 52.5, 13.401, 52.501))
    results = buildings_at_point(BuildingIndex([first, second]), 13.4005, 52.5005, 0)
    assert {item["record"].record_id for item in results} == {first.record_id, second.record_id}
    assert all(item["relation"] == "contains" and item["distance_m"] == 0 for item in results)


def test_point_and_address_queries_include_footprint_edge():
    record = building(
        "edge",
        (13.4, 52.5, 13.401, 52.501),
        address_labels=("Known Street 1",),
        address_reference_kind="house_number",
    )
    index = BuildingIndex([record])

    point_results = buildings_at_point(index, 13.4, 52.5, 0)
    assert len(point_results) == 1
    assert point_results[0]["record"].record_id == record.record_id
    assert point_results[0]["relation"] == "contains"
    assert point_results[0]["distance_m"] == 0

    address_result = address_to_buildings(index, "Different Street", "99", lon=13.4, lat=52.5)
    assert address_result["status"] == "unique"
    assert address_result["matches"][0]["record_id"] == record.record_id
    assert address_result["matches"][0]["match_methods"] == ["contains_address_point"]


def test_address_multiple_buildings_is_ambiguous_and_suffix_match_wins():
    base = building(
        "base",
        (13.4, 52.5, 13.401, 52.501),
        address_labels=("Kreuzbergstraße 19",),
        address_reference_kind="house_number",
    )
    exact = building(
        "exact",
        (13.401, 52.5, 13.402, 52.501),
        address_labels=("Kreuzbergstraße 19A",),
        address_reference_kind="house_number",
    )
    same_label = building(
        "duplicate",
        (13.402, 52.5, 13.403, 52.501),
        address_labels=("Kreuzbergstraße 19A",),
        address_reference_kind="house_number",
    )
    index = BuildingIndex([base, exact, same_label])

    without_suffix = address_to_buildings(index, "Kreuzbergstr.", "19")
    assert without_suffix["status"] == "unique"
    assert without_suffix["matches"][0]["record_id"] == base.record_id
    with_suffix = address_to_buildings(index, "Kreuzbergstraße", "19", "a")
    assert with_suffix["status"] == "ambiguous"
    assert {item["record_id"] for item in with_suffix["matches"]} == {
        exact.record_id,
        same_label.record_id,
    }
    assert all(not item["base_number_only"] for item in with_suffix["matches"])
    base_only = address_to_buildings(BuildingIndex([base]), "Kreuzbergstraße", "19", "A")
    assert base_only["matches"][0]["base_number_only"] is True


def test_real_address_record_maps_to_unique_own_address_building():
    record = building(
        "own-address",
        (13.4, 52.5, 13.401, 52.501),
        address_labels=("Kreuzbergstraße 19",),
        address_reference_kind="house_number",
        street_codes=("04786",),
    )
    address_record = address(
        "1001",
        (13.4005, 52.5005),
        "Kreuzbergstraße",
        19,
        street_code="04786",
    )

    result = address_to_buildings_for_record(BuildingIndex([record]), address_record)

    assert result["status"] == "unique"
    assert result["matches"][0]["record_id"] == record.record_id
    assert result["matches"][0]["match_methods"] == [
        "address_reference",
        "contains_address_point",
    ]
    assert result["matches"][0]["street_code_consistent"] is True


def test_real_address_record_can_match_two_buildings_ambiguously():
    first = building(
        "first-address-match",
        (13.4, 52.5, 13.401, 52.501),
        address_labels=("Kreuzbergstraße 19",),
        address_reference_kind="house_number",
    )
    second = building(
        "second-address-match",
        (13.4, 52.5, 13.401, 52.501),
        address_labels=("Kreuzbergstraße 19",),
        address_reference_kind="house_number",
    )
    address_record = address("1002", (13.4005, 52.5005), "Kreuzbergstraße", 19)

    result = address_to_buildings_for_record(BuildingIndex([first, second]), address_record)

    assert result["status"] == "ambiguous"
    assert {item["record_id"] for item in result["matches"]} == {
        first.record_id,
        second.record_id,
    }


def test_real_address_suffix_matches_base_number_building_as_base_only():
    base = building(
        "base-number",
        (13.4, 52.5, 13.401, 52.501),
        address_labels=("Kreuzbergstraße 19",),
        address_reference_kind="house_number",
    )
    address_record = address("1003", (13.4005, 52.5005), "Kreuzbergstraße", 19, suffix="A")

    result = address_to_buildings_for_record(BuildingIndex([base]), address_record)

    assert result["status"] == "unique"
    assert result["matches"][0]["record_id"] == base.record_id
    assert result["matches"][0]["base_number_only"] is True


def test_pseudonumber_kind_and_street_code_consistency_are_reported():
    pseudo = building(
        "pseudo",
        (13.4, 52.5, 13.401, 52.501),
        address_labels=("Bürknerstraße 27",),
        address_reference_kind="pseudonumber",
        street_codes=("00796",),
    )
    index = BuildingIndex([pseudo])
    result = address_to_buildings(index, "Bürknerstr.", "27", street_code="00796")
    assert result["matches"][0]["reference_kind"] == "pseudonumber"
    assert result["matches"][0]["street_code_consistent"] is True
    inconsistent = address_to_buildings(index, "Bürknerstraße", "27", street_code="99999")
    assert inconsistent["matches"][0]["street_code_consistent"] is False
    without_codes = building(
        "no-code",
        (13.4, 52.5, 13.401, 52.501),
        address_labels=("Bürknerstraße 27",),
        address_reference_kind="house_number",
    )
    unknown = address_to_buildings(
        BuildingIndex([without_codes]), "Bürknerstr.", "27", street_code="00796"
    )
    assert unknown["matches"][0]["street_code_consistent"] is None


def test_unmatched_address_returns_nearby_candidates_only_as_information():
    record = building(
        "near",
        (13.4001, 52.5, 13.4002, 52.5001),
        address_labels=("Andere Straße 1",),
    )
    result = address_to_buildings(
        BuildingIndex([record]),
        "Unbekannte Straße",
        "99",
        lon=13.4,
        lat=52.50005,
    )
    assert result["status"] == "none"
    assert result["matches"] == []
    assert result["nearby_candidates"][0]["record"].record_id == record.record_id
    assert result["nearby_candidates"][0]["relation"] == "nearby"


def test_building_to_parcels_reports_all_overlaps_and_thresholds():
    footprint = building("b", (13.4, 52.5, 13.401, 52.501))
    first = parcel("p1", (13.4, 52.5, 13.4008, 52.501))
    second = parcel("p2", (13.4008, 52.5, 13.401, 52.501))
    outside = parcel("p3", (13.402, 52.5, 13.403, 52.501))

    unique = building_to_parcels(footprint, [first], min_overlap_share=0.01, min_overlap_m2=0.5)
    assert unique["status"] == "unique"
    assert unique["matches"][0]["record_id"] == "p1"
    assert unique["matches"][0]["overlap_m2"] > 0
    assert 0.79 < unique["matches"][0]["share_of_footprint"] < 0.81

    multiple = building_to_parcels(footprint, [first, second])
    assert multiple["status"] == "multiple"
    assert [item["record_id"] for item in multiple["matches"]] == ["p1", "p2"]
    assert building_to_parcels(footprint, [outside])["status"] == "none"
    assert (
        building_to_parcels(footprint, [first, second], min_overlap_share=0.5, min_overlap_m2=1)[
            "status"
        ]
        == "unique"
    )
