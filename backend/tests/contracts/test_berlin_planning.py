"""Offline normalization, spatial query, storage, and API tests for Berlin plans."""

import hashlib
import json
import math
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from pyproj import Transformer
from shapely.geometry import Point, Polygon, mapping, shape
from shapely.ops import transform

from app.api.main import create_app
from app.application.service import DataService
from app.connectors.berlin_wfs import BerlinWFSConnector
from app.connectors.lor import LORConnector
from app.domain import (
    Area,
    DatasetConfig,
    DevelopmentPlan,
    FetchRequest,
    RawAsset,
    RawBundle,
    SchemaError,
    record_from_dict,
    record_to_dict,
)
from app.geo.development_plans import (
    EXCLUDED_CLAIMS,
    PLAN_INTERPRETATION_GUARD,
    DevelopmentPlanIndex,
)
from app.infrastructure.http import HTTPFetcher
from app.infrastructure.storage import read_records, write_records

BACKEND = Path(__file__).parents[2]
CONFIG_FILE = BACKEND / "config/datasets/berlin_planning.json"
FIXTURES = BACKEND / "tests/fixtures/berlin_planning"
LAYER_NAMES = ("a_bp_iv", "b_bp_fs", "c_bp_ak")
RETRIEVED_AT = datetime(2026, 10, 18, tzinfo=timezone.utc)


def configuration(**overrides):
    values = json.loads(CONFIG_FILE.read_text())
    values["allowed_hosts"] = tuple(values["allowed_hosts"])
    options = values["parser_options"]
    options.update(
        page_size=2,
        max_pages=10,
        max_total_bytes=2_000_000,
        layers=[
            {
                **layer,
                "expected_count_range": [
                    len(
                        json.loads(
                            (FIXTURES / f"{layer['typename'].split(':')[-1]}.json").read_text()
                        )["features"]
                    ),
                ]
                * 2,
            }
            for layer in options["layers"]
        ],
    )
    options.update(overrides.pop("parser_options", {}))
    values.update(overrides)
    return DatasetConfig(**values)


