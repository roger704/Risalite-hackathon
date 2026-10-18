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
async def test_coordinates_are_normalized_before_requests_and_cache_lookup(tmp_path):
    cache = RoutingCache(tmp_path / "rounded.sqlite")
    client, transport, calls = mock_client(cache=cache)
    async with transport:
        first = await client.route("13.4132001,52.5219001", "13.3777,52.5163")
        second = await client.route("13.4132002,52.5219001", "13.3777,52.5163")
    assert first.origin == second.origin == [13.4132, 52.5219]
    assert first.straight_line == second.straight_line
    assert second.cache_hit is True
    route_calls = [call for call in calls if call.url.path == "/route"]
    assert len(route_calls) == 1
    request = json.loads(route_calls[0].content)
    assert request["locations"][0]["lon"] == 13.4132
    assert request["locations"][0]["lat"] == 52.5219


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
    with pytest.raises(ConnectorError) as error:
        parse_coordinate("1" * 65, config)
    assert error.value.code == "INVALID_COORDINATES"

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


def test_routing_budget_renews_after_its_fixed_window():
    now = [100.0]
    budget = RoutingBudget(1, window_seconds=3600, clock=lambda: now[0])
    budget.consume()
    with pytest.raises(ConnectorError) as error:
        budget.consume()
    assert error.value.code == "ROUTING_BUDGET_EXCEEDED"
    now[0] += 3599
    with pytest.raises(ConnectorError):
        budget.consume()
    now[0] += 1
    budget.consume()
    assert budget.used == 1
    assert budget.to_dict() == {
        "max_requests": 1,
        "used": 1,
        "window_seconds": 3600,
    }
    now[0] += 3600
    assert budget.to_dict()["used"] == 0


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


@pytest.mark.asyncio
@pytest.mark.parametrize("without_content_length", [False, True])
async def test_oversized_streamed_response_is_rejected_and_not_cached(
    tmp_path, without_content_length
):
    class OversizedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"x" * 512
            yield b"x" * 513

        async def aclose(self):
            pass

    def oversized(request):
        if request.url.path != "/route":
            return None
        if without_content_length:
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                stream=OversizedStream(),
            )
        return httpx.Response(
            200,
            content=b"x" * 1025,
            headers={"content-type": "application/json"},
        )

    config = load_routing_config(CONFIG_PATH)
    config = replace(config, limits={**config.limits, "max_response_bytes": 1024})
    cache = RoutingCache(tmp_path / "oversized.sqlite")
    client, transport, calls = mock_client(config=config, cache=cache, on_request=oversized)
    async with transport:
        for _ in range(2):
            with pytest.raises(SourceUnavailableError, match="max_response_bytes") as error:
                await client.route("13.4132,52.5219", "13.3777,52.5163")
            assert error.value.retryable is False
    assert len([call for call in calls if call.url.path == "/route"]) == 2


@pytest.mark.asyncio
async def test_graph_manifest_verifies_pinned_source_and_timestamp(tmp_path):
    config = load_routing_config(CONFIG_PATH)
    status = fixture("status.json")
    manifest_path = tmp_path / "graph_manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "source_sha256": config.graph["source_sha256"],
                "source_edition": config.graph["source_edition"],
                "tileset_last_modified": status["tileset_last_modified"],
            }
        )
    )
    config = replace(config, graph={**config.graph, "manifest_path": str(manifest_path)})
    cache = RoutingCache(tmp_path / "verified.sqlite")
    unverified_client, unverified_transport, _ = mock_client(cache=cache)
    async with unverified_transport:
        unverified = await unverified_client.route("13.4132,52.5219", "13.3777,52.5163")
    client, transport, _ = mock_client(config=config, cache=cache)
    async with transport:
        graph = await client.status()
        route = await client.route("13.4132,52.5219", "13.3777,52.5163")
    assert graph.graph_source_verified is True
    assert route.routing["graph_source_verified"] is True
    assert route.cache_hit is True
    assert "graph_source_unverified" in unverified.quality_flags
    assert "graph_source_unverified" not in route.quality_flags


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_sha256", "0" * 64),
        ("tileset_last_modified", fixture("status.json")["tileset_last_modified"] + 1),
    ],
)
async def test_graph_manifest_mismatch_is_nonretryable(tmp_path, field, value):
    config = load_routing_config(CONFIG_PATH)
    manifest_path = tmp_path / "graph_manifest.json"
    manifest = {
        "source_sha256": config.graph["source_sha256"],
        "source_edition": config.graph["source_edition"],
        "tileset_last_modified": fixture("status.json")["tileset_last_modified"],
    }
    manifest[field] = value
    manifest_path.write_text(json.dumps(manifest))
    config = replace(config, graph={**config.graph, "manifest_path": str(manifest_path)})
    client, transport, _ = mock_client(config=config)
    async with transport:
        with pytest.raises(
            SourceUnavailableError, match="does not match the pinned routing config"
        ):
            await client.status()


@pytest.mark.asyncio
async def test_missing_graph_manifest_marks_route_and_cached_results_unverified(tmp_path):
    client, transport, _ = mock_client(cache=RoutingCache(tmp_path / "unverified.sqlite"))
    async with transport:
        graph = await client.status()
        first = await client.route("13.4132,52.5219", "13.3777,52.5163")
        cached = await client.route("13.4132,52.5219", "13.3777,52.5163")
        area = await client.reachable_area("13.4132,52.5219", [5, 10, 15])
    assert graph.graph_source_verified is False
    assert "graph_source_unverified" in first.quality_flags
    assert "graph_source_unverified" in cached.quality_flags
    assert "graph_source_unverified" in area.quality_flags


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
    assert (
        load_routing_config(
            path, env={"VALHALLA_GRAPH_MANIFEST": "/tmp/graph_manifest.json"}
        ).graph["manifest_path"]
        == "/tmp/graph_manifest.json"
    )


def test_recorded_error_442_fixture_is_used_and_not_sent_by_client():
    assert fixture("route_error_442.json")["error_code"] == 442
    assert "exclude_polygons" not in json.loads(
        (CONFIG_PATH.parent / "valhalla_berlin_walking.json").read_text()
    )
