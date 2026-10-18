"""Offline adapter contracts for the self-hosted Valhalla walking service."""

import copy
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from app.connectors.valhalla import ValhallaWalkingClient, decode_polyline6
from app.domain.errors import ConfigError, ConnectorError, SourceUnavailableError
from app.domain.routing import (
    RouteStatus,
    RoutingBudget,
    load_routing_config,
    parse_coordinate,
)
from app.infrastructure.routing_cache import RoutingCache

BACKEND = Path(__file__).parents[2]
FIXTURES = Path(__file__).parents[1] / "fixtures" / "valhalla"
CONFIG_PATH = BACKEND / "config" / "routing" / "valhalla_berlin_walking.json"


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


def mock_client(
    *,
    route_response=None,
    route_status=200,
    area_response=None,
    area_status=200,
    status_response=None,
    status_sequence=None,
    cache=None,
    budget=None,
    clock=None,
    config=None,
    on_request=None,
):
    config = config or load_routing_config(CONFIG_PATH)
    calls = []

    def respond(request):
        calls.append(request)
        if on_request:
            custom = on_request(request)
            if custom is not None:
                return custom
        if request.url.path == "/status":
            body = status_sequence.pop(0) if status_sequence else status_response
            return httpx.Response(200, json=body or fixture("status.json"))
        if request.url.path == "/route":
            return httpx.Response(route_status, json=route_response or fixture("route_ok.json"))
        if request.url.path == "/isochrone":
            return httpx.Response(area_status, json=area_response or fixture("isochrone_ok.json"))
        return httpx.Response(404, json={"error": "not found"})

    transport = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    client = ValhallaWalkingClient(
        config,
        cache=cache,
        budget=budget,
        client=transport,
        clock=clock,
    )
    return client, transport, calls


def encode_polyline6(coordinates):
    result = ""
    previous_lat = previous_lon = 0
    for lon, lat in coordinates:
        current_lat, current_lon = round(lat * 1_000_000), round(lon * 1_000_000)
        for delta in (current_lat - previous_lat, current_lon - previous_lon):
            value = ~(delta << 1) if delta < 0 else delta << 1
            while value >= 0x20:
                result += chr((0x20 | (value & 0x1F)) + 63)
                value >>= 5
            result += chr(value + 63)
        previous_lat, previous_lon = current_lat, current_lon
    return result


@pytest.mark.asyncio
async def test_route_contract_has_network_and_straight_line_measures():
    client, transport, calls = mock_client()
    async with transport:
        result = await client.route("13.4132,52.5219", "13.3777,52.5163")
    data = result.to_dict()
    assert data["schema_version"] == "1.0"
    assert data["kind"] == "walking_route"
    assert data["status"] == "ok"
    assert data["measure"] == "network_walking_estimate"
    assert data["duration_s"] == pytest.approx(1916.468)
    assert data["distance_m"] == pytest.approx(2706)
    assert data["snapped_origin"] == pytest.approx([13.413386, 52.521768])
    assert data["snapped_destination"] == pytest.approx([13.377751, 52.516272])
    assert data["geometry"]["type"] == "LineString"
    assert data["straight_line"]["measure"] == "straight_line"
    assert data["straight_line"]["crs"] == "EPSG:25833"
    assert data["straight_line"]["distance_m"] < data["distance_m"]
    assert data["straight_line"]["duration_s"] is None
    request_body = json.loads(calls[-1].content)
    assert request_body["costing"] == "pedestrian"
    assert request_body["costing_options"] == {
        "pedestrian": {"walking_speed": 5.1, "use_ferry": 0.0}
    }
    assert "exclude_polygons" not in request_body
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fixture_name", "body", "code"),
    [
        ("route_error_442.json", None, "valhalla_442"),
        (
            None,
            {"error_code": 170, "error": "Locations are in unconnected regions"},
            "valhalla_170",
        ),
    ],
)
async def test_unreachable_route_errors_are_valid_results(fixture_name, body, code):
    body = fixture(fixture_name) if fixture_name else body
    client, transport, _ = mock_client(route_response=body, route_status=400)
    async with transport:
        result = await client.route("13.4132,52.5219", "13.3777,52.5163")
    assert result.status is RouteStatus.UNREACHABLE
    assert result.duration_s is None
    assert result.distance_m is None
    assert result.failure["code"] == code