def _layer_url(config, typename):
    parts = urlsplit(config.resource_url)
    query = [
        (key, typename if key.lower() == "typenames" else value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def raw_bundle(paths=None, *, config=None):
    config = config or configuration()
    paths = paths or [FIXTURES / f"{name}.json" for name in LAYER_NAMES]
    assets = []
    for path in paths:
        layer_name = Path(path).stem
        typename = f"bplan:{layer_name}"
        data = Path(path).read_bytes()
        assets.append(
            RawAsset(
                path=str(path),
                sha256=hashlib.sha256(data).hexdigest(),
                size_bytes=len(data),
                resource_url=_layer_url(config, typename),
            )
        )
    return RawBundle(
        assets=tuple(assets),
        retrieved_at=RETRIEVED_AT,
        snapshot_id="planning-fixture",
        request_fingerprint="synthetic-plans",
    )


def _write_mutated(tmp_path, layer, mutate):
    data = json.loads((FIXTURES / f"{layer}.json").read_text())
    mutate(data)
    path = tmp_path / f"{layer}.json"
    path.write_text(json.dumps(data, ensure_ascii=False))
    return path


def test_normalizes_three_layers_and_preserves_original_fields_and_geometry():
    connector = BerlinWFSConnector()
    batch = connector.normalize(raw_bundle(), configuration())
    plans = {plan.plan_id: plan for plan in batch.records}
    assert len(plans) == 10
    assert plans["0206101g/1"].source_record_id == "0206101g/1"
    assert plans["0206101g/1"].record_id == "berlin_planning:0206101g/1"
    assert plans["0206101g/1"].raw_status == "In Kraft getreten"
    assert plans["0206101g/1"].status == "adopted"
    source = json.loads((FIXTURES / "b_bp_fs.json").read_text())
    slash_plan = next(
        feature for feature in source["features"] if feature["properties"]["planid"] == "0206101g/1"
    )
    assert plans["0206101g/1"].document_urls == {
        field: slash_plan["properties"][field] for field in ("scan_www", "grund_www")
    }
    assert plans["0100062c"].geometry["type"] == "MultiPolygon"
    assert plans["0206140cab"].land_use_text is None
    assert plans["0206101g/1"].land_use_text == (
        "allgemeines Wohngebiet, Kerngebiet, Straßenverkehrsfläche"
    )
    linked = plans["0400007"]
    assert linked.document_urls["url_www"].startswith("https://")
    assert linked.land_use_text
    assert linked.source_layer == "bplan:b_bp_fs"
    for plan in batch.records:
        assert shape(plan.geometry).geom_type in {"Polygon", "MultiPolygon"}
        assert 13 <= shape(plan.geometry).representative_point().x <= 13.9
        assert 52.3 <= shape(plan.geometry).representative_point().y <= 52.8
    assert connector.validate(batch).valid
    assert any("numberMatched" in warning for warning in batch.warnings)


def test_maps_unknown_statuses_and_flags_layer_mismatches(tmp_path):
    connector = BerlinWFSConnector()
    unknown = _write_mutated(
        tmp_path,
        "a_bp_iv",
        lambda data: data["features"][0]["properties"].update(bp_rechtsstand="Neue Phase"),
    )
    batch = connector.normalize(
        raw_bundle([unknown, FIXTURES / "b_bp_fs.json", FIXTURES / "c_bp_ak.json"]),
        configuration(),
    )
    plan = next(item for item in batch.records if item.plan_id == "0100001")
    assert plan.status == "unmapped"
    assert plan.raw_status == "Neue Phase"
    assert "status_unmapped" in plan.quality_flags
    assert any("Neue Phase" in warning and "=1" in warning for warning in batch.warnings)

    mismatch = _write_mutated(
        tmp_path,
        "b_bp_fs",
        lambda data: data["features"][0]["properties"].update(bp_rechtsstand="Im Verfahren"),
    )
    batch = connector.normalize(
        raw_bundle([FIXTURES / "a_bp_iv.json", mismatch, FIXTURES / "c_bp_ak.json"]),
        configuration(),
    )
    plan = next(item for item in batch.records if item.plan_id == "0206101g/1")
    assert plan.status == "in_progress"
    assert "status_layer_mismatch" in plan.quality_flags
    assert any("status/layer mismatches" in warning for warning in batch.warnings)


def test_rejects_empty_and_non_http_document_urls_and_keeps_date_flags(tmp_path):
    def mutate(data):
        props = data["features"][0]["properties"]
        props.update(url_www="ftp://example.org/file", scan_www="javascript:alert(1)")
        props["grund_www"] = "https://"

    invalid_urls = _write_mutated(tmp_path, "b_bp_fs", mutate)
    batch = BerlinWFSConnector().normalize(
        raw_bundle(
            [
                FIXTURES / "a_bp_iv.json",
                invalid_urls,
                FIXTURES / "c_bp_ak.json",
            ]
        ),
        configuration(),
    )
    slash_plan = next(item for item in batch.records if item.plan_id == "0206101g/1")
    assert slash_plan.document_urls == {}
    assert {
        "document_url_rejected:url_www",
        "document_url_rejected:scan_www",
        "document_url_rejected:grund_www",
        "no_document_links",
    }.issubset(slash_plan.quality_flags)
    assert any(
        flag.startswith("date_implausible:")
        for flag in next(
            item for item in batch.records if item.plan_id == "0400057VE"
        ).quality_flags
    )
    assert (
        next(item for item in batch.records if item.plan_id == "0400057VE").dates["aul_anfang"]
        == "0209-06-03"
    )

    iso_suffix = _write_mutated(
        tmp_path,
        "b_bp_fs",
        lambda data: data["features"][0]["properties"].update(
            aul_anfang="2022-04-05T12:30:00Z",
            afs_beschl="2022-04-06Z",
        ),
    )
    batch = BerlinWFSConnector().normalize(
        raw_bundle([FIXTURES / "a_bp_iv.json", iso_suffix, FIXTURES / "c_bp_ak.json"]),
        configuration(),
    )
    plan = next(item for item in batch.records if item.plan_id == "0206101g/1")
    assert plan.dates["aul_anfang"] == "2022-04-05"
    assert plan.dates["afs_beschl"] == "2022-04-06"

    unparsable = _write_mutated(
        tmp_path,
        "b_bp_fs",
        lambda data: data["features"][0]["properties"].update(aul_anfang="not-a-date"),
    )
    batch = BerlinWFSConnector().normalize(
        raw_bundle([FIXTURES / "a_bp_iv.json", unparsable, FIXTURES / "c_bp_ak.json"]),
        configuration(),
    )
    plan = next(item for item in batch.records if item.plan_id == "0206101g/1")
    assert "aul_anfang" not in plan.dates
    assert "date_unparseable:aul_anfang" in plan.quality_flags


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda data: data["crs"]["properties"].update(name="EPSG:4326"),
            "CRS differs",
        ),
        (
            lambda data: data["features"][0].update(
                geometry={"type": "Point", "coordinates": [390000, 5820000]}
            ),
            "Unsupported WFS geometry",
        ),
        (
            lambda data: data["features"][0].update(
                geometry={
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [390000, 5820000],
                            [390100, 5820100],
                            [390000, 5820100],
                            [390100, 5820000],
                            [390000, 5820000],
                        ]
                    ],
                }
            ),
            "Invalid WFS polygon",
        ),
        (
            lambda data: data["features"][0].update(
                geometry={
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [100000, 100000],
                            [100100, 100000],
                            [100100, 100100],
                            [100000, 100100],
                            [100000, 100000],
                        ]
                    ],
                }
            ),
            "outside source_bbox",
        ),
        (
            lambda data: data["features"][0].update(
                geometry={
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [360000, 5790000],
                            [360100, 5790000],
                            [360100, 5790100],
                            [360000, 5790100],
                            [360000, 5790000],
                        ]
                    ],
                }
            ),
            "outside allowed_bbox",
        ),
        (lambda data: data["features"][0].pop("geometry"), "missing WFS geometry"),
    ],
)
def test_rejects_geometry_and_crs_errors(tmp_path, mutation, message):
    changed = _write_mutated(tmp_path, "a_bp_iv", mutation)
    with pytest.raises(SchemaError, match=message):
        BerlinWFSConnector().normalize(
            raw_bundle([changed, FIXTURES / "b_bp_fs.json", FIXTURES / "c_bp_ak.json"]),
            configuration(),
        )


