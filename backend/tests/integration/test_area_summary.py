"""Offline area-summary scenarios over synthetic published snapshots.

The JSON files in tests/fixtures/area_summary are the frontend fixtures; they are
regenerated with UPDATE_AREA_SUMMARY_FIXTURES=1 and otherwise must match exactly.
"""

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.application.service import DataService
from app.domain import (
    Area,
    AreaObservation,
    Business,
    Place,
    QualityReport,
    RawAsset,
    SnapshotManifest,
    StopService,
    TransitStop,
)
from app.infrastructure.storage import write_records

BACKEND = Path(__file__).parents[2]
FIXTURES = BACKEND / "tests/fixtures/area_summary"
DATASETS = (
    "berlin_lor",
    "berlin_lor_names",
    "berlin_population",
    "osm_pois",
    "ihk_businesses",
    "vbb_gtfs",
)
URL = "/api/v1/areas/{}/summary"
AREAS = {"01011101": 13.35, "01011102": 13.36, "01011103": 13.37}


def square(west):
    ring = [[west, 52.5], [west + 0.01, 52.5], [west + 0.01, 52.51], [west, 52.51], [west, 52.5]]
    return {"type": "Polygon", "coordinates": [ring]}


def point(lon, lat=52.505):
    return {"type": "Point", "coordinates": [lon, lat]}


def publish(service, dataset_id, suffix, records_for, parents=(), **extra):
    snapshot_id = f"{dataset_id}-synthetic-{suffix}"
    config = service.configs[dataset_id]
    records = tuple(records_for(dataset_id, snapshot_id))
    relative = f"normalized/{dataset_id}/{snapshot_id}/records.parquet"
    write_records(records, service.root / relative)
    manifest = SnapshotManifest(
        snapshot_id=snapshot_id,
        dataset_id=dataset_id,
        adapter_version="synthetic",
        landing_url=config.landing_url,
        resource_url=f"https://example.org/synthetic/{dataset_id}",
        retrieved_at=datetime(2026, 10, 18, 12, tzinfo=timezone.utc),
        raw_assets=(
            RawAsset(
                path=f"raw/{dataset_id}/{snapshot_id}/0-source",
                sha256="0" * 64,
                size_bytes=1,
                resource_url=f"https://example.org/synthetic/{dataset_id}",
            ),
        ),
        request_fingerprint=snapshot_id,
        parser_config_hash="1" * 64,
        quality_report=QualityReport(
            valid=True,
            input_rows=len(records),
            output_rows=len(records),
            warnings=("SYNTHETIC FIXTURE: not real data.",),
        ),
        license_id=config.license_id,
        license_url=config.license_url,
        attribution=config.attribution,
        source_edition=config.source_edition,
        reference_period=config.reference_period,
        boundary_edition=config.boundary_edition,
        normalized_assets=(relative,),
        parent_snapshot_ids=tuple(parents),
        **extra,
    )
    job_id = f"job-{snapshot_id}"
    service.catalog.begin_job(job_id, dataset_id)
    service.catalog.publish(manifest, job_id)
    return snapshot_id


def common(source_id, snapshot_id, record_id):
    return {
        "source_id": source_id,
        "snapshot_id": snapshot_id,
        "source_record_id": record_id,
        "record_id": record_id,
    }


def lor_records(source_id, snapshot_id):
    for code, west in AREAS.items():
        yield Area(
            **common(source_id, snapshot_id, f"planungsraum:{code}"),
            geometry=square(west),
            area_code=code,
            name=code,
            hierarchy="planungsraum",
            boundary_edition="2021",
        )


def name_records(source_id, snapshot_id):
    for code, name in (("01011101", "Synthetic Alpha"), ("01011102", "Synthetic Beta")):
        yield Area(
            **common(source_id, snapshot_id, f"planungsraum:{code}"),
            geometry=square(AREAS[code]),
            area_code=code,
            name=name,
            hierarchy="planungsraum",
            boundary_edition="2021",
            attributes={"bez": "01 - Synthetic District", "bzr_name": "Synthetic Region"},
        )


