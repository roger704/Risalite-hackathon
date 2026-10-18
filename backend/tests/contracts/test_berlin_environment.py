"""Berlin environmental WFS adapter contracts and offline normalization."""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
import pytest
from shapely.affinity import translate
from shapely.geometry import mapping, shape

from app.connectors.berlin_environment import BerlinEnvironmentConnector
from app.domain import (
    AreaIndicatorClass,
    DatasetConfig,
    FetchRequest,
    RawAsset,
    RawBundle,
    SchemaError,
    ValidationError,
    ValueStatus,
    record_from_dict,
    record_to_dict,
)
from app.infrastructure.http import HTTPFetcher

BACKEND = Path(__file__).parents[2]
FIXTURES = BACKEND / "tests/fixtures/berlin_environment"
CONFIG_PATH = BACKEND / "config/datasets/berlin_environment.json"


def configuration(**option_changes):
    values = json.loads(CONFIG_PATH.read_text())
    values["allowed_hosts"] = tuple(values["allowed_hosts"])
    values["parser_options"].update(option_changes)
    return DatasetConfig(**values)


def layer_url(config, typename):
    parts = urlsplit(config.resource_url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query.append(("TYPENAMES", typename))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def raw_bundle(config, paths=None, snapshot_id="environment-test"):
    assets = []
    for layer in config.parser_options["layers"]:
        path = (
            Path(paths[layer["typename"].split(":")[-1]])
            if paths
            else (FIXTURES / f"{layer['typename'].split(':')[-1]}.json")
        )
        content = path.read_bytes()
        assets.append(
            RawAsset(
                path=str(path),
                sha256=hashlib.sha256(content).hexdigest(),
                size_bytes=len(content),
                resource_url=layer_url(config, layer["typename"]),
            )
        )
    return RawBundle(
        assets=tuple(assets),
        retrieved_at=datetime.now(timezone.utc),
        snapshot_id=snapshot_id,
        request_fingerprint="environment-test",
    )


def edit_layer(tmp_path, layer_name, mutate):
    paths = {}
    for fixture in FIXTURES.glob("*.json"):
        data = json.loads(fixture.read_text())
        if fixture.stem == layer_name:
            mutate(data)
        path = tmp_path / fixture.name
        path.write_text(json.dumps(data, ensure_ascii=False))
        paths[fixture.stem] = path
    return paths


def test_area_indicator_class_contract_and_round_trip():
    record = AreaIndicatorClass(
        source_id="berlin_environment",
        snapshot_id="snapshot",
        source_record_id="a_laerm2023.01100101",
        record_id="noise_burden:01100101",
        area_code="01100101",
        hierarchy="planungsraum",
        boundary_edition="2021",
        indicator_code="noise_burden",
        class_code="hoch",
        class_label="hoch",
        class_rank=3,
        class_count=3,
        rank_direction="higher_is_more_burden",
        reference_period="2022",
        value_status=ValueStatus.ESTIMATED,
        attributes={"source_layer": "a_laerm2023", "source_field": "kategorie"},
    )
    assert record_from_dict(record_to_dict(record)) == record
    assert record.record_type == "area_indicator_class"


def test_area_indicator_class_requires_null_fields_for_missing_status():
    with pytest.raises(ValidationError, match="Missing classes must have null class fields"):
        AreaIndicatorClass(
            source_id="berlin_environment",
            snapshot_id="snapshot",
            source_record_id="a_laerm2023.03400831",
            record_id="noise_burden:03400831",
            area_code="03400831",
            hierarchy="planungsraum",
            boundary_edition="2021",
            indicator_code="noise_burden",
            class_code="hoch",
            class_label="hoch",
            class_rank=3,
            class_count=3,
            rank_direction="higher_is_more_burden",
            reference_period="2022",
            value_status="missing",
        )


@pytest.mark.parametrize("rank", [0, 4, True])
def test_area_indicator_class_rejects_rank_outside_positive_integer_range(rank):
    with pytest.raises(ValidationError, match="class_rank"):
        AreaIndicatorClass(
            source_id="berlin_environment",
            snapshot_id="snapshot",
            source_record_id="a_laerm2023.01100101",
            record_id="noise_burden:01100101",
            area_code="01100101",
            hierarchy="planungsraum",
            boundary_edition="2021",
            indicator_code="noise_burden",
            class_code="hoch",
            class_label="hoch",
            class_rank=rank,
            class_count=3,
            rank_direction="higher_is_more_burden",
            reference_period="2022",
            value_status="estimated",
        )


def test_area_indicator_class_rejects_forecast_status():
    with pytest.raises(ValidationError, match="cannot be forecasts"):
        AreaIndicatorClass(
            source_id="berlin_environment",
            snapshot_id="snapshot",
            source_record_id="a_laerm2023.01100101",
            record_id="noise_burden:01100101",
            area_code="01100101",
            hierarchy="planungsraum",
            boundary_edition="2021",
            indicator_code="noise_burden",
            class_code="hoch",
            class_label="hoch",
            class_rank=3,
            class_count=3,
            rank_direction="higher_is_more_burden",
            reference_period="2022",
            value_status="forecast",
        )


def test_normalizes_all_layer_classes_and_preserves_missing_values():
    config = configuration(page_size=3, max_pages=2, expected_count=3)
    connector = BerlinEnvironmentConnector()
    batch = connector.normalize(raw_bundle(config), config)

    areas = [record for record in batch.records if record.record_type == "area"]
    classes = [record for record in batch.records if record.record_type == "area_indicator_class"]
    assert len(areas) == 3
    assert len(classes) == 27
    assert batch.input_rows == 24
    assert areas[0].record_id == "planungsraum:01100101"
    assert areas[0].attributes == {"source_layer": "a_laerm2023"}
    assert areas[0].geometry["type"] in {"Polygon", "MultiPolygon"}
    assert 13.0 < shape(areas[0].geometry).centroid.x < 13.9
    assert 52.3 < shape(areas[0].geometry).centroid.y < 52.8

    first_area_classes = {
        record.indicator_code: record for record in classes if record.area_code == "01100101"
    }
    assert {code: record.class_label for code, record in first_area_classes.items()} == {
        "noise_burden": "hoch",
        "air_pollution_burden": "hoch",
        "green_space_provision": "schlecht",
        "thermal_burden": "hoch",
        "social_disadvantage": "mittlerer Status-Index",
        "multiple_burden_environment": "vierfach",
        "multiple_burden_environment_social": "vierfach",
        "population_density_class": "10.000 bis unter 20.000",
        "simple_residential_location_hint": None,
    }
    assert {code: record.class_code for code, record in first_area_classes.items()} == {
        "noise_burden": "hoch",
        "air_pollution_burden": "hoch",
        "green_space_provision": "schlecht",
        "thermal_burden": "hoch",
        "social_disadvantage": "medium_status",
        "multiple_burden_environment": "4",
        "multiple_burden_environment_social": "4",
        "population_density_class": "10000_to_under_20000",
        "simple_residential_location_hint": None,
    }
    assert {code: record.class_rank for code, record in first_area_classes.items()} == {
        "noise_burden": 3,
        "air_pollution_burden": 3,
        "green_space_provision": 3,
        "thermal_burden": 3,
        "social_disadvantage": 2,
        "multiple_burden_environment": 5,
        "multiple_burden_environment_social": 5,
        "population_density_class": 2,
        "simple_residential_location_hint": None,
    }
    assert {code: record.reference_period for code, record in first_area_classes.items()} == {
        "noise_burden": "2022",
        "air_pollution_burden": "2023",
        "green_space_provision": "2020",
        "thermal_burden": "2022",
        "social_disadvantage": "2023",
        "multiple_burden_environment": "2023/2024",
        "multiple_burden_environment_social": "2023/2024",
        "population_density_class": "2023-12-31",
        "simple_residential_location_hint": "2024",
    }
    assert {code: record.value_status for code, record in first_area_classes.items()} == {
        "noise_burden": ValueStatus.ESTIMATED,
        "air_pollution_burden": ValueStatus.ESTIMATED,
        "green_space_provision": ValueStatus.ESTIMATED,
        "thermal_burden": ValueStatus.ESTIMATED,
        "social_disadvantage": ValueStatus.OBSERVED,
        "multiple_burden_environment": ValueStatus.ESTIMATED,
        "multiple_burden_environment_social": ValueStatus.ESTIMATED,
        "population_density_class": ValueStatus.OBSERVED,
        "simple_residential_location_hint": ValueStatus.MISSING,
    }
    noise = {
        record.area_code: record for record in classes if record.indicator_code == "noise_burden"
    }
    assert noise["01100101"].source_record_id == "a_laerm2023.01100101"
    assert noise["01100101"].attributes == {
        "source_layer": "a_laerm2023",
        "source_field": "kategorie",
        "plr_name": "Stülerstraße",
    }
    assert noise["01100101"].class_label == "hoch"
    assert noise["01100101"].class_rank == 3
    assert noise["03400831"].class_label is None
    assert noise["03400831"].value_status == ValueStatus.MISSING
    assert noise["03400831"].quality_flags == ("source_null:Gebiete ohne Zuordnung",)
    residential = {
        record.area_code: record
        for record in classes
        if record.indicator_code == "simple_residential_location_hint"
    }
    assert residential["01100101"].class_code is None
    assert residential["01100101"].quality_flags == ("source_null:not_flagged_or_unknown",)
    assert all(not hasattr(record, "geometry") for record in classes)
    assert connector.validate(batch).valid
    assert any("noise_burden classes: gering=" in warning for warning in batch.warnings)
    assert any("missing=2" in warning for warning in batch.warnings)
    assert any("Maximum cross-layer geometry" in warning for warning in batch.warnings)


def test_normalizer_rejects_unknown_class_and_missing_or_unknown_layer_asset(tmp_path):
    config = configuration(page_size=3, max_pages=2, expected_count=3)
    paths = edit_layer(
        tmp_path,
        "b_luft2023",
        lambda data: data["features"][0]["properties"].update(kategorie="unreviewed"),
    )
    with pytest.raises(SchemaError, match="b_luft2023.kategorie='unreviewed'"):
        BerlinEnvironmentConnector().normalize(raw_bundle(config, paths), config)

    bundle = raw_bundle(config)
    with pytest.raises(SchemaError, match="missing layer assets"):
        BerlinEnvironmentConnector().normalize(
            RawBundle(
                assets=bundle.assets[:-1],
                retrieved_at=bundle.retrieved_at,
                snapshot_id=bundle.snapshot_id,
                request_fingerprint=bundle.request_fingerprint,
            ),
            config,
        )
    unknown = bundle.assets[0]
    unknown_asset = RawAsset(
        path=unknown.path,
        sha256=unknown.sha256,
        size_bytes=unknown.size_bytes,
        resource_url=layer_url(config, "other_namespace:a_laerm2023"),
    )
    with pytest.raises(SchemaError, match="Unknown Berlin environment asset layer"):
        BerlinEnvironmentConnector().normalize(
            RawBundle(
                assets=(unknown_asset, *bundle.assets[1:]),
                retrieved_at=bundle.retrieved_at,
                snapshot_id=bundle.snapshot_id,
                request_fingerprint=bundle.request_fingerprint,
            ),
            config,
        )


def test_normalizer_rejects_string_null_as_class_label(tmp_path):
    config = configuration(page_size=3, max_pages=2, expected_count=3)
    paths = edit_layer(
        tmp_path,
        "z_gesamt_umwelt2023",
        lambda data: data["features"][0]["properties"].update(wol_l_l="null"),
    )
    with pytest.raises(
        SchemaError,
        match="Unknown class label z_gesamt_umwelt2023.wol_l_l='null'",
    ):
        BerlinEnvironmentConnector().normalize(raw_bundle(config, paths), config)


def test_normalizer_rejects_single_local_file_without_layer_typename():
    config = configuration(page_size=3, max_pages=2, expected_count=3)
    fixture = FIXTURES / "a_laerm2023.json"
    content = fixture.read_bytes()
    asset = RawAsset(
        path=str(fixture),
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        resource_url=config.resource_url,
    )
    with pytest.raises(SchemaError, match="Local --file imports are not supported"):
        BerlinEnvironmentConnector().normalize(
            RawBundle(
                assets=(asset,),
                retrieved_at=datetime.now(timezone.utc),
                snapshot_id="unsupported-file",
                request_fingerprint="unsupported-file",
            ),
            config,
        )


@pytest.mark.parametrize(
    ("layer_name", "mutate", "message"),
    [
        (
            "f_mehrfach4_2023",
            lambda data: data["features"][0]["properties"].update(laerm="gering"),
            "component mismatch laerm",
        ),
        (
            "z_gesamt_umwelt2023",
            lambda data: data["features"][0]["properties"].update(kategorie="einfach"),
            "z_gesamt_umwelt2023.kategorie differs",
        ),
        (
            "b_luft2023",
            lambda data: data["features"][0]["properties"].update(plr_id="00000000"),
            "PLR set mismatch",
        ),
        (
            "a_laerm2023",
            lambda data: data.update(
                crs={"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::4326"}}
            ),
            "CRS differs",
        ),
        (
            "a_laerm2023",
            lambda data: data.update(numberMatched=4),
            "count mismatch",
        ),
        (
            "a_laerm2023",
            lambda data: data["features"][0].update(
                geometry=mapping(translate(shape(data["features"][0]["geometry"]), xoff=200000))
            ),
            "outside source_bbox",
        ),
    ],
)
def test_normalizer_rejects_layer_consistency_violations(tmp_path, layer_name, mutate, message):
    config = configuration(page_size=3, max_pages=2, expected_count=3)
    paths = edit_layer(tmp_path, layer_name, mutate)
    with pytest.raises(SchemaError, match=message):
        BerlinEnvironmentConnector().normalize(raw_bundle(config, paths), config)