def test_repairs_valid_source_polygon_that_becomes_invalid_after_reprojection():
    config = configuration()
    layers = [dict(layer) for layer in config.parser_options["layers"]]
    layers[0]["expected_count_range"] = [1, 1]
    config = configuration(parser_options={"layers": layers})
    batch = BerlinWFSConnector().normalize(
        raw_bundle(
            [
                FIXTURES / "reprojection/a_bp_iv.json",
                FIXTURES / "b_bp_fs.json",
                FIXTURES / "c_bp_ak.json",
            ],
            config=config,
        ),
        config,
    )
    plan = next(record for record in batch.records if record.plan_id == "0300082")
    geometry = shape(plan.geometry)
    assert geometry.geom_type == "MultiPolygon"
    assert geometry.is_valid
    assert "geometry_repaired_after_reprojection" in plan.quality_flags
    assert any("WGS84 geometry repair after reprojection: 1 plan(s)" in w for w in batch.warnings)


def test_rejects_missing_required_properties_duplicate_ids_and_layer_counts(tmp_path):
    missing_property = _write_mutated(
        tmp_path,
        "a_bp_iv",
        lambda data: data["features"][0]["properties"].pop("planname"),
    )
    with pytest.raises(SchemaError, match="missing expected properties"):
        BerlinWFSConnector().normalize(
            raw_bundle(
                [
                    missing_property,
                    FIXTURES / "b_bp_fs.json",
                    FIXTURES / "c_bp_ak.json",
                ]
            ),
            configuration(),
        )
    duplicate = _write_mutated(
        tmp_path,
        "b_bp_fs",
        lambda data: data["features"][0]["properties"].update(planid="0100001"),
    )
    with pytest.raises(SchemaError, match="Duplicate WFS plan ID"):
        BerlinWFSConnector().normalize(
            raw_bundle([FIXTURES / "a_bp_iv.json", duplicate, FIXTURES / "c_bp_ak.json"]),
            configuration(),
        )
    with pytest.raises(SchemaError, match="count mismatch"):
        changed_count = _write_mutated(
            tmp_path, "a_bp_iv", lambda data: data.update(numberMatched=4)
        )
        BerlinWFSConnector().normalize(
            raw_bundle([changed_count, FIXTURES / "b_bp_fs.json", FIXTURES / "c_bp_ak.json"]),
            configuration(),
        )
    with pytest.raises(SchemaError, match="missing layer pages"):
        BerlinWFSConnector().normalize(
            raw_bundle([FIXTURES / "a_bp_iv.json", FIXTURES / "b_bp_fs.json"]),
            configuration(),
        )


