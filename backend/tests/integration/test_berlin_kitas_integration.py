"""Kita WFS ingestion against a pinned LOR snapshot and the childcare read endpoints."""

import json
from dataclasses import replace
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.application.childcare import childcare_areas
from app.application.service import DataService
from app.connectors.berlin_wfs import BerlinWFSConnector
from app.connectors.lor import LORConnector
from app.connectors.population import PopulationConnector
from app.domain import Facility, record_from_dict
from app.infrastructure.http import HTTPFetcher

BACKEND = Path(__file__).parents[2]
FIXTURES = BACKEND / "tests/fixtures/berlin_kitas"


def setup_service(tmp_path, fail=False):
    configs = tmp_path / "config"
    configs.mkdir(exist_ok=True)
    lor = json.loads((BACKEND / "config/datasets/berlin_lor.json").read_text())
    lor.update(
        resource_url="https://example.org/synthetic-lor",
        allowed_hosts=["example.org"],
        source_crs="EPSG:4326",
    )
    lor["parser_options"].update(expected_rows=2, name_field="PLR_NAME")
    population = json.loads((BACKEND / "config/datasets/berlin_population.json").read_text())
    population.update(resource_url="https://example.org/population", allowed_hosts=["example.org"])
    population["parser_options"] = {
        "format": "csv",
        "encoding": "utf-8",
        "delimiter": ";",
        "area_code_columns": ["LOR"],
        "area_code_widths": [8],
        "hierarchy": "planungsraum",
        "expected_area_count": 2,
        "sentinels": {"•": "suppressed"},
        "metrics": [
            {"column": "Total", "metric_code": "population_total", "unit": "persons"},
            {
                "column": "Under6",
                "metric_code": "population_age_band",
                "unit": "persons",
                "age_min": 0,
                "age_max_exclusive": 6,
            },
        ],
    }
    kitas = json.loads((BACKEND / "config/datasets/berlin_kitas.json").read_text())
    kitas["parser_options"].pop("expected_count_range")
    kitas["parser_options"].update(page_size=2, expected_count=3)
    for name, values in (
        ("berlin_lor", lor),
        ("berlin_population", population),
        ("berlin_kitas", kitas),
    ):
        (configs / f"{name}.json").write_text(json.dumps(values))

    async def handler(request):
        if fail:
            return httpx.Response(503, request=request)
        if request.url.host == "datenregister.berlin.de":
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=(FIXTURES / "edition-metadata.json").read_bytes(),
                request=request,
            )
        start = int(request.url.params["STARTINDEX"])
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=(FIXTURES / f"page-{start // 2:04d}.json").read_bytes(),
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = DataService(
        tmp_path / "data",
        configs,
        connectors={
            "lor": LORConnector(),
            "population": PopulationConnector(),
            "berlin_wfs": BerlinWFSConnector(HTTPFetcher(client)),
        },
    )
    return service, client


async def ingest_all(tmp_path):
    service, client = setup_service(tmp_path)
    lor_file = tmp_path / "areas.geojson"
    lor_file.write_bytes((BACKEND / "tests/fixtures/lor/areas.geojson").read_bytes())
    population_file = tmp_path / "population.csv"
    population_file.write_text("LOR;Total;Under6\n01011101;120;14\n01011102;•;•\n")
    async with client:
        lor = await service.ingest("berlin_lor", local_file=lor_file)
        population = await service.ingest(
            "berlin_population", local_file=population_file, parent_snapshot_id=lor.snapshot_id
        )
        kitas = await service.ingest("berlin_kitas", parent_snapshot_id=lor.snapshot_id)
    return service, lor, population, kitas


async def test_ingests_kitas_with_provenance_and_area_counts(tmp_path):
    service, lor, _, kitas = await ingest_all(tmp_path)
    assert kitas.parent_snapshot_ids == (lor.snapshot_id,)
    assert len(kitas.raw_assets) == 3
    assert kitas.raw_assets[-1].path.endswith("edition-metadata.json")
    assert kitas.source_edition == "2025-12-31"
    assert kitas.license_id == "dl-de-zero-2.0"
    assert kitas.quality_report.valid and kitas.quality_report.rejected_rows == 0
    assert kitas.coverage["record_count"] == 3
    assert kitas.coverage["area_join"]["areas_with_facilities"] == 2
    page = service.records("berlin_kitas", kitas.snapshot_id, limit=10)
    records = [record_from_dict(item) for item in page["records"]]
    assert all(isinstance(record, Facility) for record in records)
    assert {record.provider_facility_id for record in records} == {
        "01000001",
        "01000002",
        "01000003",
    }


