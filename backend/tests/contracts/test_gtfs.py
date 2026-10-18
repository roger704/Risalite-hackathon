"""Offline contract tests for reviewed GTFS normalization and counting rules."""

import hashlib
import json
import zipfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
import pytest

from app.connectors.gtfs import GTFSConnector
from app.domain import ConfigError, DatasetConfig, RawAsset, RawBundle, SchemaError, StopService

FIXTURES = Path(__file__).parents[1] / "fixtures" / "gtfs"
CONFIG_PATH = Path(__file__).parents[2] / "config" / "datasets" / "vbb_gtfs.json"


def config(**changes):
    values = json.loads(CONFIG_PATH.read_text())
    configured = DatasetConfig(**values)
    options = {**configured.parser_options, "valid_from": "2026-11-01", "valid_to": "2026-11-30"}
    options.update(changes)
    return replace(configured, parser_options=options)


def make_zip(directory, overrides=None):
    files = {path.name: path.read_text() for path in FIXTURES.glob("*.txt")}
    for name, content in (overrides or {}).items():
        if content is None:
            files.pop(name, None)
        else:
            files[name] = content
    target = directory / "synthetic-gtfs.zip"
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return target


def raw(path):
    content = path.read_bytes()
    return RawBundle(
        assets=(
            RawAsset(
                path=str(path),
                sha256=hashlib.sha256(content).hexdigest(),
                size_bytes=len(content),
                resource_url="https://www.vbb.de/gtfs",
            ),
        ),
        snapshot_id="test-vbb-gtfs",
        request_fingerprint="test",
        retrieved_at=datetime.now(timezone.utc),
    )


def services(batch, stop_id=None):
    return [
        record
        for record in batch.records
        if isinstance(record, StopService) and (stop_id is None or record.stop_id == stop_id)
    ]


def service(batch, stop_id, start):
    return next(record for record in services(batch, stop_id) if record.window_start == start)


def test_scheduled_departures_deduplicate_stations_and_expand_frequencies(tmp_path):
    connector = GTFSConnector()
    batch = connector.normalize(raw(make_zip(tmp_path)), config())
    report = connector.validate(batch)

    assert report.valid
    assert {(record.stop_id, record.window_start) for record in services(batch)} == {
        (stop_id, window) for stop_id in ("S1", "B2", "B3") for window in ("07:00:00", "16:00:00")
    }
    s1_morning = service(batch, "S1", "07:00:00")
    assert (s1_morning.departures, s1_morning.trip_count, s1_morning.route_count) == (1, 1, 1)
    assert s1_morning.route_types == (1,)
    assert s1_morning.quality_flags == ("collapsed_duplicate_stop_events=1",)

    b2_morning = service(batch, "B2", "07:00:00")
    assert (b2_morning.departures, b2_morning.trip_count, b2_morning.route_count) == (4, 2, 1)
    assert b2_morning.route_types == (3,)
    assert b2_morning.quality_flags == ("frequency_headway_estimate",)
    assert service(batch, "B3", "07:00:00").departures == 0
    assert service(batch, "S1", "16:00:00").departures == 0
    assert service(batch, "B2", "16:00:00").departures == 0
    b3_afternoon = service(batch, "B3", "16:00:00")
    assert (b3_afternoon.departures, b3_afternoon.trip_count, b3_afternoon.route_count) == (1, 1, 1)
    assert b3_afternoon.route_types == (3,)
    assert any("2 stations, 5 departures" in warning for warning in batch.warnings)


def test_transit_stops_preserve_location_parent_and_wheelchair_values(tmp_path):
    batch = GTFSConnector().normalize(raw(make_zip(tmp_path)), config())
    stops = {
        record.stop_id: record for record in batch.records if record.record_type == "transit_stop"
    }
    assert "N1" not in stops
    assert stops["E1"].parent_station == "S1"
    assert stops["P1a"].wheelchair_boarding == 1
    assert stops["P1b"].wheelchair_boarding is None
    assert stops["B2"].wheelchair_boarding == 0
    assert any(
        "1 stops with unselected location_type values skipped" == item for item in batch.warnings
    )


def test_type_4_boarding_area_parent_is_valid_when_type_is_not_emitted(tmp_path):
    stops = (FIXTURES / "stops.txt").read_text()
    stops += "BA1,Boarding Area,52.5005,13.4005,4,P1a,0\n"
    batch = GTFSConnector().normalize(raw(make_zip(tmp_path, {"stops.txt": stops})), config())

    assert GTFSConnector().validate(batch).valid
    assert "BA1" not in {
        record.stop_id for record in batch.records if record.record_type == "transit_stop"
    }