def _fetch_configuration(**option_changes):
    config = configuration()
    options = dict(config.parser_options)
    options.update(option_changes)
    resource_url = config.resource_url.replace("gdi.berlin.de", "example.org")
    return replace(
        config,
        resource_url=resource_url,
        allowed_hosts=("example.org",),
        parser_options=options,
    )


def _response_for_request(request, *, mutate=None):
    query = request.url.params
    layer = query["TYPENAMES"].split(":")[-1]
    start = int(query["STARTINDEX"])
    data = json.loads((FIXTURES / f"{layer}.json").read_text())
    size = int(query["COUNT"])
    data["features"] = data["features"][start : start + size]
    if start + size < data["numberMatched"]:
        data["links"] = [
            {
                "rel": "next",
                "href": str(request.url.copy_set_param("STARTINDEX", str(start + size))),
            }
        ]
    else:
        data.pop("links", None)
    if mutate is not None:
        data = mutate(layer, start, data)
    return httpx.Response(200, json=data, request=request)


async def _fetch(config, tmp_path, handler):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        connector = BerlinWFSConnector(HTTPFetcher(client))
        return await connector.fetch(
            FetchRequest(
                config=config,
                staging_dir=str(tmp_path / "staging"),
                snapshot_id="planning-fetch",
            )
        )


@pytest.mark.asyncio
async def test_fetch_pages_each_layer_and_records_layer_on_raw_assets(tmp_path):
    bundle = await _fetch(
        _fetch_configuration(),
        tmp_path,
        lambda request: _response_for_request(request),
    )
    assert len(bundle.assets) == 6
    assert {
        urlsplit(asset.resource_url).query.split("TYPENAMES=")[1].split("&")[0]
        for asset in bundle.assets
    } == {"bplan%3Aa_bp_iv", "bplan%3Ab_bp_fs", "bplan%3Ac_bp_ak"}
    assert len(BerlinWFSConnector().normalize(bundle, _fetch_configuration()).records) == 10


@pytest.mark.parametrize(
    ("option_changes", "mutate", "message"),
    [
        (
            {},
            lambda layer, start, data: {
                key: value for key, value in data.items() if key != "links"
            },
            "omitted the next link",
        ),
        (
            {},
            lambda layer, start, data: (
                (data.update(numberMatched=data["numberMatched"] + 1) if start == 0 else data)
                or data
            ),
            "numberMatched changed",
        ),
        ({"max_pages": 1}, None, "page cap exceeded"),
        (
            {},
            lambda layer, start, data: (
                (
                    data.update(
                        links=[
                            {
                                "rel": "next",
                                "href": str(
                                    httpx.URL("https://attacker.example" + "/changed?TYPENAMES=x")
                                ),
                            }
                        ]
                    )
                    if start == 0 and data["numberMatched"] > 2
                    else data
                )
                or data
            ),
            "URL is not allowed",
        ),
    ],
)
@pytest.mark.asyncio
async def test_fetch_rejects_incomplete_paging_or_changed_links(
    tmp_path, option_changes, mutate, message
):
    def handler(request):
        if mutate is None:
            return _response_for_request(request)
        return _response_for_request(request, mutate=mutate)

    with pytest.raises(SchemaError, match=message):
        await _fetch(_fetch_configuration(**option_changes), tmp_path, handler)


