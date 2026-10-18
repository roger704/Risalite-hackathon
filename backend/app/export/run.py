"""Loading and normalization for persisted research runs."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.domain import ConnectorError, NotFoundError


@dataclass(frozen=True)
class RunBundle:
    run: dict[str, Any]
    cells: dict[str, Any] | None
    geojson: dict[str, Any] | None
    candidates: dict[str, Any] | None
    evidence: list[dict[str, Any]]
    trace: list[dict[str, Any]]


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConnectorError("RUN_INVALID", f"Could not read {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConnectorError("RUN_INVALID", f"{path.name} must contain a JSON object")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        entries = [json.loads(line) for line in lines if line.strip()]
    except (OSError, json.JSONDecodeError) as exc:
        raise ConnectorError("RUN_INVALID", f"Could not read {path.name}: {exc}") from exc
    if any(not isinstance(entry, dict) for entry in entries):
        raise ConnectorError("RUN_INVALID", f"{path.name} entries must be JSON objects")
    return entries


def normalize_evidence(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = []
    number = 0
    for entry in entries:
        evidence_id = entry.get("evidence_id") or entry.get("id")
        status = entry.get("status") or entry.get("verification_status") or "unverified"
        if status not in {"verified", "partial", "unsupported", "unverified"}:
            status = "unverified"
        if status != "unsupported":
            number += 1
        normalized.append(
            {
                **entry,
                "evidence_id": str(evidence_id) if evidence_id is not None else "",
                "url": entry.get("url")
                or entry.get("canonical_url")
                or entry.get("original_url")
                or "",
                "quote": entry.get("quote") or entry.get("passage") or entry.get("snippet") or "",
                "status": status,
                "n": number if status != "unsupported" else None,
            }
        )
    return normalized


def load_run(run_dir: Path) -> RunBundle:
    """Load one immutable run directory and normalize its evidence ledger."""
    run_dir = Path(run_dir)
    run_path = run_dir / "run.json"
    if not run_path.is_file():
        raise NotFoundError(f"Research run not found: {run_dir.name}", "RUN_NOT_FOUND")

    run = _read_json(run_path)
    cells_path = run_dir / "cells.json"
    candidates_path = run_dir / "candidates.json"
    if not cells_path.is_file() and not candidates_path.is_file():
        raise ConnectorError("RUN_INVALID", "Run must contain cells.json or candidates.json")

    cells = _read_json(cells_path) if cells_path.is_file() else None
    candidates = _read_json(candidates_path) if candidates_path.is_file() else None
    geojson_path = run_dir / "cells.geojson"
    geojson = _read_json(geojson_path) if geojson_path.is_file() else None
    evidence = normalize_evidence(_read_jsonl(run_dir / "evidence.jsonl"))
    trace = _read_jsonl(run_dir / "trace.jsonl")
    return RunBundle(run, cells, geojson, candidates, evidence, trace)
