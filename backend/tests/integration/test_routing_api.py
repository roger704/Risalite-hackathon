"""Offline API contracts for walking routing."""

import asyncio
import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.connectors.valhalla import ValhallaWalkingClient
from app.domain.routing import RoutingBudget, load_routing_config

BACKEND = Path(__file__).parents[2]
FIXTURES = Path(__file__).parents[1] / "fixtures" / "valhalla"


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


def api_client(*, budget=None, failure=False, route_error=False):
    calls = []

    def respond(request):
        calls.append(request)
        if failure:
            raise httpx.ConnectError("offline", request=request)
        if request.url.path == "/status":
            return httpx.Response(200, json=fixture("status.json"))
        if request.url.path == "/route":
            if route_error:
                return httpx.Response(
                    400,
                    json={"error_code": 442, "error": "No path could be found for input"},
                )
            return httpx.Response(200, json=fixture("route_ok.json"))
        if request.url.path == "/isochrone":
            return httpx.Response(200, json=fixture("isochrone_ok.json"))
        return httpx.Response(404)

    transport = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    config = load_routing_config(BACKEND / "config/routing/valhalla_berlin_walking.json")
    routing = ValhallaWalkingClient(config, budget=budget, client=transport)
    return create_app(routing_client=routing), transport, calls


def test_routing_api_returns_status_route_and_area_contracts():
    app, transport, _ = api_client()
    with TestClient(app) as client:
        status = client.get("/api/v1/routing/walking/status")
        route = client.get(
            "/api/v1/routing/walking/route",
            params={"origin": "13.4132,52.5219", "destination": "13.3777,52.5163"},
        )
        area = client.get(
            "/api/v1/routing/walking/reachable-area",
            params={"origin": "13.4132,52.5219", "minutes": "5,10,15"},
        )
    assert status.status_code == route.status_code == area.status_code == 200
    assert status.json()["available"] is True
    assert status.json()["budget"] == {"max_requests": 500, "used": 0}
    assert route.json()["measure"] == "network_walking_estimate"
    assert route.json()["attribution"]
    assert route.json()["warnings"]
    assert area.json()["kind"] == "walking_reachable_area"
    assert area.json()["measure"] == "network_walking_isochrone_estimate"
    assert len(area.json()["contours"]) == 3
    asyncio.run(transport.aclose())


def test_routing_api_validation_unknown_params_budget_and_unreachable():
    app, transport, _ = api_client()
    with TestClient(app) as client:
        bad_coordinate = client.get(
            "/api/v1/routing/walking/route",
            params={"origin": "52.52,13.41", "destination": "13.3777,52.5163"},
        )
        unknown = client.get("/api/v1/routing/walking/status", params={"unexpected": "true"})
        unreachable_app, unreachable_transport, _ = api_client(route_error=True)
        with TestClient(unreachable_app) as unreachable_client:
            unreachable = unreachable_client.get(
                "/api/v1/routing/walking/route",
                params={"origin": "13.4132,52.5219", "destination": "13.3777,52.5163"},
            )
    assert bad_coordinate.status_code == 422
    assert bad_coordinate.json()["error"]["code"] == "COORDINATE_OUT_OF_COVERAGE"
    assert unknown.status_code == 422
    assert unknown.json()["error"]["code"] == "UNSUPPORTED_FILTER"
    assert unreachable.status_code == 200
    assert unreachable.json()["status"] == "unreachable"
    asyncio.run(transport.aclose())
    asyncio.run(unreachable_transport.aclose())

    app, transport, _ = api_client(budget=RoutingBudget(1))
    with TestClient(app) as client:
        first = client.get(
            "/api/v1/routing/walking/route",
            params={"origin": "13.4132,52.5219", "destination": "13.3777,52.5163"},
        )
        second = client.get(
            "/api/v1/routing/walking/route",
            params={"origin": "13.4132,52.5219", "destination": "13.38,52.5163"},
        )
    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "ROUTING_BUDGET_EXCEEDED"
    asyncio.run(transport.aclose())


def test_routing_api_source_unavailable_is_503():
    app, transport, _ = api_client(failure=True)
    with TestClient(app) as client:
        response = client.get("/api/v1/routing/walking/status")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SOURCE_UNAVAILABLE"
    asyncio.run(transport.aclose())


def test_routing_api_rejects_unrecognized_minutes_as_422():
    app, transport, _ = api_client()
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/routing/walking/reachable-area",
            params={"origin": "13.4132,52.5219", "minutes": "5,nope"},
        )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ROUTING_LIMIT_EXCEEDED"
    asyncio.run(transport.aclose())