@pytest.mark.asyncio
async def test_fetch_rejects_http_200_exception_report_on_second_page(tmp_path):
    def handler(request):
        if (
            request.url.params["TYPENAMES"] == "bplan:a_bp_iv"
            and request.url.params["STARTINDEX"] == "2"
        ):
            return httpx.Response(
                200,
                content=(
                    b'<ows:ExceptionReport xmlns:ows="http://www.opengis.net/ows/1.1">'
                    b'<ows:Exception exceptionCode="InvalidParameterValue">'
                    b"<ows:ExceptionText>synthetic failure</ows:ExceptionText>"
                    b"</ows:Exception></ows:ExceptionReport>"
                ),
                request=request,
            )
        return _response_for_request(request)

    with pytest.raises(SchemaError, match="ExceptionReport"):
        await _fetch(_fetch_configuration(), tmp_path, handler)


def _projected_square(x, y, width):
    polygon = Polygon(
        [
            (x, y),
            (x + width, y),
            (x + width, y + width),
            (x, y + width),
            (x, y),
        ]
    )
    inverse = Transformer.from_crs("EPSG:25833", "EPSG:4326", always_xy=True)
    return transform(inverse.transform, polygon)


def _plan(plan_id, x, y, width, status="adopted"):
    return DevelopmentPlan(
        source_id="berlin_planning",
        snapshot_id="spatial-fixture",
        source_record_id=plan_id,
        record_id=f"berlin_planning:{plan_id}",
        geometry=mapping(_projected_square(x, y, width)),
        plan_id=plan_id,
        name=plan_id,
        status=status,
        raw_status="In Kraft getreten",
        source_layer="bplan:b_bp_fs",
        district=None,
        plan_type=None,
        procedure_type=None,
        area_description=None,
        land_use_text=None,
        aufstellungsbeschluss_date=None,
        festsetzung_date=None,
        dates={},
        document_urls={},
        attributes={},
    )


def test_spatial_index_exact_intersection_shares_radius_and_status_filters():
    plans = (
        _plan("A", 390000, 5820000, 100),
        _plan("B", 390140, 5820000, 40, status="repealed"),
        _plan("boundary", 390150, 5820000, 20),
        _plan("far", 390600, 5820000, 20),
    )
    index = DevelopmentPlanIndex(plans)
    query = Polygon(
        [
            (390050, 5820000),
            (390150, 5820000),
            (390150, 5820100),
            (390050, 5820100),
            (390050, 5820000),
        ]
    )
    matches = index.intersecting_area(query, statuses={"adopted"})
    assert [match.plan.plan_id for match in matches] == ["A"]
    assert matches[0].intersection_area_m2 == pytest.approx(5000)
    assert matches[0].plan_area_share == pytest.approx(0.5)
    assert matches[0].query_area_share == pytest.approx(0.5)
    assert matches[0].distance_m == 0

    radius_matches = index.within_radius(390050, 5820050, 100)
    assert "A" in {match.plan.plan_id for match in radius_matches}
    assert "far" not in {match.plan.plan_id for match in radius_matches}
    assert "boundary" not in {match.plan.plan_id for match in matches}
    assert {
        match.plan.plan_id
        for match in index.within_radius(390050, 5820050, 100, statuses={"repealed"})
    } == {"B"}
    for invalid_radius in (0, -1, 5001):
        with pytest.raises(ValueError):
            index.within_radius(390050, 5820050, invalid_radius)