@pytest.mark.asyncio
async def test_route_171_and_computed_snap_over_limit_are_invalid_snap():
    client, transport, _ = mock_client(
        route_response=fixture("route_error_171.json"), route_status=400
    )
    async with transport:
        result = await client.route("13.06,52.40", "13.061,52.401")
    assert result.status is RouteStatus.INVALID_SNAP
    assert result.failure["code"] == "valhalla_171"

    body = fixture("route_ok.json")
    first = decode_polyline6(body["trip"]["legs"][0]["shape"])[0]
    moved_origin = f"{first[0] - 0.004},{first[1]}"
    client, transport, _ = mock_client(route_response=body)
    async with transport:
        result = await client.route(moved_origin, "13.3777,52.5163")
    assert result.status is RouteStatus.INVALID_SNAP
    assert result.failure["code"] == "snap_distance_exceeded"
    assert result.duration_s is None
    assert result.distance_m is None
    assert result.geometry is None
    assert result.snap_distance_m["origin"] > 150


@pytest.mark.asyncio
async def test_ferry_flag_and_zero_length_same_point_route():
    client, transport, _ = mock_client(route_response=fixture("route_ferry.json"))
    async with transport:
        ferry = await client.route("13.12,52.433", "13.1557,52.4311")
    assert ferry.status is RouteStatus.OK
    assert "includes_ferry" in ferry.quality_flags

    point = [13.413386, 52.521768]
    body = {
        "trip": {
            "summary": {"time": 0, "length": 0},
            "legs": [{"shape": encode_polyline6([point])}],
        }
    }
    client, transport, _ = mock_client(route_response=body)
    async with transport:
        same = await client.route(point, point)
    assert same.status is RouteStatus.OK
    assert same.duration_s == 0
    assert same.distance_m == 0
    assert same.geometry["type"] == "LineString"
    assert len(same.geometry["coordinates"]) == 2


@pytest.mark.asyncio
async def test_reachable_area_contours_are_ordered_and_measured():
    client, transport, _ = mock_client()
    async with transport:
        result = await client.reachable_area("13.4132,52.5219", [15, 5, 10])
    assert result.status is RouteStatus.OK
    assert [contour["minutes"] for contour in result.contours] == [5, 10, 15]
    assert result.contours[0]["area_m2"] < result.contours[1]["area_m2"]
    assert result.contours[1]["area_m2"] < result.contours[2]["area_m2"]
    assert all(item["geometry"]["type"] in {"Polygon", "MultiPolygon"} for item in result.contours)
    assert all(item["area_m2"] > 0 for item in result.contours)


@pytest.mark.asyncio
async def test_unsnapped_and_missing_isochrone_contours():
    client, transport, _ = mock_client(area_response=fixture("isochrone_unsnapped.json"))
    async with transport:
        result = await client.reachable_area("13.06,52.40", [10])
    assert result.status is RouteStatus.INVALID_SNAP
    assert result.failure["code"] == "snap_distance_exceeded"
    assert result.contours == ()

    body = fixture("isochrone_ok.json")
    body["features"] = [
        feature
        for feature in body["features"]
        if feature.get("properties", {}).get("contour") != 10.0
    ]
    client, transport, _ = mock_client(area_response=body)
    async with transport:
        with pytest.raises(SourceUnavailableError, match="exactly one contour"):
            await client.reachable_area("13.4132,52.5219", [5, 10, 15])


@pytest.mark.asyncio
async def test_empty_snapped_multipoint_is_invalid_snap():
    body = fixture("isochrone_unsnapped.json")
    for feature in body["features"]:
        if feature.get("properties", {}).get("type") == "snapped":
            feature["geometry"]["coordinates"] = []
    client, transport, _ = mock_client(area_response=body)
    async with transport:
        result = await client.reachable_area("13.06,52.40", [10])
    assert result.status is RouteStatus.INVALID_SNAP
    assert result.failure["code"] == "no_snapped_location"


