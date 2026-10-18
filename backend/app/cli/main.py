"""Explicit, scriptable local ingestion and immutable-snapshot inspection."""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from app.application.service import DataService
from app.domain import ConnectorError, manifest_to_dict
from app.infrastructure.storage import read_records


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

    cells = subcommands.add_parser("cells", help="Build scored PLR cells from local snapshots")
    cells.add_argument("--area")
    cells.add_argument("--template", default="coffee")
    cells.add_argument("--out", type=Path, required=True)
    cells.add_argument("--modifiers", type=Path)

    locate = subcommands.add_parser("locate", help="Locate an address and nearby competitors")
    locate.add_argument("address")
    return command


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
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
        elif args.command == "cells":
            from app.scoring.areas import resolve_area
            from app.scoring.build import build_cells, write_run
            from app.scoring.geometry import cells_geojson, load_plr_geometries
            from app.scoring.layers import compute_layers

            config_path = (
                Path(__file__).resolve().parents[2]
                / "config"
                / "scoring"
                / (f"{args.template}_v1.json")
            )
            config = json.loads(config_path.read_text(encoding="utf-8"))
            datasets = {item["dataset_id"]: item for item in service.list_datasets()}
            names_manifest = service.catalog.get(
                "berlin_lor_names", datasets["berlin_lor_names"]["latest_snapshot_id"]
            )
            names_records = read_records(service._snapshot_path(names_manifest))
            names = {
                item.area_code: {**item.attributes, "name": item.name}
                for item in names_records
                if getattr(item, "record_type", None) == "area"
            }
            area = resolve_area(args.area, names)
            lor_manifest = service.catalog.get(
                "berlin_lor", datasets["berlin_lor"]["latest_snapshot_id"]
            )
            geometries = load_plr_geometries(read_records(service._snapshot_path(lor_manifest)))
            subset = geometries[geometries["id"].isin(area.plr_ids)].copy()
            modifiers = (
                json.loads(args.modifiers.read_text(encoding="utf-8")) if args.modifiers else []
            )
            config["_geometry_source_url"] = service.configs["berlin_lor_names"].resource_url
            config["_geometry_snapshot_id"] = names_manifest.snapshot_id
            config["_geometry_geometry_snapshot_id"] = lor_manifest.snapshot_id
            config["_area_name"] = area.name
            layers = compute_layers(
                service, area.plr_ids, config, geometries=geometries, names=names
            )
            doc, evidence = build_cells(
                area,
                layers,
                None,
                modifiers,
                {},
                {},
                config=config,
                names=names,
                template=args.template,
            )
            geojson = cells_geojson(subset, names)
            write_run(args.out, doc, geojson, evidence)
            result = {
                "out": {
                    "cells": str(args.out / "cells.json"),
                    "geojson": str(args.out / "cells.geojson"),
                    "evidence": str(args.out / "evidence.jsonl"),
                },
                "cell_count": len(doc.cells),
                "excluded_count": len(doc.excluded_cells),
                "top3": [
                    {
                        "id": cell.id,
                        "name": cell.name,
                        "score_present": cell.score_present,
                        "raw_values": cell.raw_values,
                    }
                    for cell in doc.cells[:3]
                ],
            }
        elif args.command == "locate":
            from app.scoring.locate import locate as locate_address

            result = asyncio.run(locate_address(args.address, service=service)).model_dump(
                mode="json"
            )
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