def test_radius_membership_uses_true_circle_beyond_buffer_edge_midpoints():
    x, y, radius = 390050, 5820050, 100
    edge_angle = math.pi / 256
    edge_distance = 99.997
    edge_x = x + edge_distance * math.cos(edge_angle)
    edge_y = y + edge_distance * math.sin(edge_angle)
    edge_plan = _plan("edge", edge_x - 0.0005, edge_y - 0.0005, 0.001)
    outside_plan = _plan("outside", x + radius + 0.495, y - 0.005, 0.01)
    index = DevelopmentPlanIndex((edge_plan, outside_plan))
    forward = Transformer.from_crs("EPSG:4326", "EPSG:25833", always_xy=True)
    projected_edge = transform(forward.transform, shape(edge_plan.geometry))
    disc = Point(x, y).buffer(radius, quad_segs=64)

    assert projected_edge.distance(Point(x, y)) < radius
    assert projected_edge.intersection(disc).area == pytest.approx(0)
    matches = {match.plan.plan_id: match for match in index.within_radius(x, y, radius)}
    assert set(matches) == {"edge"}
    assert matches["edge"].distance_m < radius
    assert matches["edge"].intersection_area_m2 == pytest.approx(0)
    assert matches["edge"].query_area_share == pytest.approx(0)


class PlanningAPIService:
    def __init__(self):
        config = configuration()
        self.configs = {"berlin_planning": config}
        self.plan = _plan("0206101g/1", 390000, 5820000, 100)
        self.plan = replace(
            self.plan,
            raw_status="In Kraft getreten",
            document_urls={"url_www": "https://example.org/plan"},
            land_use_text="Mischgebiet",
        )
        self.area = Area(
            source_id="berlin_lor",
            snapshot_id="lor-snapshot",
            source_record_id="01100101",
            record_id="planungsraum:01100101",
            geometry=mapping(_projected_square(389950, 5819950, 200)),
            area_code="01100101",
            name="Synthetic PLR",
            hierarchy="planungsraum",
            boundary_edition="2021",
        )

    def manifest(self, dataset_id, snapshot_id):
        return {
            "dataset_id": dataset_id,
            "snapshot_id": snapshot_id,
            "resource_url": self.configs["berlin_planning"].resource_url,
            "landing_url": "https://daten.berlin.de/plans",
            "retrieved_at": "2026-10-18T00:00:00Z",
            "license_id": "DL-DE-ZERO-2.0",
            "license_url": "https://www.govdata.de/dl-de/zero-2-0",
            "attribution": "Berlin",
            "quality_report": {"warnings": ["synthetic warning"]},
            "coverage": {},
            "parent_snapshot_ids": ["lor-snapshot"],
        }

    def records(self, dataset_id, snapshot_id, **filters):
        if dataset_id == "berlin_lor":
            records = [record_to_dict(self.area)]
        else:
            records = [record_to_dict(self.plan)]
        return {
            "records": records,
            "next_cursor": None,
            "count": len(records),
            "snapshot_id": snapshot_id,
        }


