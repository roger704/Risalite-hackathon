"""Synthetic road-traffic contracts and offline EPSG:25833 helper checks."""

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pyproj import Transformer
from shapely.geometry import LineString, Polygon, mapping, shape
from shapely.ops import transform

from app.connectors.berlin_wfs import BerlinWFSConnector
from app.domain import (
    Area,
    ConfigError,
    DatasetConfig,
    RawAsset,
    RawBundle,
    RoadTrafficSegment,
    SchemaError,
    ValidationError,
    record_from_dict,
    record_to_dict,
)
from app.geo.traffic import road_segments_near_point, summarize_road_traffic_by_area

BACKEND = Path(__file__).parents[2]
FIXTURES = BACKEND / "tests/fixtures/berlin_wfs_traffic"
CONFIG = BACKEND / "config/datasets/berlin_traffic_volumes_2019.json"
TO_WGS84 = Transformer.from_crs("EPSG:25833", "EPSG:4326", always_xy=True)


def configuration(**option_changes):
    values = json.loads(CONFIG.read_text())
    values["parser_options"].pop("expected_count_range")
    values["parser_options"].update(expected_count=3, **option_changes)
    return DatasetConfig(**values)


def make_bundle(paths):
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
        snapshot_id="synthetic-road-traffic",
        request_fingerprint="synthetic",
    )


def fixture_bundle(tmp_path, mutations=None):
    paths = []
    for index in range(2):
        data = json.loads((FIXTURES / f"page-{index:04d}.json").read_text())
        if mutations and index in mutations:
            mutations[index](data)
        path = tmp_path / f"page-{index:04d}.json"
        path.write_text(json.dumps(data))
        paths.append(path)
    return make_bundle(paths)


def normalize(bundle=None, config=None):
    return BerlinWFSConnector().normalize(
        bundle or make_bundle([FIXTURES / "page-0000.json", FIXTURES / "page-0001.json"]),
        config or configuration(),
    )


def test_road_traffic_record_round_trips_and_validates_values():
    record = RoadTrafficSegment(
        source_id="berlin_traffic_volumes_2019",
        snapshot_id="synthetic",
        source_record_id="00000001_00000002",
        record_id="road_segment:00000001_00000002",
        geometry={"type": "LineString", "coordinates": [[13.4, 52.5], [13.41, 52.5]]},
        reference_period="2019",
        dtv_motor_vehicles_per_day=1000,
        vehicle_class_volumes_per_day={"cars": 800},
        name="Synthetic Road",
        source_length_m=100,
        attributes={"herkunft": "synthetic"},
    )
    assert record_from_dict(record_to_dict(record)) == record
    assert record.record_type == "road_traffic_segment"
    with pytest.raises(ValidationError):
        replace(record, dtv_motor_vehicles_per_day=True)
    with pytest.raises(ValidationError):
        replace(record, vehicle_class_volumes_per_day={"cars": -1})
    with pytest.raises(ValidationError):
        replace(record, vehicle_class_volumes_per_day={"cars": True})
    with pytest.raises(ValidationError):
        replace(record, source_length_m=-1)
    with pytest.raises(ValidationError):
        replace(record, name="")