@pytest.mark.asyncio
async def test_isochrone_unreachable_error_is_a_valid_result():
    client, transport, _ = mock_client(
        area_response={"error_code": 442, "error": "No path could be found for input"},
        area_status=400,
    )
    async with transport:
        result = await client.reachable_area("13.4132,52.5219", [10])
    assert result.status is RouteStatus.UNREACHABLE
    assert result.contours == ()
    assert result.failure["code"] == "valhalla_442"


@pytest.mark.asyncio
async def test_coordinate_minute_and_route_distance_validation_precedes_http():
    config = load_routing_config(CONFIG_PATH)
    assert parse_coordinate("13.41,52.52").to_list() == [13.41, 52.52]
    for value in ("nan,52.52", "200,52.52"):
        with pytest.raises(ConnectorError) as error:
            parse_coordinate(value, config)
        assert error.value.code == "INVALID_COORDINATES"
    with pytest.raises(ConnectorError) as error:
        parse_coordinate("52.52,13.41", config)
    assert error.value.code == "COORDINATE_OUT_OF_COVERAGE"
    with pytest.raises(ConnectorError, match="lon,lat"):
        parse_coordinate("13.4", config)

    client, transport, calls = mock_client()
    async with transport:
        with pytest.raises(ConnectorError) as error:
            await client.route("13.1,52.5", "13.3,52.5")
        assert error.value.code == "ROUTING_LIMIT_EXCEEDED"
        for values in ([0], [31], [5, 5], [5, 10, 15, 20, 25]):
            with pytest.raises(ConnectorError) as error:
                await client.reachable_area("13.4132,52.5219", values)
            assert error.value.code == "ROUTING_LIMIT_EXCEEDED"
    assert calls == []


@pytest.mark.asyncio
async def test_route_cache_graph_profile_persistence_and_budget(tmp_path):
    cache = RoutingCache(tmp_path / "routing" / "cache.sqlite")
    fake_time = [100.0]
    changed_status = copy.deepcopy(fixture("status.json"))
    changed_status["tileset_last_modified"] += 1
    statuses = [fixture("status.json"), changed_status]
    client, transport, calls = mock_client(
        cache=cache, clock=lambda: fake_time[0], status_sequence=statuses
    )
    async with transport:
        first = await client.route("13.4132,52.5219", "13.3777,52.5163")
        second = await client.route("13.4132,52.5219", "13.3777,52.5163")
        assert first.cache_hit is False
        assert second.cache_hit is True
        assert len([call for call in calls if call.url.path == "/route"]) == 1
        fake_time[0] += 61
        changed_graph = await client.route("13.4132,52.5219", "13.3777,52.5163")
        assert changed_graph.cache_hit is False
        assert changed_graph.routing["graph_version"] != first.routing["graph_version"]
    assert len([call for call in calls if call.url.path == "/route"]) == 2

    profile = copy.deepcopy(load_routing_config(CONFIG_PATH).profile)
    profile["costing_options"]["walking_speed"] = 4.0
    alternate_config = replace(load_routing_config(CONFIG_PATH), profile=profile)
    persisted, persisted_transport, persisted_calls = mock_client(
        cache=cache, config=alternate_config
    )
    async with persisted_transport:
        profile_result = await persisted.route("13.4132,52.5219", "13.3777,52.5163")
    assert profile_result.cache_hit is False
    assert alternate_config.profile_hash != load_routing_config(CONFIG_PATH).profile_hash
    assert len([call for call in persisted_calls if call.url.path == "/route"]) == 1

    second_instance, second_transport, second_calls = mock_client(cache=cache)
    async with second_transport:
        cached = await second_instance.route("13.4132,52.5219", "13.3777,52.5163")
    assert cached.cache_hit is True
    assert not [call for call in second_calls if call.url.path == "/route"]

    limited = RoutingBudget(max_requests=1)
    budget_cache = RoutingCache(tmp_path / "budget.sqlite")
    budget_client, budget_transport, _ = mock_client(cache=budget_cache, budget=limited)
    async with budget_transport:
        first = await budget_client.route("13.4132,52.5219", "13.3777,52.5163")
        assert first.cache_hit is False
        with pytest.raises(ConnectorError) as error:
            await budget_client.route("13.4132,52.5219", "13.38,52.5163")
        cached_after_exhaustion = await budget_client.route("13.4132,52.5219", "13.3777,52.5163")
        assert cached_after_exhaustion.cache_hit is True
    assert error.value.code == "ROUTING_BUDGET_EXCEEDED"
    assert error.value.retryable is True