def test_planning_api_returns_geometry_documents_provenance_guard_and_matches():
    service = PlanningAPIService()
    plan_lon, plan_lat = Transformer.from_crs("EPSG:25833", "EPSG:4326", always_xy=True).transform(
        390050, 5820050
    )
    with TestClient(create_app(service=service)) as client:
        near = client.get(
            "/api/v1/planning/plans/near",
            params={
                "snapshot_id": "plan-snapshot",
                "lon": plan_lon,
                "lat": plan_lat,
                "radius_m": 500,
                "status": "adopted",
            },
        )
        in_area = client.get(
            "/api/v1/planning/plans/in-area",
            params={"snapshot_id": "plan-snapshot", "area_code": "01100101"},
        )
        detail = client.get(
            "/api/v1/planning/plans/0206101g/1",
            params={"snapshot_id": "plan-snapshot"},
        )
    for response in (near, in_area, detail):
        assert response.status_code == 200
        payload = response.json()
        assert payload["snapshot_id"] == "plan-snapshot"
        assert payload["interpretation_guard"] == PLAN_INTERPRETATION_GUARD
        assert payload["excluded_claims"] == list(EXCLUDED_CLAIMS)
        assert payload["distance_semantics"] == (
            "straight-line metres in EPSG:25833, not walking time"
        )
        assert payload["provenance"]["license_id"] == "DL-DE-ZERO-2.0"
        assert set(payload["provenance"]["layer_source_urls"]) == {
            "bplan:a_bp_iv",
            "bplan:b_bp_fs",
            "bplan:c_bp_ak",
        }
        assert payload["quality"]["warnings"] == ["synthetic warning"]
    item = near.json()["plans"][0]
    assert item["plan_id"] == "0206101g/1"
    assert item["status"] == "adopted"
    assert item["raw_status"] == "In Kraft getreten"
    assert item["geometry"]["type"] == "Polygon"
    assert item["documents"][0]["availability"] == "not_checked"
    assert "intersection_area_m2" in item
    assert not {
        "residents",
        "completion",
        "rent",
        "permitted",
    }.intersection(item)
    assert in_area.json()["plans"][0]["plan_id"] == "0206101g/1"
    assert detail.json()["plan"]["plan_id"] == "0206101g/1"
    assert detail.json()["plan"]["document_status"] == "linked"
    assert (
        dict(parse_qsl(urlsplit(detail.json()["plan"]["source_url"]).query))["TYPENAMES"]
        == "bplan:b_bp_fs"
    )
    assert (
        dict(
            parse_qsl(
                urlsplit(detail.json()["provenance"]["layer_source_urls"]["bplan:b_bp_fs"]).query
            )
        )["TYPENAMES"]
        == "bplan:b_bp_fs"
    )


def test_planning_api_rejects_unknown_filters_and_reports_missing_area():
    service = PlanningAPIService()
    plan_lon, plan_lat = Transformer.from_crs("EPSG:25833", "EPSG:4326", always_xy=True).transform(
        390050, 5820050
    )
    with TestClient(create_app(service=service)) as client:
        unknown = client.get(
            "/api/v1/planning/plans/near",
            params={
                "snapshot_id": "plan-snapshot",
                "lon": plan_lon,
                "lat": plan_lat,
                "radius_m": 500,
                "nonsense": 1,
            },
        )
        missing_area = client.get(
            "/api/v1/planning/plans/in-area",
            params={"snapshot_id": "plan-snapshot", "area_code": "99999999"},
        )
    no_document_service = PlanningAPIService()
    no_document_service.plan = replace(no_document_service.plan, document_urls={})
    with TestClient(create_app(service=no_document_service)) as client:
        no_documents = client.get(
            "/api/v1/planning/plans/near",
            params={
                "snapshot_id": "plan-snapshot",
                "lon": plan_lon,
                "lat": plan_lat,
                "radius_m": 500,
            },
        )
    assert unknown.status_code == 422
    assert missing_area.status_code == 404
    assert no_documents.json()["plans"][0]["document_status"] == "none_published"


def test_development_plan_records_round_trip_through_geoparquet(tmp_path):
    records = BerlinWFSConnector().normalize(raw_bundle(), configuration()).records
    target = tmp_path / "plans.parquet"
    write_records(records, target)
    loaded = read_records(target)
    assert len(loaded) == len(records)
    assert isinstance(loaded[0], DevelopmentPlan)
    assert record_from_dict(record_to_dict(loaded[0])) == loaded[0]
    assert shape(loaded[0].geometry).geom_type in {"Polygon", "MultiPolygon"}


@pytest.mark.asyncio
async def test_local_file_import_is_rejected_without_publishing_planning_snapshot(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "berlin_planning.json").write_text(CONFIG_FILE.read_text())
    lor_values = json.loads((BACKEND / "config/datasets/berlin_lor.json").read_text())
    lor_values["parser_options"]["expected_rows"] = 2
    lor_values["source_crs"] = "EPSG:4326"
    (config_dir / "berlin_lor.json").write_text(json.dumps(lor_values))
    service = DataService(
        tmp_path / "data",
        config_dir,
        connectors={"lor": LORConnector(), "berlin_wfs": BerlinWFSConnector()},
    )
    parent = await service.ingest(
        "berlin_lor",
        local_file=BACKEND / "tests/fixtures/lor/areas.geojson",
    )

    with pytest.raises(
        SchemaError,
        match=(
            "provider download of all three bplan layers; single-file local import is not supported"
        ),
    ):
        await service.ingest(
            "berlin_planning",
            local_file=FIXTURES / "a_bp_iv.json",
            parent_snapshot_id=parent.snapshot_id,
        )

    dataset = next(
        item for item in service.list_datasets() if item["dataset_id"] == "berlin_planning"
    )
    assert dataset["latest_snapshot_id"] is None
    assert dataset["availability"] == "unavailable"


