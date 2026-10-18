"""Opt-in smoke tests for the reviewed local or configured Valhalla service."""

import os

import pytest

from app.connectors.valhalla import ValhallaWalkingClient
from app.domain.routing import RouteStatus, RoutingBudget, load_routing_config


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("RUN_LIVE_TESTS") != "1",
    reason="set RUN_LIVE_TESTS=1 to contact the configured Valhalla service",
)
@pytest.mark.asyncio
async def test_live_valhalla_status_route_isochrone_and_berlin_extract_limit():
    config = load_routing_config()
    budget = RoutingBudget(3)
    async with ValhallaWalkingClient(config, budget=budget) as client:
        graph = await client.status()
        assert graph.engine_version == config.engine_version
        route = await client.route("13.4132,52.5219", "13.3777,52.5163")
        assert route.status is RouteStatus.OK
        assert 20 * 60 <= route.duration_s <= 60 * 60
        area = await client.reachable_area("13.4132,52.5219", [10])
        assert area.status is RouteStatus.OK
        outside = await client.route("13.06,52.40", "13.061,52.401")
        assert outside.status is RouteStatus.INVALID_SNAP
        assert budget.used == 3