def population_records(source_id, snapshot_id):
    values = {
        "01011101": [(None, None, 100), (0, 18, 20), (18, 65, 60), (65, None, 20)],
        "01011102": [(None, None, None), (0, 18, None), (18, 65, 40), (65, None, None)],
        "01011103": [(None, None, 0), (0, 18, 0), (18, 65, 0), (65, None, 0)],
    }
    for code, rows in values.items():
        for low, high, value in rows:
            metric = "population_total" if low is None else "population_age_band"
            yield AreaObservation(
                **common(source_id, snapshot_id, f"{code}:2026-06-30:{metric}:{low}:{high}"),
                area_code=code,
                hierarchy="planungsraum",
                boundary_edition="2021",
                metric_code=metric,
                value=value,
                unit="persons",
                reference_period="2026-06-30",
                value_status="observed" if value is not None else "suppressed",
                age_min=low,
                age_max_exclusive=high,
            )


def place_records(source_id, snapshot_id, mapping_version="osm_v1"):
    rows = [
        ("node/1", 13.352, "coffee_shop", "Synthetic Café One"),
        ("node/2", 13.355, "coffee_shop", None),
        ("node/3", 13.357, "bakery", "Synthetic Bakery"),
        ("way/4", 13.365, "coffee_shop", "Synthetic Café Beta"),
    ]
    for record_id, lon, category, name in rows:
        yield Place(
            **common(source_id, snapshot_id, record_id),
            geometry=point(lon),
            source_geometry_type=record_id.split("/")[0],
            original_category="amenity=cafe" if category == "coffee_shop" else "shop=bakery",
            category_mapping_version=mapping_version,
            canonical_category=category,
            name=name,
        )


def business_records(source_id, snapshot_id):
    rows = [
        ("1001", "01011101", "coffee_shop", "1 - 3 Beschäftigte", 1, 3),
        ("1002", "01011101", "restaurant", "unbekannt", None, None),
        ("1003", "01011101", "coffee_shop", "0 Beschäftigte", 0, 0),
    ]
    for record_id, code, category, label, low, high in rows:
        yield Business(
            **common(source_id, snapshot_id, record_id),
            geometry=point(13.353),
            industry_code_system="ihk_berlin_branch",
            category_mapping_version="ihk_v1",
            industry_code="56114" if category == "coffee_shop" else "56111",
            canonical_category=category,
            employees_label=label,
            employees_min=low,
            employees_max=high,
            area_code=code,
            hierarchy="planungsraum",
            boundary_edition="2021",
        )


def transit_records(source_id, snapshot_id):
    stops = [
        ("S1", 13.354, 1, None, "Synthetic Station Alpha"),
        ("P1", 13.3541, 0, "S1", "Synthetic Station Alpha"),
        ("S2", 13.366, 0, None, "Synthetic Stop Beta"),
    ]
    for stop_id, lon, kind, parent, name in stops:
        yield TransitStop(
            **common(source_id, snapshot_id, stop_id),
            geometry=point(lon),
            stop_id=stop_id,
            name=name,
            location_type=kind,
            parent_station=parent,
        )
    for station, counts in (("S1", (10, 8)), ("S2", (0, 5))):
        for (start, end), departures in zip(
            (("07:00:00", "10:00:00"), ("16:00:00", "19:00:00")), counts
        ):
            yield StopService(
                **common(source_id, snapshot_id, f"{station}|2026-11-10|{start}-{end}"),
                stop_id=station,
                service_date="2026-11-10",
                window_start=start,
                window_end=end,
                timezone="Europe/Berlin",
                departures=departures,
                trip_count=departures,
                route_count=1 if departures else 0,
                route_types=(3,) if departures else (),
                quality_flags=("frequency_headway_estimate",)
                if station == "S2" and departures
                else (),
            )


BUILDERS = {
    "berlin_lor_names": name_records,
    "berlin_population": population_records,
    "osm_pois": place_records,
    "ihk_businesses": business_records,
    "vbb_gtfs": transit_records,
}
NEEDS_PARENT = {"berlin_lor_names", "berlin_population", "ihk_businesses"}


def make_service(tmp_path, datasets=DATASETS):
    configs = tmp_path / "config"
    configs.mkdir(parents=True)
    for name in DATASETS:
        shutil.copy(BACKEND / f"config/datasets/{name}.json", configs / f"{name}.json")
    service = DataService(tmp_path / "data", configs)
    ids = {}
    if "berlin_lor" in datasets:
        ids["berlin_lor"] = publish(service, "berlin_lor", "1", lor_records)
    for name in datasets:
        if name != "berlin_lor":
            parents = (ids["berlin_lor"],) if name in NEEDS_PARENT else ()
            ids[name] = publish(service, name, "1", BUILDERS[name], parents)
    return service, ids


def get(service, area_code, **params):
    with TestClient(create_app(service=service)) as client:
        return client.get(URL.format(area_code), params=params)