@pytest.mark.asyncio
async def test_failed_refresh_preserves_latest_snapshot_and_api_records(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    lor_values = json.loads((BACKEND / "config/datasets/berlin_lor.json").read_text())
    lor_values["parser_options"]["expected_rows"] = 2
    lor_values["source_crs"] = "EPSG:4326"
    plan_values = json.loads(CONFIG_FILE.read_text())
    plan_values["resource_url"] = plan_values["resource_url"].replace(
        "gdi.berlin.de", "example.org"
    )
    plan_values["allowed_hosts"] = ["example.org"]
    options = plan_values["parser_options"]
    options.update(page_size=2, max_pages=10, max_total_bytes=2_000_000)
    options["layers"] = [
        {
            **layer,
            "expected_count_range": [
                len(
                    json.loads((FIXTURES / f"{layer['typename'].split(':')[-1]}.json").read_text())[
                        "features"
                    ]
                ),
            ]
            * 2,
        }
        for layer in options["layers"]
    ]
    (config_dir / "berlin_lor.json").write_text(json.dumps(lor_values))
    (config_dir / "berlin_planning.json").write_text(json.dumps(plan_values))
    fail_refresh = False

    def handler(request):
        if (
            fail_refresh
            and request.url.params["TYPENAMES"] == "bplan:a_bp_iv"
            and request.url.params["STARTINDEX"] == "2"
        ):
            return httpx.Response(
                200,
                content=(
                    b'<ows:ExceptionReport xmlns:ows="http://www.opengis.net/ows/1.1">'
                    b'<ows:Exception exceptionCode="InvalidParameterValue">'
                    b"<ows:ExceptionText>failed refresh</ows:ExceptionText>"
                    b"</ows:Exception></ows:ExceptionReport>"
                ),
                request=request,
            )
        return _response_for_request(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        service = DataService(
            tmp_path / "data",
            config_dir,
            connectors={
                "lor": LORConnector(),
                "berlin_wfs": BerlinWFSConnector(HTTPFetcher(http)),
            },
        )
        parent = await service.ingest(
            "berlin_lor",
            local_file=BACKEND / "tests/fixtures/lor/areas.geojson",
        )
        first = await service.ingest("berlin_planning", parent_snapshot_id=parent.snapshot_id)
        fail_refresh = True
        with pytest.raises(SchemaError, match="ExceptionReport"):
            await service.ingest("berlin_planning", parent_snapshot_id=parent.snapshot_id)

    assert (
        service.list_datasets()[
            [item["dataset_id"] for item in service.list_datasets()].index("berlin_planning")
        ]["latest_snapshot_id"]
        == first.snapshot_id
    )
    page = service.records("berlin_planning", first.snapshot_id, limit=1000)
    assert page["count"] == 10
    with TestClient(create_app(service=service)) as client:
        response = client.get(
            "/api/v1/planning/plans/0206101g/1",
            params={"snapshot_id": first.snapshot_id},
        )
        assert response.status_code == 200
        records_path = service._snapshot_path(
            service.catalog.get("berlin_planning", first.snapshot_id)
        )
        assert records_path.name == "records.parquet"
        records_path.unlink()
        unavailable = client.get(
            "/api/v1/planning/plans/0206101g/1",
            params={"snapshot_id": first.snapshot_id},
        )
    assert response.status_code == 200
    assert response.json()["plan"]["plan_id"] == "0206101g/1"
    assert unavailable.status_code == 503
    assert unavailable.json()["error"]["code"] == "SOURCE_UNAVAILABLE"
