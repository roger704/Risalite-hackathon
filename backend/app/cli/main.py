"""Explicit, scriptable local ingestion and immutable-snapshot inspection."""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from time import perf_counter

from app.application.service import DataService
from app.domain import ConnectorError, manifest_to_dict
from app.export import EXPORT_FORMATS, Mix, load_run


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description="Acquire and inspect location dataset snapshots")
    command.add_argument("--data-dir", type=Path, default=None)
    command.add_argument("--config-dir", type=Path, default=None)
    subcommands = command.add_subparsers(dest="command", required=True)
    subcommands.add_parser("datasets", help="List dataset configuration and local snapshots")

    ingest = subcommands.add_parser(
        "ingest", help="Import a reviewed provider resource or local file"
    )
    ingest.add_argument("dataset")
    ingest.add_argument("--file", type=Path)
    ingest.add_argument("--lor-snapshot", help="Pin compatible published LOR boundaries")

    manifest = subcommands.add_parser("manifest", help="Read an immutable snapshot manifest")
    manifest.add_argument("dataset")
    manifest.add_argument("snapshot")

    records = subcommands.add_parser("records", help="Query an explicitly selected local snapshot")
    records.add_argument("dataset")
    records.add_argument("--snapshot", required=True)
    records.add_argument("--area-code")
    records.add_argument("--metric-code")
    records.add_argument("--limit", type=int, default=100)
    records.add_argument("--cursor")

    catchment = subcommands.add_parser(
        "catchment",
        help="Aggregate grid observations for a circular catchment from a pinned snapshot",
    )
    catchment.add_argument("dataset")
    catchment.add_argument("--snapshot", required=True)
    catchment.add_argument("--lon", type=float, required=True)
    catchment.add_argument("--lat", type=float, required=True)
    catchment.add_argument("--radius-m", type=float, required=True)

    export = subcommands.add_parser("export", help="Export a persisted research run")
    export.add_argument("run_dir", type=Path)
    export.add_argument("--format", choices=("md", "html", "csv", "bib"), required=True)
    export.add_argument("--layers", help="Comma-separated layer IDs")
    export.add_argument("--mode", choices=("present", "future"))
    export.add_argument("--share-url")
    export.add_argument("-o", "--output", type=Path)

    subcommands.add_parser(
        "precompute-berlin-reference",
        help="Precompute the coffee_v2 Berlin-wide scoring reference cache",
    )
    return command


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "export":
            bundle = load_run(args.run_dir)
            fmt = {
                "md": "report.md",
                "html": "report.html",
                "csv": "sources.csv",
                "bib": "sources.bib",
            }[args.format]
            layers = (
                frozenset(layer.strip() for layer in args.layers.split(",") if layer.strip())
                if args.layers is not None
                else None
            )
            mix = Mix(layers_on=layers, mode=args.mode)
            _, renderer = EXPORT_FORMATS[fmt]
            if fmt.startswith("report."):
                result = renderer(bundle, mix=mix, share_url=args.share_url)
            else:
                result = renderer(bundle)
            if args.output:
                encoding = "utf-8-sig" if args.format == "csv" else "utf-8"
                args.output.write_text(result, encoding=encoding)
            else:
                print(result, end="")
            return 0

        configured_dir = os.environ.get("LOCATION_DATA_DIR", "").strip()
        default_dir = (
            Path(configured_dir).expanduser()
            if configured_dir
            else Path(__file__).resolve().parents[3] / "data"
        )
        service = DataService(data_dir=args.data_dir or default_dir, config_dir=args.config_dir)
        if args.command == "datasets":
            result = service.list_datasets()
        elif args.command == "manifest":
            result = service.manifest(args.dataset, args.snapshot)
        elif args.command == "records":
            if not 1 <= args.limit <= 1000:
                raise ConnectorError("INVALID_REQUEST", "limit must be between 1 and 1000")
            result = service.records(
                args.dataset,
                args.snapshot,
                area_code=args.area_code,
                metric_code=args.metric_code,
                limit=args.limit,
                cursor=args.cursor,
            )
        elif args.command == "catchment":
            from app.geo.zensus_catchment import snapshot_catchment

            result = snapshot_catchment(
                service,
                args.dataset,
                args.snapshot,
                args.lon,
                args.lat,
                args.radius_m,
            )
        elif args.command == "precompute-berlin-reference":
            from app.application.area_data import SummaryAreaData
            from app.scoring import v2

            port = SummaryAreaData.from_data_service(service)
            started = perf_counter()
            cells = port.berlin_reference(v2.load_template("coffee_v2"))
            result = {
                "template_id": "coffee_v2",
                "cells": len(cells),
                "elapsed_seconds": round(perf_counter() - started, 3),
                "cache_dir": str(port._reference_cache_dir),
            }
        else:
            result = manifest_to_dict(
                asyncio.run(
                    service.ingest(
                        args.dataset,
                        local_file=args.file,
                        parent_snapshot_id=args.lor_snapshot,
                    )
                )
            )
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except ConnectorError as exc:
        error = {"code": exc.code, "message": exc.message, "retryable": exc.retryable}
    except (OSError, ValueError) as exc:
        error = {"code": "LOCAL_OPERATION_FAILED", "message": str(exc), "retryable": False}
    except Exception as exc:
        error = {
            "code": "INTERNAL_ERROR",
            "message": f"Unexpected local operation failure ({type(exc).__name__})",
            "retryable": False,
        }
    print(json.dumps({"error": error}, ensure_ascii=False), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
