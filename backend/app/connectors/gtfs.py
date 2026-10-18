"""VBB GTFS normalization.

After-midnight GTFS times on service day D-1 are shifted back by 24 hours and
can count in D's windows. Last-stop events and pickup_type=1 events are excluded.
Departures are deduplicated by station, service day, trip, and frequency run;
windows include their start and exclude their end.
"""

from __future__ import annotations

import csv
import io
import re
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import numpy as np
import pyarrow as pa
import pyarrow.csv as pacsv

from app.connectors.base import FileConnector
from app.domain import (
    ConfigError,
    DatasetConfig,
    NormalizedBatch,
    QualityReport,
    RawBundle,
    SchemaError,
    StopService,
    TransitStop,
)

_TIME = re.compile(r"^\d{1,3}:[0-5]\d:[0-5]\d$")
_DATE = re.compile(r"^\d{8}$")
_STOP_TIME_COLUMNS = ("trip_id", "stop_id", "stop_sequence", "departure_time")
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _safe_name(name: str) -> None:
    path = PurePosixPath(name)
    if not name or "\\" in name or path.is_absolute() or ".." in path.parts or ":" in name:
        raise SchemaError("Archive contains an unsafe member path")


def _seconds(value: str) -> int:
    if not _TIME.fullmatch(value):
        raise ValueError(f"invalid GTFS time {value!r}")
    hours, minutes, seconds = (int(part) for part in value.split(":"))
    return hours * 3600 + minutes * 60 + seconds


def _iso_gtfs_date(value: str) -> date:
    if not _DATE.fullmatch(value):
        raise ValueError(f"invalid GTFS date {value!r}")
    return datetime.strptime(value, "%Y%m%d").date()


def _config_date(value: object, name: str) -> date:
    if not isinstance(value, str):
        raise ConfigError(f"{name} must be an ISO calendar date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an ISO calendar date") from exc


def _read_table(archive: ZipFile, filename: str, required: set[str]) -> list[dict[str, str]]:
    try:
        with archive.open(filename) as member:
            text = member.read().decode("utf-8-sig")
    except (UnicodeError, OSError, KeyError) as exc:
        raise SchemaError(f"Cannot read GTFS {filename}: {exc}") from exc
    try:
        reader = csv.reader(io.StringIO(text, newline=""))
        headers = next(reader)
        headers = [header.strip() for header in headers]
        if len(headers) != len(set(headers)) or not required.issubset(headers):
            raise SchemaError(
                f"GTFS {filename} is missing required columns: {sorted(required - set(headers))}"
            )
        rows = []
        for number, values in enumerate(reader, 2):
            if len(values) != len(headers):
                raise SchemaError(f"GTFS {filename} row {number} has an invalid column count")
            rows.append(dict(zip(headers, values)))
        return rows
    except (csv.Error, StopIteration) as exc:
        raise SchemaError(f"Cannot parse GTFS {filename}: {exc}") from exc


def _ratio_check(name: str, orphans: int, rows: int, threshold: float) -> str | None:
    if rows and orphans / rows > threshold:
        raise SchemaError(f"Foreign-key check {name} found {orphans} orphaned rows out of {rows}")
    return f"{orphans} orphaned {name} references excluded" if orphans else None


def _parse_windows(value: object) -> list[tuple[str, str, int, int]]:
    if not isinstance(value, list) or not value:
        raise ConfigError("service_windows must be a nonempty list")
    result = []
    for window in value:
        if not isinstance(window, dict) or not {"start", "end"}.issubset(window):
            raise ConfigError("Each service window requires start and end")
        start, end = window["start"], window["end"]
        pattern = re.compile(r"^(?:[01]\d|2[0-4]):[0-5]\d:[0-5]\d$")
        if not isinstance(start, str) or not isinstance(end, str):
            raise ConfigError("Service window values must be HH:MM:SS strings")
        if not pattern.fullmatch(start) or not pattern.fullmatch(end):
            raise ConfigError("Service windows must use HH:MM:SS")
        start_seconds, end_seconds = _seconds(start), _seconds(end)
        if start_seconds >= end_seconds or end_seconds > 86400:
            raise ConfigError("Service windows require 00:00:00 <= start < end <= 24:00:00")
        if start_seconds == 86400:
            raise ConfigError("Service window start cannot be 24:00:00")
        result.append((start, end, start_seconds, end_seconds))
    return result


