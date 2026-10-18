"""Shared point and schedule records used by the OSM, IHK and GTFS connectors."""

import pytest

from app.domain import (
    Area,
    Business,
    Place,
    StopService,
    TransitStop,
    ValidationError,
    record_from_dict,
    record_to_dict,
)

POINT = {"type": "Point", "coordinates": [13.4, 52.5]}
BASE = dict(snapshot_id="snap", source_record_id="src", record_id="rec")


def place(**changes):
    values = dict(
        BASE,
        source_id="osm_pois",
        geometry=POINT,
        source_geometry_type="way",
        original_category="amenity=cafe",
        category_mapping_version="osm-1",
        canonical_category="coffee_shop",
        attributes={"opening_hours": "Mo-Fr 08:00-18:00"},
    )
    return Place(**{**values, **changes})


def business(**changes):
    values = dict(
        BASE,
        source_id="ihk_businesses",
        geometry=POINT,
        industry_code="56.30",
        industry_code_system="NACE",
        category_mapping_version="ihk-1",
        employees_label="1 - 4",
        employees_min=1,
        employees_max=4,
        area_code="01100101",
    )
    return Business(**{**values, **changes})


def stop(**changes):
    values = dict(
        BASE, source_id="vbb_gtfs", geometry=POINT, stop_id="de:1", name="S X", location_type=0
    )
    return TransitStop(**{**values, **changes})


def service(**changes):
    values = dict(
        BASE,
        source_id="vbb_gtfs",
        stop_id="de:1",
        service_date="2026-10-20",
        window_start="23:00:00",
        window_end="25:30:00",
        timezone="Europe/Berlin",
        departures=12,
        trip_count=12,
        route_count=2,
        route_types=[109, 3],
    )
    return StopService(**{**values, **changes})


@pytest.mark.parametrize("factory", [place, business, stop, service])
def test_records_round_trip(factory):
    record = factory()
    assert record_from_dict(record_to_dict(record)) == record


def test_area_attributes_round_trip_and_require_string_values():
    record = Area(
        source_id="berlin_lor_names",
        snapshot_id="snap",
        source_record_id="01100101",
        record_id="planungsraum:01100101",
        geometry={
            "type": "Polygon",
            "coordinates": [[[13.4, 52.5], [13.4, 52.6], [13.5, 52.5], [13.4, 52.5]]],
        },
        area_code="01100101",
        name="Stülerstraße",
        hierarchy="planungsraum",
        boundary_edition="2021",
        attributes={"bzr_name": "Tiergarten Süd"},
    )
    assert record_from_dict(record_to_dict(record)) == record
    with pytest.raises(ValidationError, match="strings"):
        Area(
            source_id="berlin_lor_names",
            snapshot_id="snap",
            source_record_id="01100101",
            record_id="planungsraum:01100101",
            geometry={
                "type": "Polygon",
                "coordinates": [[[13.4, 52.5], [13.4, 52.6], [13.5, 52.5], [13.4, 52.5]]],
            },
            area_code="01100101",
            name="Stülerstraße",
            hierarchy="planungsraum",
            boundary_edition="2021",
            attributes={"finhalt": 366933.11},
        )


def test_unmapped_place_category_stays_explicit():
    assert place(canonical_category=None).canonical_category is None


@pytest.mark.parametrize(
    "geometry",
    [
        {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [0, 1], [0, 0]]]},
        {"type": "Point", "coordinates": [390000.0, 5820000.0]},
        {"type": "Point", "coordinates": [13.4]},
        {"type": "Point", "coordinates": [True, 52.5]},
    ],
)
def test_point_records_require_wgs84_points(geometry):
    with pytest.raises(ValidationError):
        place(geometry=geometry)


def test_employee_interval_must_be_ordered():
    with pytest.raises(ValidationError):
        business(employees_min=10, employees_max=4)
    assert business(employees_min=None, employees_max=None).employees_label == "1 - 4"


@pytest.mark.parametrize("location_type", [5, -1, "0", True])
def test_stop_location_type_follows_gtfs(location_type):
    with pytest.raises(ValidationError):
        stop(location_type=location_type)


def test_stop_service_accepts_after_midnight_and_rejects_empty_windows():
    assert service().window_end == "25:30:00"
    with pytest.raises(ValidationError):
        service(window_start="10:00:00", window_end="10:00:00")
    with pytest.raises(ValidationError):
        service(departures=None)