def check_fixture(name, body):
    if isinstance(body.get("error"), dict):
        body = {**body, "error": {**body["error"], "request_id": "<request-id>"}}
    path = FIXTURES / f"{name}.json"
    text = json.dumps(body, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    if os.environ.get("UPDATE_AREA_SUMMARY_FIXTURES") == "1":
        path.write_text(text)
    assert json.loads(path.read_text()) == json.loads(text), f"{path.name} is out of date"


def by_dataset(items):
    return {item["dataset_id"]: item for item in items}


def test_complete_summary_is_traceable_and_stable(tmp_path):
    service, ids = make_service(tmp_path)
    response = get(service, "01011101")
    assert response.status_code == 200, response.text
    body = response.json()
    check_fixture("complete", body)

    assert body["schema_version"] == "1.0" and body["calculation_version"] == "area_summary_v1"
    assert body["area"]["name"]["value"] == "Synthetic Alpha"
    assert body["area"]["name"]["sources"] == [
        {"dataset_id": "berlin_lor_names", "snapshot_id": ids["berlin_lor_names"]}
    ]
    assert body["area"]["area_km2"]["calculation"]["id"] == "planar_area"
    assert body["area"]["boundary"]["type"] == "Polygon"
    residents = body["residents"]
    assert residents["status"] == "available"
    assert residents["total"]["value"] == 100
    assert residents["reference_date"] == "2026-06-30"
    assert [band["share"]["value"] for band in residents["age_bands"]] == [0.2, 0.6, 0.2]
    assert residents["age_bands"][0]["share"]["calculation"]["version"] == "area_summary_v1"

    competitors = by_dataset(body["competitors"]["sources"])
    assert body["competitors"]["combination"] == "not_combined"
    assert competitors["osm_pois"]["count"]["value"] == 2
    assert competitors["osm_pois"]["record_ids"] == ["node/1", "node/2"]
    assert competitors["ihk_businesses"]["count"]["value"] == 2
    assert competitors["ihk_businesses"]["record_ids"] == ["1001", "1003"]
    assert competitors["ihk_businesses"]["category_mapping_version"] == "ihk_v1"

    workplaces = body["workplaces"]
    assert workplaces["registered_businesses"]["value"] == 3
    assert workplaces["measured_daytime_population"]["value"] is None
    assert workplaces["measured_daytime_population"]["status"] == "not_available"
    assert [c["label"] for c in workplaces["employee_size_classes"]] == [
        "0 Beschäftigte",
        "1 - 3 Beschäftigte",
        "unbekannt",
    ]

    transit = body["transit"]
    assert [w["scheduled_departures"]["value"] for w in transit["windows"]] == [10, 8]
    assert transit["stops_in_area"]["0"]["value"] == 1
    assert transit["stops_in_area"]["1"]["value"] == 1
    assert transit["nearest_station"]["distance_type"] == "straight_line"
    assert transit["nearest_station"]["stop_id"] == "S1"
    assert transit["routed_distance"] == {
        "value": None,
        "status": "not_available",
        "unit": "m",
        "sources": [],
        "reference_period": None,
        "calculation": None,
        "note": "No routing source is integrated; straight-line distance is not walking time.",
    }
    snapshots = by_dataset(body["snapshots"])
    assert {item["status"] for item in snapshots.values()} == {"used"}
    assert snapshots["vbb_gtfs"]["license_id"] == "CC-BY-4.0"
    assert snapshots["berlin_population"]["parent_snapshot_ids"] == [ids["berlin_lor"]]
    assert "households" in body["not_integrated"]
    assert {"footfall", "rent", "revenue", "vacancy", "confidence", "ranking"} <= set(
        body["not_provided"]
    )
    assert body["warnings"] == []

    pinned = get(
        service,
        "01011101",
        lor_snapshot_id=ids["berlin_lor"],
        names_snapshot_id=ids["berlin_lor_names"],
        population_snapshot_id=ids["berlin_population"],
        osm_snapshot_id=ids["osm_pois"],
        ihk_snapshot_id=ids["ihk_businesses"],
        gtfs_snapshot_id=ids["vbb_gtfs"],
    ).json()
    assert get(service, "01011101").json() == body
    for item in pinned["snapshots"]:
        assert item.pop("pinned_by") == "request"
    for item in body["snapshots"]:
        assert item.pop("pinned_by") == "active_snapshot"
    assert pinned == body


def test_optional_datasets_missing_still_returns_area(tmp_path):
    service, _ = make_service(tmp_path, ("berlin_lor", "berlin_population"))
    response = get(service, "01011101", include_geometry="false")
    assert response.status_code == 200, response.text
    body = response.json()
    check_fixture("optional_missing", body)
    assert body["area"]["boundary"] is None
    assert body["area"]["name"]["value"] is None
    assert body["area"]["name"]["status"] == "missing"
    assert body["residents"]["total"]["value"] == 100
    assert {item["status"] for item in body["competitors"]["sources"]} == {"missing"}
    assert body["workplaces"]["status"] == "missing"
    assert body["workplaces"]["registered_businesses"] is None
    assert body["transit"]["status"] == "missing"
    assert body["transit"]["windows"] == []
    assert len(body["warnings"]) == 4


def test_suppressed_population_stays_null(tmp_path):
    service, _ = make_service(tmp_path)
    body = get(service, "01011102", include_geometry="false").json()
    check_fixture("suppressed", body)
    residents = body["residents"]
    assert residents["status"] == "partial"
    assert residents["total"] == {
        "value": None,
        "status": "suppressed",
        "unit": "persons",
        "sources": residents["total"]["sources"],
        "reference_period": "2026-06-30",
        "calculation": None,
    }
    assert [band["share"]["status"] for band in residents["age_bands"]] == ["suppressed"] * 3
    assert residents["age_bands"][1]["count"]["value"] == 40
    assert residents["density"]["value"] is None
    transit = body["transit"]
    assert [w["stations_with_departures"]["value"] for w in transit["windows"]] == [0, 1]
    assert [w["scheduled_departures"]["value"] for w in transit["windows"]] == [0, 5]
    assert [w["includes_frequency_estimates"] for w in transit["windows"]] == [False, True]
    assert "frequencies.txt" in transit["windows"][1]["scheduled_departures"]["note"]
    assert transit["stations"][0]["departures"][1]["quality_flags"] == [
        "frequency_headway_estimate"
    ]


def test_zero_population_and_zero_competitors(tmp_path):
    service, _ = make_service(tmp_path)
    body = get(service, "01011103", include_geometry="false").json()
    check_fixture("zero", body)
    residents = body["residents"]
    assert residents["total"]["value"] == 0 and residents["total"]["status"] == "observed"
    assert {band["share"]["status"] for band in residents["age_bands"]} == {"not_applicable"}
    assert residents["density"]["value"] == 0
    competitors = by_dataset(body["competitors"]["sources"])
    assert competitors["osm_pois"]["count"]["value"] == 0
    assert "not none in reality" in competitors["osm_pois"]["count"]["note"]
    assert competitors["ihk_businesses"]["count"]["value"] == 0
    assert body["area"]["name"]["status"] == "missing"
    assert body["transit"]["stops_in_area"] == {}
    assert body["transit"]["nearest_station"]["stop_id"] == "S2"


def test_missing_required_boundary_and_unknown_area(tmp_path):
    service, _ = make_service(tmp_path, ())
    response = get(service, "01011101")
    assert response.status_code == 503
    check_fixture("missing_required", response.json())
    assert response.json()["error"]["code"] == "DATA_UNAVAILABLE"

    service, _ = make_service(tmp_path / "other")
    response = get(service, "09999999")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "AREA_NOT_FOUND"
    assert get(service, "0101110").status_code == 422
    unknown_snapshot = get(service, "01011101", gtfs_snapshot_id="nope")
    assert unknown_snapshot.status_code == 404
    assert unknown_snapshot.json()["error"]["code"] == "SNAPSHOT_NOT_FOUND"


def test_incompatible_snapshots(tmp_path):
    service, ids = make_service(tmp_path)
    new_lor = publish(service, "berlin_lor", "2", lor_records)

    default = get(service, "01011101", include_geometry="false")
    assert default.status_code == 200
    body = default.json()
    check_fixture("incompatible_default", body)
    snapshots = by_dataset(body["snapshots"])
    assert snapshots["berlin_lor"]["snapshot_id"] == new_lor
    for dataset_id in ("berlin_lor_names", "berlin_population", "ihk_businesses"):
        assert snapshots[dataset_id]["status"] == "incompatible"
    assert body["residents"]["status"] == "incompatible"
    assert body["residents"]["total"] is None
    assert body["area"]["name"]["value"] is None
    assert by_dataset(body["competitors"]["sources"])["osm_pois"]["count"]["value"] == 2

    explicit = get(service, "01011101", population_snapshot_id=ids["berlin_population"])
    assert explicit.status_code == 409
    check_fixture("incompatible_snapshot", explicit.json())
    assert explicit.json()["error"]["code"] == "SNAPSHOT_INCOMPATIBLE"

    old = get(
        service,
        "01011101",
        lor_snapshot_id=ids["berlin_lor"],
        population_snapshot_id=ids["berlin_population"],
        names_snapshot_id=ids["berlin_lor_names"],
        ihk_snapshot_id=ids["ihk_businesses"],
    )
    assert old.status_code == 200
    assert old.json()["residents"]["total"]["value"] == 100


def test_category_and_parameter_validation(tmp_path):
    service, _ = make_service(tmp_path)
    coworking = get(service, "01011101", category="coworking").json()
    sources = by_dataset(coworking["competitors"]["sources"])
    assert sources["osm_pois"]["count"]["value"] == 0
    assert sources["ihk_businesses"]["status"] == "not_applicable"
    assert get(service, "01011101", category="space_bar").status_code == 422
    unsupported = get(service, "01011101", radius_m="500")
    assert unsupported.status_code == 422
    assert unsupported.json()["error"]["code"] == "UNSUPPORTED_FILTER"


def test_unavailable_snapshot_asset(tmp_path):
    service, ids = make_service(tmp_path)
    (service.root / f"normalized/vbb_gtfs/{ids['vbb_gtfs']}/records.parquet").unlink()
    body = get(service, "01011101").json()
    assert body["transit"]["status"] == "unavailable"
    assert by_dataset(body["snapshots"])["vbb_gtfs"]["status"] == "unavailable"
    assert get(service, "01011101", gtfs_snapshot_id=ids["vbb_gtfs"]).status_code == 503


@pytest.mark.parametrize("name", ["complete", "suppressed", "zero", "optional_missing"])
def test_frontend_fixtures_are_marked_synthetic(name):
    body = json.loads((FIXTURES / f"{name}.json").read_text())
    assert all(
        "SYNTHETIC FIXTURE: not real data." in item.get("quality_warnings", [])
        for item in body["snapshots"]
        if item["status"] == "used"
    )


def test_pinned_snapshot_keeps_its_own_category_mapping(tmp_path):
    service, _ = make_service(tmp_path)
    old = publish(
        service,
        "osm_pois",
        "legacy",
        lambda source_id, snapshot_id: place_records(source_id, snapshot_id, "osm_v0"),
    )
    body = get(service, "01011101", osm_snapshot_id=old).json()
    osm = by_dataset(body["competitors"]["sources"])["osm_pois"]
    assert osm["status"] == "used"
    assert osm["category_mapping_version"] == "osm_v0"
    assert osm["count"]["value"] == 2


def test_concurrent_cold_requests_build_one_index(tmp_path):
    import threading
    import time

    from app.application.area_summary import AreaSummaryService

    service, ids = make_service(tmp_path)
    summaries = AreaSummaryService(service)
    manifest = service.catalog.get("osm_pois", ids["osm_pois"])
    builds = []

    def builder(path):
        builds.append(path)
        time.sleep(0.05)
        return object()

    results = []
    threads = [
        threading.Thread(target=lambda: results.append(summaries._cached(manifest, builder)))
        for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(builds) == 1
    assert len({id(value) for value in results}) == 1


def test_failed_index_build_keeps_single_flight(tmp_path):
    import threading
    import time

    from app.application.area_summary import AreaSummaryService

    service, ids = make_service(tmp_path)
    summaries = AreaSummaryService(service)
    manifest = service.catalog.get("osm_pois", ids["osm_pois"])
    active, overlaps, calls = [0], [], []
    guard = threading.Lock()

    def builder(path):
        with guard:
            calls.append(path)
            active[0] += 1
            overlaps.append(active[0])
            first = len(calls) == 1
        time.sleep(0.05)
        with guard:
            active[0] -= 1
        if first:
            raise RuntimeError("first build fails")
        return object()

    def call():
        try:
            summaries._cached(manifest, builder)
        except RuntimeError:
            pass

    first = threading.Thread(target=call)
    first.start()
    time.sleep(0.01)
    waiter = threading.Thread(target=call)
    waiter.start()
    first.join()
    late = threading.Thread(target=call)
    late.start()
    waiter.join()
    late.join()
    assert max(overlaps) == 1
    assert len(calls) == 2
