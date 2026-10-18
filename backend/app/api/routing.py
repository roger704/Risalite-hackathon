"""HTTP endpoints for bounded self-hosted walking routes and isochrones."""

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request

from app.domain.errors import ConnectorError
from app.domain.routing import Coordinate, RoutingConfig, parse_coordinate


def _attribution(config: RoutingConfig) -> list[dict[str, str]]:
    return [
        {
            "text": "© OpenStreetMap contributors, ODbL 1.0; Berlin extract by Geofabrik GmbH.",
            "url": "https://www.openstreetmap.org/copyright",
        },
        {
            "text": "Valhalla routing engine, MIT License.",
            "url": config.engine_license_url,
        },
    ]


def _unknown_parameters(request: Request, allowed: set[str]) -> None:
    unknown = set(request.query_params) - allowed
    if unknown:
        raise ConnectorError(
            "UNSUPPORTED_FILTER", "Unsupported query filters: " + ", ".join(sorted(unknown))
        )


def _minutes(value: str) -> list[int | float]:
    try:
        parsed = []
        for part in value.split(","):
            if not part:
                raise ValueError
            number = float(part)
            parsed.append(int(number) if number.is_integer() else number)
        return parsed
    except ValueError as exc:
        raise ConnectorError(
            "ROUTING_LIMIT_EXCEEDED", "minutes must be a comma-separated list of numbers"
        ) from exc


def routing_router(get_client: Callable[[], Any]) -> APIRouter:
    router = APIRouter()

    @router.get("/api/v1/routing/walking/status")
    async def status(request: Request):
        _unknown_parameters(request, set())
        client = get_client()
        graph = await client.status()
        return {
            "available": True,
            "engine_version": graph.engine_version,
            "graph_version": graph.graph_version,
            "tileset_last_modified": client._routing_metadata(graph)["tileset_last_modified"],
            "profile_id": client.config.profile["profile_id"],
            "profile_hash": client.config.profile_hash,
            "source_edition": client.config.graph["source_edition"],
            "limits": dict(client.config.limits),
            "budget": {
                "max_requests": client.budget.max_requests,
                "used": client.budget.used,
            },
            "attribution": _attribution(client.config),
        }

    @router.get("/api/v1/routing/walking/route")
    async def route(request: Request, origin: str, destination: str):
        _unknown_parameters(request, {"origin", "destination"})
        client = get_client()
        start = parse_coordinate(origin, client.config)
        end = parse_coordinate(destination, client.config)
        result = (await client.route(start, end)).to_dict()
        result["attribution"] = _attribution(client.config)
        result["warnings"] = list(client.config.limitations)
        return result

    @router.get("/api/v1/routing/walking/reachable-area")
    async def reachable_area(request: Request, origin: str, minutes: str = "5,10,15"):
        _unknown_parameters(request, {"origin", "minutes"})
        client = get_client()
        start: Coordinate = parse_coordinate(origin, client.config)
        result = (await client.reachable_area(start, _minutes(minutes))).to_dict()
        result["attribution"] = _attribution(client.config)
        result["warnings"] = list(client.config.limitations)
        return result

    return router