def test_normalizes_projected_lines_and_missing_values_with_summary_warnings(tmp_path):
    batch = normalize(fixture_bundle(tmp_path))
    first, second, third = batch.records

    assert all(isinstance(record, RoadTrafficSegment) for record in batch.records)
    assert first.record_id == "road_segment:00000001_00000002"
    assert first.source_record_id == "00000001_00000002"
    assert first.reference_period == "2019"
    assert first.name == "Synthetic Road A"
    assert first.source_length_m == 100
    assert first.dtv_motor_vehicles_per_day == 1000
    assert first.vehicle_class_volumes_per_day == {
        "cars": 800,
        "trucks_over_3_5t_excl_buses": 50,
        "vans_up_to_3_5t": 100,
        "motorcycles": 20,
        "coaches": 10,
        "scheduled_buses": 20,
    }
    assert first.attributes == {
        "link_id": "00000001_00000002",
        "herkunft": "Verkehrsmengenkarte 2019",
    }
    assert shape(first.geometry).geom_type == "LineString"
    west, south, east, north = shape(first.geometry).bounds
    assert 13.0 <= west <= east <= 13.9
    assert 52.3 <= south <= north <= 52.8
    assert third.dtv_motor_vehicles_per_day is None
    assert third.vehicle_class_volumes_per_day == {
        "vans_up_to_3_5t": 0,
        "motorcycles": 0,
        "coaches": 0,
        "scheduled_buses": 0,
    }
    assert "volume_missing:dtv" in third.quality_flags
    assert "volume_missing:pkw" in third.quality_flags
    assert "name_missing" in third.quality_flags
    assert "length_missing" in third.quality_flags
    assert batch.input_rows == 3
    assert batch.rejected_rows == 0
    assert all(value in batch.warnings for value in configuration().parser_options["limitations"])
    assert any("Segments with missing DTV: 1" == warning for warning in batch.warnings)
    assert any(
        "DTV source (herkunft): Verkehrsmengenkarte 2019 (incl. Korrektur) 1, "
        "VISUM model 1, VMZ plausibility check 0, traffic counts 1 "
        "(dates 2019-01-01..2019-01-01)" == warning
        for warning in batch.warnings
    )
    assert any("Total network length: 0.4 km" == warning for warning in batch.warnings)
    assert BerlinWFSConnector().validate(batch).valid


def test_keeps_class_sum_mismatch_and_reports_extra_properties(tmp_path):
    def edit(data):
        data["features"][0]["properties"]["pkw"] = 700
        data["features"][0]["properties"]["provider_note"] = "extra"

    batch = normalize(fixture_bundle(tmp_path, {0: edit}))
    assert "dtv_class_sum_mismatch" in batch.records[0].quality_flags
    assert any("provider_note" in warning for warning in batch.warnings)


@pytest.mark.parametrize("value", ["1000", -1, True, 1.5])
def test_rejects_invalid_volumes(tmp_path, value):
    def edit(data):
        data["features"][0]["properties"]["dtv"] = value

    with pytest.raises(SchemaError, match="dtv must be a nonnegative integer"):
        normalize(fixture_bundle(tmp_path, {0: edit}))


def test_rejects_invalid_class_volumes_and_unsupported_geometry(tmp_path):
    def bad_class(data):
        data["features"][0]["properties"]["pkw"] = -1

    with pytest.raises(SchemaError, match="pkw must be a nonnegative integer"):
        normalize(fixture_bundle(tmp_path, {0: bad_class}))

    def bad_geometry(data):
        data["features"][0]["geometry"] = {
            "type": "Point",
            "coordinates": [390000, 5819000],
        }

    with pytest.raises(SchemaError, match="Unsupported WFS line geometry"):
        normalize(fixture_bundle(tmp_path, {0: bad_geometry}))

    def missing_required_property(data):
        data["features"][0]["properties"].pop("dtv")

    with pytest.raises(SchemaError, match="missing expected properties"):
        normalize(fixture_bundle(tmp_path, {0: missing_required_property}))


def test_rejects_duplicate_ids_and_missing_reference_period(tmp_path):
    def duplicate(data):
        data["features"][0]["properties"]["schluessel"] = "T0000001_00000003"

    with pytest.raises(SchemaError, match="Duplicate WFS ID"):
        normalize(fixture_bundle(tmp_path, {0: duplicate}))

    with pytest.raises(ConfigError, match="requires reference_period"):
        normalize(fixture_bundle(tmp_path), replace(configuration(), reference_period=None))


def wgs84_geometry(geometry):
    return transform(TO_WGS84.transform, geometry)


def road_segment(record_id, coordinates, volume, reference_period="2019"):
    return RoadTrafficSegment(
        source_id="synthetic",
        snapshot_id="synthetic",
        source_record_id=record_id,
        record_id=record_id,
        geometry=mapping(wgs84_geometry(LineString(coordinates))),
        reference_period=reference_period,
        dtv_motor_vehicles_per_day=volume,
        name=record_id,
    )


