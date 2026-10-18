"""Explicit, scriptable local ingestion and immutable-snapshot inspection."""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from app.application.service import DataService
from app.domain import ConnectorError, manifest_to_dict


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