@pytest.mark.asyncio
async def test_status_version_mismatch_and_failed_results_are_not_cached(tmp_path):
    wrong_version = fixture("status.json")
    wrong_version["version"] = "different"
    client, transport, _ = mock_client(status_response=wrong_version)
    async with transport:
        with pytest.raises(SourceUnavailableError, match="does not match reviewed") as error:
            await client.status()
    assert error.value.retryable is False

    route_calls = 0

    def fail_route(request):
        nonlocal route_calls
        if request.url.path == "/route":
            route_calls += 1
            return httpx.Response(503, json={"error": "unavailable"})
        return None

    client, transport, _ = mock_client(
        cache=RoutingCache(tmp_path / "failures.sqlite"), on_request=fail_route
    )
    async with transport:
        for _ in range(2):
            with pytest.raises(SourceUnavailableError):
                await client.route("13.4132,52.5219", "13.3777,52.5163")
    assert route_calls == 2


@pytest.mark.asyncio
async def test_timeout_and_malformed_html_are_unavailable_and_not_cached(tmp_path):
    def timeout(request):
        if request.url.path == "/route":
            raise httpx.ReadTimeout("request timed out")
        return None

    cache = RoutingCache(tmp_path / "timeout.sqlite")
    client, transport, _ = mock_client(cache=cache, on_request=timeout)
    async with transport:
        with pytest.raises(SourceUnavailableError) as error:
            await client.route("13.4132,52.5219", "13.3777,52.5163")
    assert error.value.retryable is True

    def html(request):
        if request.url.path == "/route":
            return httpx.Response(200, text="<html>maintenance</html>")
        return None

    client, transport, _ = mock_client(
        cache=cache,
        on_request=html,
    )
    async with transport:
        with pytest.raises(SourceUnavailableError, match="malformed non-JSON") as error:
            await client.route("13.4132,52.5219", "13.3777,52.5163")
    assert error.value.retryable is False


def test_routing_config_rejects_demo_hosts_userinfo_and_unallowed_overrides(tmp_path):
    base = json.loads(CONFIG_PATH.read_text())
    for url, env in (
        ("http://valhalla1.openstreetmap.de", None),
        ("http://user:secret@127.0.0.1:8002", None),
        (base["base_url"], {"VALHALLA_BASE_URL": "https://example.com"}),
        (base["base_url"], {"VALHALLA_BASE_URL": "http://valhalla.openstreetmap.de"}),
    ):
        values = copy.deepcopy(base)
        values["base_url"] = url
        path = tmp_path / "routing.json"
        path.write_text(json.dumps(values))
        with pytest.raises(ConfigError):
            load_routing_config(path, env=env)
    values = copy.deepcopy(base)
    path = tmp_path / "routing.json"
    path.write_text(json.dumps(values))
    assert load_routing_config(
        path, env={"VALHALLA_BASE_URL": "http://localhost:9000"}
    ).base_url == ("http://localhost:9000")


def test_recorded_error_442_fixture_is_used_and_not_sent_by_client():
    assert fixture("route_error_442.json")["error_code"] == 442
    assert "exclude_polygons" not in json.loads(
        (CONFIG_PATH.parent / "valhalla_berlin_walking.json").read_text()
    )