async def test_childcare_endpoints_keep_sources_and_dates_separate(tmp_path):
    service, lor, population, kitas = await ingest_all(tmp_path)
    with TestClient(create_app(service=service)) as client:
        points = client.get(
            "/api/v1/childcare/facilities", params={"snapshot_id": kitas.snapshot_id}
        )
        areas = client.get(
            "/api/v1/childcare/areas",
            params={
                "snapshot_id": kitas.snapshot_id,
                "population_snapshot_id": population.snapshot_id,
            },
        )
        without_population = client.get(
            "/api/v1/childcare/areas", params={"snapshot_id": kitas.snapshot_id}
        )
        missing = client.get("/api/v1/childcare/areas", params={"snapshot_id": "unknown"})
    assert points.status_code == 200
    collection = points.json()
    assert collection["type"] == "FeatureCollection"
    assert collection["sources"]["facilities"]["source_edition"] == "2025-12-31"
    assert collection["sources"]["boundaries"]["snapshot_id"] == lor.snapshot_id
    feature = collection["features"][0]["properties"]
    assert feature["provider_facility_id"] == "01000001"
    assert feature["area_code"] == "01011101"
    assert feature["vacancies"] is None
    assert any("not proof of a childcare shortage" in text for text in collection["limitations"])

    assert areas.status_code == 200
    body = areas.json()
    assert body["sources"]["facilities"]["reference_period"] == "2025-12-31"
    assert body["sources"]["children_under_6"]["snapshot_id"] == population.snapshot_id
    rows = {row["area_code"]: row for row in body["areas"]}
    assert rows["01011101"]["facility_count"] == 2
    assert rows["01011101"]["facilities_with_capacity"] == 1
    assert rows["01011101"]["facilities_without_capacity"] == 1
    assert rows["01011101"]["permitted_places_known_sum"] == 45
    assert rows["01011101"]["children_under_6"]["value"] == 14
    assert rows["01011102"]["facility_count"] == 1
    assert rows["01011102"]["children_under_6"]["value"] is None
    assert rows["01011102"]["children_under_6"]["value_status"] == "suppressed"
    assert body["unassigned_facility_count"] == 0
    assert "available" not in json.dumps(rows)

    assert without_population.json()["sources"]["children_under_6"] is None
    assert all(row["children_under_6"] is None for row in without_population.json()["areas"])
    assert missing.status_code == 404


async def test_childcare_areas_reject_population_on_another_lor_snapshot(tmp_path):
    service, lor, population, kitas = await ingest_all(tmp_path)
    other_lor = tmp_path / "other.geojson"
    source = json.loads((BACKEND / "tests/fixtures/lor/areas.geojson").read_text())
    source["features"][0]["properties"]["PLR_NAME"] = "Revised Alpha"
    other_lor.write_text(json.dumps(source))
    other = await service.ingest("berlin_lor", local_file=other_lor)
    population_file = tmp_path / "population.csv"
    other_population = await service.ingest(
        "berlin_population", local_file=population_file, parent_snapshot_id=other.snapshot_id
    )
    assert other.snapshot_id != lor.snapshot_id
    with TestClient(create_app(service=service)) as client:
        response = client.get(
            "/api/v1/childcare/areas",
            params={
                "snapshot_id": kitas.snapshot_id,
                "population_snapshot_id": other_population.snapshot_id,
            },
        )
    assert response.status_code == 409


async def test_childcare_view_uses_join_pinned_in_manifest_not_current_config(tmp_path):
    service, _, _, kitas = await ingest_all(tmp_path)
    config = service.configs["berlin_kitas"]
    service.configs["berlin_kitas"] = replace(
        config,
        parser_options={**config.parser_options, "parent_dataset": "x", "hierarchy": "bezirk"},
    )
    body = childcare_areas(service, kitas.snapshot_id)
    assert body["hierarchy"] == "planungsraum"
    assert body["facility_count"] == 3 and body["unassigned_facility_count"] == 0


async def test_failed_refresh_keeps_last_valid_snapshot(tmp_path):
    service, lor, _, kitas = await ingest_all(tmp_path)
    failing, client = setup_service(tmp_path, fail=True)
    async with client:
        try:
            await failing.ingest("berlin_kitas", parent_snapshot_id=lor.snapshot_id)
        except Exception:
            pass
        else:
            raise AssertionError("refresh should fail")
    page = failing.records("berlin_kitas", kitas.snapshot_id, limit=1)
    assert page["records"][0]["provider_facility_id"] == "01000001"