def test_type_4_event_aggregates_at_platform_station_without_double_counting(tmp_path):
    stops = (FIXTURES / "stops.txt").read_text()
    stops += "BA1,Boarding Area,52.5005,13.4005,4,P1a,0\n"
    stop_times = (FIXTURES / "stop_times.txt").read_text()
    stop_times += "T1,BA1,2,0,0,,07:10:30,07:10:30\n"
    batch = GTFSConnector().normalize(
        raw(make_zip(tmp_path, {"stops.txt": stops, "stop_times.txt": stop_times})),
        config(stop_location_types=[0, 1, 2, 4]),
    )

    s1_morning = service(batch, "S1", "07:00:00")
    assert s1_morning.departures == 1
    assert s1_morning.quality_flags == ("collapsed_duplicate_stop_events=2",)
    assert "BA1" in {
        record.stop_id for record in batch.records if record.record_type == "transit_stop"
    }
    assert GTFSConnector().validate(batch).valid


def test_type_4_without_station_ancestor_uses_topmost_platform_key(tmp_path):
    stops = (
        (FIXTURES / "stops.txt")
        .read_text()
        .replace(
            "P1a,Platform One A,52.5001,13.4001,0,S1,1",
            "P1a,Platform One A,52.5001,13.4001,0,,1",
        )
    )
    stops += "BA1,Boarding Area,52.5005,13.4005,4,P1a,0\n"
    stop_times = (FIXTURES / "stop_times.txt").read_text()
    stop_times += "T1,BA1,2,0,0,,07:10:30,07:10:30\n"
    batch = GTFSConnector().normalize(
        raw(make_zip(tmp_path, {"stops.txt": stops, "stop_times.txt": stop_times})),
        config(stop_location_types=[0, 1, 2, 4]),
    )

    assert service(batch, "P1a", "07:00:00").departures == 1
    assert GTFSConnector().validate(batch).valid


def test_type_0_stop_cannot_have_type_0_parent(tmp_path):
    stops = (
        (FIXTURES / "stops.txt")
        .read_text()
        .replace(
            "P1a,Platform One A,52.5001,13.4001,0,S1,1",
            "P1a,Platform One A,52.5001,13.4001,0,P1b,1",
        )
    )
    with pytest.raises(SchemaError, match="parent_station"):
        GTFSConnector().normalize(raw(make_zip(tmp_path, {"stops.txt": stops})), config())


def test_child_of_orphaned_parent_is_excluded_with_its_ancestor_chain(tmp_path):
    stops = (
        (FIXTURES / "stops.txt")
        .read_text()
        .replace(
            "P1a,Platform One A,52.5001,13.4001,0,S1,1",
            "P1a,Platform One A,52.5001,13.4001,0,MISSING,1",
        )
    )
    stops += "BA1,Boarding Area,52.5005,13.4005,4,P1a,0\n"
    stop_times = (FIXTURES / "stop_times.txt").read_text()
    stop_times += "T1,BA1,2,0,0,,07:10:30,07:10:30\n"
    batch = GTFSConnector().normalize(
        raw(make_zip(tmp_path, {"stops.txt": stops, "stop_times.txt": stop_times})),
        config(
            stop_location_types=[0, 1, 2, 4],
            foreign_key_max_orphan_ratio=0.5,
        ),
    )
    stop_ids = {record.stop_id for record in batch.records if record.record_type == "transit_stop"}
    assert {"P1a", "BA1"}.isdisjoint(stop_ids)
    assert any(
        "1 orphaned stops.parent_station references excluded" in item for item in batch.warnings
    )
    assert any("1 stops in orphaned parent chains excluded" in item for item in batch.warnings)
    assert GTFSConnector().validate(batch).valid


def test_archive_headers_accept_utf8_bom_and_surrounding_whitespace(tmp_path):
    agency = (FIXTURES / "agency.txt").read_text()
    agency = agency.replace("agency_id,", "\ufeff agency_id,").replace(
        ",agency_timezone", ", agency_timezone "
    )
    batch = GTFSConnector().normalize(raw(make_zip(tmp_path, {"agency.txt": agency})), config())
    assert GTFSConnector().validate(batch).valid


def test_previous_service_day_after_midnight_shift(tmp_path):
    c = config(
        service_windows=[{"start": "01:00:00", "end": "02:00:00"}],
    )
    batch = GTFSConnector().normalize(raw(make_zip(tmp_path)), c)
    b3 = services(batch, "B3")[0]
    assert (b3.departures, b3.trip_count, b3.route_types) == (1, 1, (3,))
    assert GTFSConnector().validate(batch).valid