async def test_fetch_pages_all_layers_in_order_with_encoded_next_links(tmp_path):
    requests = []
    config = configuration(page_size=2, max_pages=2, expected_count=3)

    async def handler(request):
        requests.append(request)
        typename = request.url.params["TYPENAMES"]
        layer_name = typename.split(":")[-1]
        source = json.loads((FIXTURES / f"{layer_name}.json").read_text())
        start = int(request.url.params["STARTINDEX"])
        count = int(request.url.params["COUNT"])
        source["features"] = source["features"][start : start + count]
        source["numberMatched"] = 3
        source["numberReturned"] = len(source["features"])
        if start + count < 3:
            pairs = [
                (key, value)
                for key, value in request.url.params.multi_items()
                if key.upper() not in {"COUNT", "STARTINDEX"}
            ]
            pairs.extend((("COUNT", str(count)), ("STARTINDEX", str(start + count))))
            source["links"] = [
                {
                    "rel": "next",
                    "href": urlunsplit(
                        (
                            urlsplit(str(request.url)).scheme,
                            urlsplit(str(request.url)).netloc,
                            urlsplit(str(request.url)).path,
                            urlencode(list(reversed(pairs)), safe=""),
                            "",
                        )
                    ),
                }
            ]
        else:
            source["links"] = []
        return httpx.Response(200, json=source, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        bundle = await BerlinEnvironmentConnector(HTTPFetcher(client)).fetch(
            FetchRequest(
                config=config,
                staging_dir=str(tmp_path / "stage"),
                snapshot_id="environment-fetch",
            )
        )

    expected_typenames = [
        layer["typename"] for layer in config.parser_options["layers"] for _ in range(2)
    ]
    assert [
        dict(parse_qsl(urlsplit(asset.resource_url).query))["TYPENAMES"] for asset in bundle.assets
    ] == [typename for typename in expected_typenames]
    assert [request.url.params["STARTINDEX"] for request in requests] == ["0", "2"] * 8
    assert len(bundle.assets) == 16
    assert len(bundle.request_fingerprint) == 64


async def test_fetch_rejects_exception_report_returned_with_http_200(tmp_path):
    exception = (BACKEND / "tests/fixtures/berlin_wfs/exception_report.xml").read_bytes()
    config = configuration(page_size=2, max_pages=2, expected_count=3)

    async def handler(request):
        return httpx.Response(200, content=exception, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        connector = BerlinEnvironmentConnector(HTTPFetcher(client))
        with pytest.raises(SchemaError, match="OGC ExceptionReport"):
            await connector.fetch(
                FetchRequest(
                    config=config,
                    staging_dir=str(tmp_path / "stage"),
                    snapshot_id="environment-exception",
                )
            )
