"""Local command-line access to bounded Valhalla walking estimates."""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from app.api.main import default_data_dir
from app.connectors.valhalla import ValhallaWalkingClient
from app.domain.errors import ConnectorError
from app.domain.routing import (
    DEFAULT_ROUTING_CONFIG,
    RoutingBudget,
    load_routing_config,
    parse_coordinate,
)
from app.infrastructure.routing_cache import RoutingCache, default_routing_cache_path


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description="Query self-hosted Valhalla walking routes")
    command.add_argument("--data-dir", type=Path, default=None)
    command.add_argument("--config", type=Path, default=None)
    subcommands = command.add_subparsers(dest="command", required=True)
    subcommands.add_parser("status", help="Check the configured Valhalla graph")
    route = subcommands.add_parser("route", help="Compute an estimated walking route")
    route.add_argument("--from", dest="origin", required=True)
    route.add_argument("--to", dest="destination", required=True)
    area = subcommands.add_parser("reachable-area", help="Compute walking-time contours")
    area.add_argument("--origin", required=True)
    area.add_argument("--minutes", default="5,10,15")
    return command


def _parse_minutes(value: str) -> list[int | float]:
    try:
        values = []
        for part in value.split(","):
            if not part:
                raise ValueError
            number = float(part)
            values.append(int(number) if number.is_integer() else number)
        return values
    except ValueError as exc:
        raise ConnectorError(
            "ROUTING_LIMIT_EXCEEDED", "minutes must be a comma-separated list of numbers"
        ) from exc


async def _execute(args) -> dict:
    env_data_dir = os.environ.get("LOCATION_DATA_DIR", "").strip()
    data_dir = (
        args.data_dir.expanduser()
        if args.data_dir is not None
        else Path(env_data_dir).expanduser()
        if env_data_dir
        else default_data_dir()
    )
    config = load_routing_config(args.config or DEFAULT_ROUTING_CONFIG)
    cache = RoutingCache(default_routing_cache_path(data_dir))
    budget = RoutingBudget(config.limits["max_requests"])
    async with ValhallaWalkingClient(config, cache=cache, budget=budget) as client:
        if args.command == "status":
            graph = await client.status()
            return {
                "available": True,
                "engine_version": graph.engine_version,
                "graph_version": graph.graph_version,
                "tileset_last_modified": client._routing_metadata(graph)["tileset_last_modified"],
                "profile_id": config.profile["profile_id"],
                "profile_hash": config.profile_hash,
                "source_edition": config.graph["source_edition"],
                "limits": dict(config.limits),
                "budget": {"max_requests": budget.max_requests, "used": budget.used},
            }
        if args.command == "route":
            origin = parse_coordinate(args.origin, config)
            destination = parse_coordinate(args.destination, config)
            return (await client.route(origin, destination)).to_dict()
        origin = parse_coordinate(args.origin, config)
        return (await client.reachable_area(origin, _parse_minutes(args.minutes))).to_dict()


def main(argv: list[str] | None = None) -> int:
    try:
        result = asyncio.run(_execute(parser().parse_args(argv)))
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except ConnectorError as exc:
        error = {"code": exc.code, "message": exc.message, "retryable": exc.retryable}
    except (OSError, ValueError) as exc:
        error = {"code": "LOCAL_OPERATION_FAILED", "message": str(exc), "retryable": False}
    except Exception as exc:
        error = {
            "code": "INTERNAL_ERROR",
            "message": f"Unexpected routing failure ({type(exc).__name__})",
            "retryable": False,
        }
    print(json.dumps({"error": error}, ensure_ascii=False), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