def area(area_code, bounds):
    x1, y1, x2, y2 = bounds
    return Area(
        source_id="synthetic",
        snapshot_id="synthetic",
        source_record_id=area_code,
        record_id=f"planungsraum:{area_code}",
        geometry=mapping(wgs84_geometry(Polygon([(x1, y1), (x2, y1), (x2, y2), (x1, y2)]))),
        area_code=area_code,
        name=area_code,
        hierarchy="planungsraum",
        boundary_edition="2021",
    )


def test_nearby_roads_use_metric_distance_sort_and_inclusive_radius():
    x, y = 390000, 5819000
    segments = [
        road_segment("road-b", [(x - 100, y + 20), (x + 100, y + 20)], None),
        road_segment("road-a", [(x - 100, y), (x + 100, y)], 1000),
    ]
    lon, lat = TO_WGS84.transform(x, y)
    nearby = road_segments_near_point(segments, lon, lat, 100)
    assert [item.record_id for item in nearby] == ["road-a", "road-b"]
    assert nearby[0].straight_line_distance_m == pytest.approx(0, abs=1e-8)
    assert nearby[1].straight_line_distance_m == pytest.approx(20, abs=0.01)
    assert nearby[1].dtv_motor_vehicles_per_day is None
    assert (
        len(road_segments_near_point(segments, lon, lat, nearby[1].straight_line_distance_m)) == 2
    )
    with pytest.raises(ValueError, match="finite"):
        road_segments_near_point(segments, float("nan"), lat, 1)
    with pytest.raises(ValueError, match="nonnegative"):
        road_segments_near_point(segments, lon, lat, -1)


def test_area_summary_uses_clipped_metric_lengths_and_excludes_touch_only():
    x, y = 390000, 5819000
    segments = [
        road_segment("road-1000", [(x - 20, y + 50), (x + 120, y + 50)], 1000),
        road_segment("road-missing", [(x, y + 70), (x + 100, y + 70)], None),
        road_segment("road-3000", [(x, y + 90), (x + 50, y + 90)], 3000),
        road_segment("corner-touch", [(x + 100, y + 100), (x + 200, y + 200)], 5000),
    ]
    areas = [
        area("inside", (x, y, x + 100, y + 100)),
        area("empty", (x + 500, y, x + 600, y + 100)),
    ]
    summaries = summarize_road_traffic_by_area(segments, areas)

    assert summaries["inside"].segment_count == 3
    assert summaries["inside"].segments_with_volume == 2
    assert summaries["inside"].intersecting_length_m == pytest.approx(250)
    assert summaries["inside"].length_with_volume_m == pytest.approx(150)
    assert summaries["inside"].max_dtv_motor_vehicles_per_day == 3000
    assert summaries["inside"].length_weighted_dtv_motor_vehicles_per_day == pytest.approx(5000 / 3)
    assert summaries["empty"].segment_count == 0
    assert summaries["empty"].intersecting_length_m == 0
    assert summaries["empty"].max_dtv_motor_vehicles_per_day is None
    assert summaries["empty"].length_weighted_dtv_motor_vehicles_per_day is None


def test_area_summary_empty_and_mixed_periods():
    sample_area = area("empty", (390000, 5819000, 390100, 5819100))
    empty = summarize_road_traffic_by_area([], [sample_area])["empty"]
    assert empty.reference_period is None
    assert empty.segment_count == 0
    assert empty.length_weighted_dtv_motor_vehicles_per_day is None
    with pytest.raises(ValueError, match="mixed reference periods"):
        summarize_road_traffic_by_area(
            [
                road_segment("2019", [(390000, 5819000), (390100, 5819000)], 1),
                road_segment("2020", [(390000, 5819010), (390100, 5819010)], 1, "2020"),
            ],
            [sample_area],
        )