def _is_dst_transition(day: date, zone: ZoneInfo) -> bool:
    midnight = datetime.combine(day, time.min, tzinfo=zone)
    next_midnight = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)
    return midnight.utcoffset() != next_midnight.utcoffset()


class GTFSConnector(FileConnector):
    adapter_version = "1.0.0"

    def normalize(self, raw: RawBundle, config: DatasetConfig) -> NormalizedBatch:
        if len(raw.assets) != 1:
            raise SchemaError("GTFS import requires exactly one ZIP asset")
        options = config.parser_options
        required_options = (
            "max_unpacked_bytes",
            "valid_from",
            "valid_to",
            "timezone",
            "service_date",
            "service_windows",
            "stop_location_types",
            "foreign_key_max_orphan_ratio",
        )
        missing_options = [name for name in required_options if name not in options]
        if missing_options:
            raise ConfigError(f"GTFS parser configuration is missing {missing_options}")
        max_unpacked = options["max_unpacked_bytes"]
        if type(max_unpacked) is not int or max_unpacked <= 0:
            raise ConfigError("max_unpacked_bytes must be a positive integer")
        threshold = options["foreign_key_max_orphan_ratio"]
        if (
            isinstance(threshold, bool)
            or not isinstance(threshold, (int, float))
            or not 0 <= threshold <= 1
        ):
            raise ConfigError("foreign_key_max_orphan_ratio must be between 0 and 1")
        reviewed_sha256 = options.get("reviewed_sha256")
        if not isinstance(reviewed_sha256, str) or not re.fullmatch(
            r"[0-9a-fA-F]{64}", reviewed_sha256
        ):
            raise ConfigError("reviewed_sha256 must be a 64-character SHA-256 digest")
        timezone_name = options["timezone"]
        if not isinstance(timezone_name, str) or not timezone_name:
            raise ConfigError("timezone must be an IANA timezone name")
        try:
            zone = ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ConfigError(f"Unknown timezone: {timezone_name}") from exc
        valid_from = _config_date(options["valid_from"], "valid_from")
        valid_to = _config_date(options["valid_to"], "valid_to")
        service_date = _config_date(options["service_date"], "service_date")
        if valid_from > valid_to:
            raise ConfigError("valid_from must not be after valid_to")
        if not valid_from < service_date <= valid_to:
            raise ConfigError("service_date must be after valid_from and on or before valid_to")
        if any(
            _is_dst_transition(day, zone)
            for day in (service_date - timedelta(days=1), service_date)
        ):
            raise ConfigError("service_date or its previous day is a DST transition day")
        windows = _parse_windows(options["service_windows"])
        stop_location_types = options["stop_location_types"]
        if not isinstance(stop_location_types, list) or any(
            type(value) is not int or value not in range(5) for value in stop_location_types
        ):
            raise ConfigError("stop_location_types must be a list of GTFS location type integers")

        source = Path(raw.assets[0].path)
        try:
            with source.open("rb") as source_file:
                prefix = source_file.read(512).lstrip().lower()
            if prefix.startswith((b"<!doctype html", b"<html")) or b"<html" in prefix:
                raise SchemaError("GTFS resource returned HTML instead of a ZIP archive")
            with ZipFile(source) as archive:
                members = archive.infolist()
                if len(members) > 1000:
                    raise SchemaError("GTFS archive contains too many members")
                if sum(member.file_size for member in members) > max_unpacked:
                    raise SchemaError("GTFS archive expansion budget exceeded")
                names: dict[str, str] = {}
                seen = set()
                for member in members:
                    _safe_name(member.filename)
                    if (member.external_attr >> 16) & 0o170000 == 0o120000:
                        raise SchemaError("GTFS archive links are not permitted")
                    canonical = str(PurePosixPath(member.filename))
                    if canonical in seen:
                        raise SchemaError("GTFS archive contains duplicate member paths")
                    seen.add(canonical)
                    if member.is_dir():
                        continue
                    basename = PurePosixPath(member.filename).name
                    if basename in names:
                        raise SchemaError(f"GTFS archive contains duplicate {basename}")
                    names[basename] = member.filename
                required_files = {
                    "agency.txt",
                    "stops.txt",
                    "routes.txt",
                    "trips.txt",
                    "stop_times.txt",
                }
                missing_files = required_files - names.keys()
                if missing_files:
                    raise SchemaError(
                        f"GTFS archive is missing required files: {sorted(missing_files)}"
                    )
                if not {"calendar.txt", "calendar_dates.txt"} & names.keys():
                    raise SchemaError("GTFS archive requires calendar.txt or calendar_dates.txt")
                result = self._normalize_archive(
                    archive,
                    names,
                    raw,
                    config,
                    zone,
                    valid_from,
                    valid_to,
                    service_date,
                    windows,
                    stop_location_types,
                    float(threshold),
                )
                return result
        except SchemaError:
            raise
        except BadZipFile as exc:
            raise SchemaError("GTFS resource is not a valid ZIP archive") from exc
        except (OSError, EOFError, ValueError) as exc:
            raise SchemaError(f"Cannot read GTFS archive: {exc}") from exc

    def _normalize_archive(
        self,
        archive: ZipFile,
        names: dict[str, str],
        raw: RawBundle,
        config: DatasetConfig,
        zone: ZoneInfo,
        valid_from: date,
        valid_to: date,
        service_date: date,
        windows: list[tuple[str, str, int, int]],
        stop_location_types: list[int],
        threshold: float,
    ) -> NormalizedBatch:
        warnings = list(config.parser_options.get("limitations", ()))
        if raw.assets[0].sha256.lower() != config.parser_options["reviewed_sha256"].lower():
            warnings.append("GTFS archive SHA-256 differs from reviewed_sha256.")

        agency = _read_table(archive, names["agency.txt"], {"agency_timezone"})
        stops = _read_table(
            archive,
            names["stops.txt"],
            {"stop_id", "stop_name", "stop_lat", "stop_lon"},
        )
        routes = _read_table(archive, names["routes.txt"], {"route_id", "route_type"})
        trips = _read_table(archive, names["trips.txt"], {"route_id", "service_id", "trip_id"})
        calendar = (
            _read_table(
                archive,
                names["calendar.txt"],
                {"service_id", "start_date", "end_date", *_WEEKDAYS},
            )
            if "calendar.txt" in names
            else []
        )
        calendar_dates = (
            _read_table(
                archive,
                names["calendar_dates.txt"],
                {"service_id", "date", "exception_type"},
            )
            if "calendar_dates.txt" in names
            else []
        )
        frequencies = (
            _read_table(
                archive,
                names["frequencies.txt"],
                {"trip_id", "start_time", "end_time", "headway_secs"},
            )
            if "frequencies.txt" in names
            else []
        )

        if not agency:
            raise SchemaError("GTFS agency.txt is empty")
        if any(row["agency_timezone"] != config.parser_options["timezone"] for row in agency):
            raise SchemaError("GTFS agency_timezone differs from configured timezone")
        agency_ids = {row.get("agency_id", "") for row in agency}
        if len(agency_ids) != len(agency):
            raise SchemaError("GTFS agency identifiers are empty or duplicated")

        date_values = []
        service_ids = set()
        calendar_by_service: dict[str, dict[str, str]] = {}
        for row in calendar:
            service_id = row["service_id"]
            if not service_id or service_id in calendar_by_service:
                raise SchemaError("GTFS calendar has an empty or duplicate service_id")
            try:
                start, end = _iso_gtfs_date(row["start_date"]), _iso_gtfs_date(row["end_date"])
            except ValueError as exc:
                raise SchemaError(f"GTFS calendar contains {exc}") from exc
            if start > end:
                raise SchemaError(f"GTFS calendar has an inverted date range for {service_id}")
            if any(row[weekday] not in {"0", "1"} for weekday in _WEEKDAYS):
                raise SchemaError(f"GTFS calendar has invalid weekday flags for {service_id}")
            date_values.extend((start, end))
            calendar_by_service[service_id] = row
            service_ids.add(service_id)
        calendar_dates_by_service: dict[str, dict[date, str]] = defaultdict(dict)
        for row in calendar_dates:
            service_id = row["service_id"]
            if not service_id:
                raise SchemaError("GTFS calendar_dates has an empty service_id")
            try:
                exception_date = _iso_gtfs_date(row["date"])
            except ValueError as exc:
                raise SchemaError(f"GTFS calendar_dates contains {exc}") from exc
            exception_type = row["exception_type"]
            if exception_type not in {"1", "2"}:
                raise SchemaError(
                    f"GTFS calendar_dates has invalid exception_type {exception_type!r}"
                )
            if exception_date in calendar_dates_by_service[service_id]:
                raise SchemaError(
                    f"GTFS calendar_dates duplicates {service_id} on {exception_date}"
                )
            calendar_dates_by_service[service_id][exception_date] = exception_type
            service_ids.add(service_id)
            if exception_type == "1":
                date_values.append(exception_date)
        if not date_values or min(date_values) != valid_from or max(date_values) != valid_to:
            raise SchemaError("GTFS feed edition differs from reviewed config")

        active_services = {}
        for day in (service_date - timedelta(days=1), service_date):
            day_services = {
                service_id
                for service_id, row in calendar_by_service.items()
                if _iso_gtfs_date(row["start_date"]) <= day <= _iso_gtfs_date(row["end_date"])
                and row[_WEEKDAYS[day.weekday()]] == "1"
            }
            for service_id, exception_map in calendar_dates_by_service.items():
                exception = exception_map.get(day)
                if exception == "1":
                    day_services.add(service_id)
                elif exception == "2":
                    day_services.discard(service_id)
            active_services[day] = day_services

        stop_index: dict[str, int] = {}
        stop_values = []
        stop_types = []
        stop_parents = []
        stop_records = []
        rejected = []
        skipped_types = 0
        emitted_type_rows = 0
        invalid_location_type_rows = 0
        for row_number, row in enumerate(stops, 2):
            stop_id = row["stop_id"]
            if not stop_id or stop_id in stop_index:
                raise SchemaError(
                    f"GTFS stops has an empty or duplicate stop_id at row {row_number}"
                )
            raw_type = row.get("location_type", "")
            try:
                location_type = int(raw_type) if raw_type else 0
                if location_type not in range(5):
                    raise ValueError
            except ValueError:
                rejected.append(f"stops row {row_number}: invalid location_type {raw_type!r}")
                invalid_location_type_rows += 1
                location_type = -1
            stop_index[stop_id] = len(stop_values)
            stop_values.append(row)
            stop_types.append(location_type)
            stop_parents.append(row.get("parent_station", "").strip())
            if location_type not in stop_location_types:
                if location_type >= 0:
                    skipped_types += 1
                continue
            emitted_type_rows += 1
            wheelchair_text = row.get("wheelchair_boarding", "").strip()
            wheelchair = None
            if wheelchair_text:
                if wheelchair_text not in {"0", "1", "2"}:
                    rejected.append(
                        f"stops row {row_number}: invalid wheelchair_boarding {wheelchair_text!r}"
                    )
                    continue
                wheelchair = int(wheelchair_text)
            name = row["stop_name"].strip()
            try:
                latitude, longitude = float(row["stop_lat"]), float(row["stop_lon"])
            except ValueError:
                latitude, longitude = float("nan"), float("nan")
            if not name or not np.isfinite(latitude) or not np.isfinite(longitude):
                rejected.append(f"stops row {row_number}: missing or invalid name/coordinates")
                continue
            if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
                rejected.append(f"stops row {row_number}: coordinates are outside WGS84 bounds")
                continue
            stop_records.append(
                TransitStop(
                    source_id=config.dataset_id,
                    snapshot_id=raw.snapshot_id,
                    source_record_id=stop_id,
                    record_id=stop_id,
                    geometry={"type": "Point", "coordinates": [longitude, latitude]},
                    stop_id=stop_id,
                    name=name,
                    location_type=location_type,
                    parent_station=stop_parents[-1] or None,
                    wheelchair_boarding=wheelchair,
                )
            )
        if skipped_types:
            warnings.append(f"{skipped_types} stops with unselected location_type values skipped")

        # Validate parent types and resolve the complete hierarchy before using child stops.
        parent_orphans = 0
        direct_orphan_stops = set()
        for index, parent in enumerate(stop_parents):
            child_type = stop_types[index]
            if child_type == 1 and parent:
                raise SchemaError("GTFS station stops cannot have parent_station")
            if child_type == 4 and not parent:
                raise SchemaError("GTFS location_type 4 requires a type-0 parent_station")
            if not parent:
                continue
            parent_index = stop_index.get(parent)
            if parent_index is None:
                parent_orphans += 1
                direct_orphan_stops.add(index)
                continue
            if child_type < 0:
                continue
            expected_parent_type = 0 if child_type == 4 else 1
            if stop_types[parent_index] != expected_parent_type:
                raise SchemaError(
                    f"GTFS stops.parent_station target {parent!r} must have "
                    f"location_type {expected_parent_type}"
                )
        warning = _ratio_check("stops.parent_station", parent_orphans, len(stops), threshold)
        if warning:
            warnings.append(warning)
        resolved_station_by_stop: list[str | None] = [None] * len(stop_values)
        orphaned_parent_stops = set()
        for index in range(len(stop_values)):
            chain = []
            visited = set()
            current = index
            while True:
                if current in visited:
                    raise SchemaError("GTFS stops.parent_station hierarchy contains a cycle")
                visited.add(current)
                chain.append(current)
                parent = stop_parents[current]
                if not parent:
                    break
                parent_index = stop_index.get(parent)
                if parent_index is None:
                    orphaned_parent_stops.add(index)
                    break
                current = parent_index
            if index not in orphaned_parent_stops:
                station_index = next(
                    (ancestor for ancestor in chain if stop_types[ancestor] == 1), chain[-1]
                )
                resolved_station_by_stop[index] = stop_values[station_index]["stop_id"]
        cascading_orphans = orphaned_parent_stops - direct_orphan_stops
        if cascading_orphans:
            warnings.append(f"{len(cascading_orphans)} stops in orphaned parent chains excluded")
        stop_records = [
            record
            for record in stop_records
            if stop_index[record.stop_id] not in orphaned_parent_stops
        ]
        if orphaned_parent_stops:
            stop_index = {
                stop_id: index
                for stop_id, index in stop_index.items()
                if index not in orphaned_parent_stops
            }

        route_types = {}
        route_ids = {}
        seen_route_ids = set()
        route_agency_orphans = 0
        for row in routes:
            route_id = row["route_id"]
            if not route_id or route_id in seen_route_ids:
                raise SchemaError("GTFS routes has an empty or duplicate route_id")
            seen_route_ids.add(route_id)
            agency_id = row.get("agency_id", "")
            if not agency_id and len(agency) != 1 or agency_id and agency_id not in agency_ids:
                route_agency_orphans += 1
                continue
            try:
                route_type = int(row["route_type"])
            except ValueError as exc:
                raise SchemaError(f"GTFS routes has invalid route_type for {route_id}") from exc
            route_ids[route_id] = len(route_ids)
            route_types[route_ids[route_id]] = route_type
        warning = _ratio_check("routes.agency_id", route_agency_orphans, len(routes), threshold)
        if warning:
            warnings.append(warning)

        trip_route_codes = []
        trip_service_ids = []
        trip_index = {}
        seen_trip_ids = set()
        trip_route_orphans = 0
        trip_service_orphans = 0
        for row in trips:
            trip_id = row["trip_id"]
            if not trip_id or trip_id in seen_trip_ids:
                raise SchemaError("GTFS trips has an empty or duplicate trip_id")
            seen_trip_ids.add(trip_id)
            route_code = route_ids.get(row["route_id"])
            route_ok = route_code is not None
            service_ok = row["service_id"] in service_ids
            if not route_ok:
                trip_route_orphans += 1
            if not service_ok:
                trip_service_orphans += 1
            if not route_ok or not service_ok:
                continue
            trip_index[trip_id] = len(trip_route_codes)
            trip_route_codes.append(route_code)
            trip_service_ids.append(row["service_id"])
        warning = _ratio_check("trips.route_id", trip_route_orphans, len(trips), threshold)
        if warning:
            warnings.append(warning)
        warning = _ratio_check("trips.service_id", trip_service_orphans, len(trips), threshold)
        if warning:
            warnings.append(warning)
        if not trip_index:
            raise SchemaError("GTFS trips has no rows with valid route and service references")
        trip_route_codes_array = np.asarray(trip_route_codes, dtype=np.int32)
        trip_service_active = np.zeros((2, len(trip_index)), dtype=np.bool_)
        days = (service_date - timedelta(days=1), service_date)
        for day_index, day in enumerate(days):
            trip_service_active[day_index] = np.fromiter(
                (service_id in active_services[day] for service_id in trip_service_ids),
                dtype=np.bool_,
                count=len(trip_service_ids),
            )

        # Parse stop_times in bounded Arrow batches and retain only compact numeric arrays.
        stop_times_name = names["stop_times.txt"]
        with archive.open(stop_times_name) as member:
            header_line = member.readline()
        try:
            header = next(csv.reader([header_line.decode("utf-8-sig")]))
        except (UnicodeError, csv.Error, StopIteration) as exc:
            raise SchemaError("GTFS stop_times.txt has an invalid header") from exc
        header = [field.strip() for field in header]
        required_stop_columns = set(_STOP_TIME_COLUMNS)
        if len(header) != len(set(header)) or not required_stop_columns.issubset(header):
            raise SchemaError(
                f"GTFS stop_times.txt is missing required columns: "
                f"{sorted(required_stop_columns - set(header))}"
            )
        selected_columns = [*_STOP_TIME_COLUMNS]
        if "pickup_type" in header:
            selected_columns.append("pickup_type")
        trip_chunks, stop_chunks, sequence_chunks, departure_chunks, pickup_chunks = (
            [],
            [],
            [],
            [],
            [],
        )
        stop_time_rows = 0
        empty_departures = 0
        rejected_stop_times = 0
        orphan_trip_rows = 0
        orphan_stop_rows = 0
        try:
            with archive.open(stop_times_name) as member:
                reader = pacsv.open_csv(
                    member,
                    read_options=pacsv.ReadOptions(
                        column_names=header,
                        skip_rows=1,
                        block_size=8 * 1024 * 1024,
                    ),
                    parse_options=pacsv.ParseOptions(delimiter=","),
                    convert_options=pacsv.ConvertOptions(
                        include_columns=selected_columns,
                        column_types={name: pa.string() for name in selected_columns},
                        strings_can_be_null=False,
                    ),
                )
                for batch in reader:
                    columns = {
                        name: batch.column(batch.schema.get_field_index(name)).to_pylist()
                        for name in selected_columns
                    }
                    size = batch.num_rows
                    stop_time_rows += size
                    trip_codes = np.empty(size, dtype=np.int32)
                    stop_codes = np.empty(size, dtype=np.int32)
                    sequences = np.full(size, -1, dtype=np.int32)
                    departures = np.full(size, -1, dtype=np.int32)
                    pickups = np.zeros(size, dtype=np.int8)
                    for index in range(size):
                        trip_code = trip_index.get(columns["trip_id"][index], -1)
                        stop_code = stop_index.get(columns["stop_id"][index], -1)
                        trip_codes[index] = trip_code
                        stop_codes[index] = stop_code
                        if trip_code < 0:
                            orphan_trip_rows += 1
                        if stop_code < 0:
                            orphan_stop_rows += 1
                        malformed = False
                        try:
                            sequences[index] = int(columns["stop_sequence"][index])
                            if sequences[index] < 0:
                                raise ValueError
                        except ValueError:
                            malformed = True
                        departure = columns["departure_time"][index]
                        if not departure:
                            empty_departures += 1
                        else:
                            try:
                                departures[index] = _seconds(departure)
                            except ValueError:
                                malformed = True
                        if "pickup_type" in columns:
                            pickups[index] = columns["pickup_type"][index] == "1"
                        if malformed:
                            rejected_stop_times += 1
                    trip_chunks.append(trip_codes)
                    stop_chunks.append(stop_codes)
                    sequence_chunks.append(sequences)
                    departure_chunks.append(departures)
                    pickup_chunks.append(pickups)
        except (pa.ArrowException, OSError, UnicodeError, ValueError) as exc:
            raise SchemaError(f"Cannot stream GTFS stop_times.txt: {exc}") from exc
        warning = _ratio_check("stop_times.trip_id", orphan_trip_rows, stop_time_rows, threshold)
        if warning:
            warnings.append(warning)
        warning = _ratio_check("stop_times.stop_id", orphan_stop_rows, stop_time_rows, threshold)
        if warning:
            warnings.append(warning)
        trip_codes = np.concatenate(trip_chunks) if trip_chunks else np.empty(0, dtype=np.int32)
        stop_codes = np.concatenate(stop_chunks) if stop_chunks else np.empty(0, dtype=np.int32)
        sequences = (
            np.concatenate(sequence_chunks) if sequence_chunks else np.empty(0, dtype=np.int32)
        )
        departures = (
            np.concatenate(departure_chunks) if departure_chunks else np.empty(0, dtype=np.int32)
        )
        pickups = np.concatenate(pickup_chunks) if pickup_chunks else np.empty(0, dtype=np.int8)
        del trip_chunks, stop_chunks, sequence_chunks, departure_chunks, pickup_chunks
        if rejected_stop_times:
            rejected.append(
                f"{rejected_stop_times} stop_times rows contain malformed times or sequences"
            )
        if empty_departures:
            warnings.append(
                f"{empty_departures} stop_times rows with empty departure_time excluded; no interpolation"
            )

        # Only stop-time references define the station keys to publish.
        referenced_stop_codes = np.unique(stop_codes[stop_codes >= 0])
        station_by_stop = np.full(len(stop_values), -1, dtype=np.int32)
        station_keys = []
        station_key_codes = {}
        for stop_code in referenced_stop_codes:
            station = resolved_station_by_stop[stop_code]
            if station is None:
                continue
            if station not in station_key_codes:
                station_key_codes[station] = len(station_keys)
                station_keys.append(station)
            station_by_stop[stop_code] = station_key_codes[station]

        # Route/trip/time metadata needed for departure event aggregation.
        max_sequence = np.full(len(trip_index), -1, dtype=np.int32)
        first_sequence = np.full(len(trip_index), np.iinfo(np.int32).max, dtype=np.int32)
        first_departure = np.full(len(trip_index), np.iinfo(np.int32).max, dtype=np.int32)
        valid_trip_rows_mask = trip_codes >= 0
        seq_mask = valid_trip_rows_mask & (sequences >= 0)
        np.maximum.at(max_sequence, trip_codes[seq_mask], sequences[seq_mask])
        dep_mask = seq_mask & (departures >= 0)
        np.minimum.at(first_sequence, trip_codes[dep_mask], sequences[dep_mask])
        first_rows = dep_mask & (sequences == first_sequence[np.maximum(trip_codes, 0)])
        np.minimum.at(
            first_departure,
            trip_codes[first_rows],
            departures[first_rows],
        )
        base_events = (
            valid_trip_rows_mask
            & (stop_codes >= 0)
            & seq_mask
            & (departures >= 0)
            & (sequences < max_sequence[np.maximum(trip_codes, 0)])
            & (pickups != 1)
        )

        frequency_by_trip: dict[int, list[tuple[int, int, int, bool]]] = defaultdict(list)
        frequency_orphans = 0
        for row in frequencies:
            trip_code = trip_index.get(row["trip_id"], -1)
            if trip_code < 0:
                frequency_orphans += 1
                continue
            try:
                start, end = _seconds(row["start_time"]), _seconds(row["end_time"])
                headway = int(row["headway_secs"])
            except ValueError as exc:
                raise SchemaError(f"GTFS frequencies has invalid time or headway: {exc}") from exc
            if start >= end or headway <= 0:
                raise SchemaError("GTFS frequencies requires start < end and positive headway_secs")
            exact = row.get("exact_times", "") or "0"
            if exact not in {"0", "1"}:
                raise SchemaError(f"GTFS frequencies has invalid exact_times {exact!r}")
            frequency_by_trip[trip_code].append((start, end, headway, exact == "1"))
        warning = _ratio_check(
            "frequencies.trip_id", frequency_orphans, len(frequencies), threshold
        )
        if warning:
            warnings.append(warning)
        frequency_mask = np.zeros(len(trip_index), dtype=np.bool_)
        if frequency_by_trip:
            frequency_mask[list(frequency_by_trip)] = True
        rows_for_frequency = base_events & frequency_mask[np.maximum(trip_codes, 0)]
        rows_for_regular = base_events & ~frequency_mask[np.maximum(trip_codes, 0)]

        states = [
            [
                {"runs": set(), "trips": set(), "routes": set(), "raw": 0, "estimated": False}
                for _ in windows
            ]
            for _ in station_keys
        ]

        def add_departure(
            station_code: int,
            trip_code: int,
            day: date,
            run_start: int,
            window_index: int,
            estimated: bool,
        ) -> None:
            state = states[station_code][window_index]
            state["raw"] += 1
            state["runs"].add((day, trip_code, run_start))
            state["trips"].add(trip_code)
            state["routes"].add(int(trip_route_codes_array[trip_code]))
            state["estimated"] |= estimated

        for day_index, day in enumerate(days):
            active_trip_mask = trip_service_active[day_index]
            active_rows = rows_for_regular & active_trip_mask[np.maximum(trip_codes, 0)]
            event_indices = np.flatnonzero(active_rows)
            if len(event_indices):
                local_times = departures[event_indices] - (86400 if day_index == 0 else 0)
                local_stations = station_by_stop[stop_codes[event_indices]]
                local_trips = trip_codes[event_indices]
                for window_index, (_, _, start, end) in enumerate(windows):
                    selected = (local_times >= start) & (local_times < end)
                    for station_code, trip_code in zip(
                        local_stations[selected].tolist(), local_trips[selected].tolist()
                    ):
                        add_departure(station_code, trip_code, day, 0, window_index, False)

            for trip_code, frequency_rows in frequency_by_trip.items():
                if not active_trip_mask[trip_code]:
                    continue
                template_indices = np.flatnonzero(rows_for_frequency & (trip_codes == trip_code))
                if not len(template_indices):
                    continue
                template_departures = departures[template_indices]
                template_offsets = template_departures - first_departure[trip_code]
                template_stations = station_by_stop[stop_codes[template_indices]]
                for start_time, end_time, headway, exact in frequency_rows:
                    for run_start in range(start_time, end_time, headway):
                        run_times = run_start + template_offsets - (86400 if day_index == 0 else 0)
                        for window_index, (_, _, start, end) in enumerate(windows):
                            selected = (run_times >= start) & (run_times < end)
                            for station_code in template_stations[selected].tolist():
                                add_departure(
                                    station_code,
                                    trip_code,
                                    day,
                                    run_start,
                                    window_index,
                                    not exact,
                                )

        records = list(stop_records)
        for station_code, station in enumerate(station_keys):
            for window_index, (start, end, _, _) in enumerate(windows):
                state = states[station_code][window_index]
                departures_count = len(state["runs"])
                route_codes = state["routes"]
                route_type_values = tuple(
                    sorted({route_types[route_code] for route_code in route_codes})
                )
                flags = []
                collapsed = state["raw"] - departures_count
                if collapsed > 0:
                    flags.append(f"collapsed_duplicate_stop_events={collapsed}")
                if state["estimated"]:
                    flags.append("frequency_headway_estimate")
                records.append(
                    StopService(
                        source_id=config.dataset_id,
                        snapshot_id=raw.snapshot_id,
                        source_record_id=station,
                        record_id=f"{station}|{service_date.isoformat()}|{start}-{end}",
                        stop_id=station,
                        service_date=service_date.isoformat(),
                        window_start=start,
                        window_end=end,
                        timezone=str(zone),
                        departures=departures_count,
                        trip_count=len(state["trips"]),
                        route_count=len(route_codes),
                        route_types=route_type_values,
                        area_code=None,
                        quality_flags=tuple(flags),
                    )
                )
        for window_index, (start, end, _, _) in enumerate(windows):
            active_stations = sum(bool(state[window_index]["runs"]) for state in states)
            total_departures = sum(len(state[window_index]["runs"]) for state in states)
            warnings.append(
                f"{service_date.isoformat()} {start}-{end}: "
                f"{active_stations} stations, {total_departures} departures"
            )
        return NormalizedBatch(
            records=tuple(records),
            input_rows=emitted_type_rows + invalid_location_type_rows + stop_time_rows,
            rejected_rows=len(rejected),
            rejections=tuple(rejected),
            warnings=tuple(warnings),
        )

    def validate(self, batch: NormalizedBatch) -> QualityReport:
        errors = list(batch.rejections)
        if not batch.records:
            errors.append("GTFS snapshot is empty")
        seen = set()
        totals: dict[tuple[str, str], int] = defaultdict(int)
        for record in batch.records:
            if not isinstance(record, (TransitStop, StopService)):
                errors.append("GTFS snapshot contains an unsupported record type")
                continue
            if record.record_id in seen:
                errors.append(f"Duplicate GTFS record_id: {record.record_id}")
            seen.add(record.record_id)
            if isinstance(record, StopService):
                totals[(record.window_start, record.window_end)] += record.departures
        if not totals:
            errors.append("GTFS snapshot contains no station service windows")
        for window, total in totals.items():
            if total == 0:
                errors.append(f"GTFS window {window[0]}-{window[1]} has zero departures")
        return QualityReport(
            valid=not errors,
            input_rows=batch.input_rows,
            output_rows=len(batch.records),
            rejected_rows=batch.rejected_rows,
            warnings=batch.warnings,
            errors=tuple(errors),
        )