def test_stop_times_orphans_fail_at_zero_threshold_and_warn_when_allowed(tmp_path):
    archive = make_zip(
        tmp_path,
        {
            "stop_times.txt": (
                (FIXTURES / "stop_times.txt").read_text() + "T2,UNKNOWN,0,0,0,,09:00:00,09:00:00\n"
            )
        },
    )
    with pytest.raises(SchemaError, match="stop_times.stop_id.*1 orphaned"):
        GTFSConnector().normalize(raw(archive), config(foreign_key_max_orphan_ratio=0.0))
    batch = GTFSConnector().normalize(raw(archive), config(foreign_key_max_orphan_ratio=0.5))
    assert batch.rejected_rows == 0
    assert any(
        "1 orphaned stop_times.stop_id references excluded" in item for item in batch.warnings
    )
    assert GTFSConnector().validate(batch).valid


@pytest.mark.parametrize(
    "changes",
    [
        {"service_date": "2026-12-01"},
        {"service_date": "2026-11-01"},
    ],
)
def test_service_date_must_be_strictly_inside_configured_validity(tmp_path, changes):
    with pytest.raises(ConfigError, match="service_date"):
        GTFSConnector().normalize(raw(make_zip(tmp_path)), config(**changes))


def test_dst_transition_service_day_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="DST transition"):
        GTFSConnector().normalize(
            raw(make_zip(tmp_path)),
            config(valid_from="2026-10-25", service_date="2026-10-26"),
        )


def test_service_window_missing_required_key_is_config_error(tmp_path):
    with pytest.raises(ConfigError, match="requires start and end"):
        GTFSConnector().normalize(
            raw(make_zip(tmp_path)),
            config(service_windows=[{"start": "07:00:00", "other": "10:00:00"}]),
        )


def test_feed_edition_must_match_reviewed_config(tmp_path):
    with pytest.raises(SchemaError, match="feed edition differs"):
        GTFSConnector().normalize(raw(make_zip(tmp_path)), config(valid_from="2026-11-02"))


def test_agency_timezone_must_match_config(tmp_path):
    agency = (FIXTURES / "agency.txt").read_text().replace("Europe/Berlin", "Europe/Paris")
    with pytest.raises(SchemaError, match="agency_timezone"):
        GTFSConnector().normalize(raw(make_zip(tmp_path, {"agency.txt": agency})), config())


@pytest.mark.parametrize(
    ("overrides", "html"),
    [
        ({"routes.txt": None}, False),
        ({}, True),
    ],
)
def test_missing_required_archive_member_and_html_payload_are_rejected(tmp_path, overrides, html):
    connector = GTFSConnector()
    if not html:
        path = make_zip(tmp_path, overrides)
    else:
        path = tmp_path / "landing-page.zip"
        path.write_text("<!doctype html><html>not a feed</html>")
    message = "HTML" if html else "missing required files"
    with pytest.raises(SchemaError, match=message):
        connector.normalize(raw(path), config())


def test_malformed_departure_time_is_rejected(tmp_path):
    stop_times = (FIXTURES / "stop_times.txt").read_text()
    stop_times += "T2,B2,3,0,0,,08:50:00,08:xx:00\n"
    batch = GTFSConnector().normalize(
        raw(make_zip(tmp_path, {"stop_times.txt": stop_times})), config()
    )
    assert batch.rejected_rows == 1
    assert not GTFSConnector().validate(batch).valid
    assert any("malformed times" in message for message in batch.rejections)


@pytest.mark.live
def test_live_vbb_gtfs_resource_is_reviewed_zip():
    values = json.loads(CONFIG_PATH.read_text())
    allowed_hosts = set(values["allowed_hosts"])
    url = values["resource_url"]
    response = None
    with httpx.Client(timeout=60, follow_redirects=False) as client:
        for _ in range(6):
            request = client.build_request("GET", url, headers={"Range": "bytes=0-3"})
            response = client.send(request, stream=True)
            host = urlsplit(url).hostname
            assert host in allowed_hosts
            if response.is_redirect:
                next_url = urljoin(url, response.headers["location"])
                assert urlsplit(next_url).hostname in allowed_hosts
                response.close()
                url = next_url
                continue
            break
        assert response is not None
        try:
            assert response.status_code in {200, 206}
            assert response.headers.get("content-type", "").split(";")[0] == "application/zip"
            assert int(response.headers["content-length"]) <= values["max_download_bytes"]
            assert next(response.iter_bytes())[:4] == b"PK\x03\x04"
        finally:
            response.close()
