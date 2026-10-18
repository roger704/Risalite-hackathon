"""Temporary fixture-backed run API; S1 replaces this implementation."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import re
import secrets
import time
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from threading import RLock
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.contracts.api import ApproveRequest, ResearchRequest
from app.domain import ConnectorError, NotFoundError
from app.evidence.ledger import entry_status
from app.export import EXPORT_FORMATS, Mix, RunBundle, normalize_evidence

router = APIRouter()
RUNS: dict[str, dict] = {}
RUNS_LOCK = RLock()
LIVE_TASKS: set[asyncio.Task] = set()
MAX_RUNS = 500
IDLE_TTL = 3600
SUPPORTED_AREAS = {"ortsteil:0202"}
FIXTURE_NOTICE = (
    "Demo fixtures only cover the Kreuzberg coffee question — results for this question/area "
    "will show that example, not a computed answer."
)
HYPOTHESIS_NOTICE = "Summary regenerated from the fixture for your approved hypotheses."

AREAS = [
    {"area_id": "bezirk:01", "level": "bezirk", "name": "Mitte", "bezirk": "Mitte"},
    {"area_id": "ortsteil:0101", "level": "ortsteil", "name": "Mitte", "bezirk": "Mitte"},
    {"area_id": "ortsteil:0102", "level": "ortsteil", "name": "Moabit", "bezirk": "Mitte"},
    {"area_id": "ortsteil:0103", "level": "ortsteil", "name": "Hansaviertel", "bezirk": "Mitte"},
    {"area_id": "ortsteil:0104", "level": "ortsteil", "name": "Tiergarten", "bezirk": "Mitte"},
    {"area_id": "ortsteil:0105", "level": "ortsteil", "name": "Gesundbrunnen", "bezirk": "Mitte"},
    {"area_id": "ortsteil:0106", "level": "ortsteil", "name": "Wedding", "bezirk": "Mitte"},
    {
        "area_id": "bezirk:02",
        "level": "bezirk",
        "name": "Friedrichshain-Kreuzberg",
        "bezirk": "Friedrichshain-Kreuzberg",
    },
    {
        "area_id": "ortsteil:0201",
        "level": "ortsteil",
        "name": "Friedrichshain",
        "bezirk": "Friedrichshain-Kreuzberg",
    },
    {
        "area_id": "ortsteil:0202",
        "level": "ortsteil",
        "name": "Kreuzberg",
        "bezirk": "Friedrichshain-Kreuzberg",
    },
    {"area_id": "bezirk:03", "level": "bezirk", "name": "Pankow", "bezirk": "Pankow"},
    {
        "area_id": "ortsteil:0301",
        "level": "ortsteil",
        "name": "Prenzlauer Berg",
        "bezirk": "Pankow",
    },
    {
        "area_id": "bezirk:04",
        "level": "bezirk",
        "name": "Charlottenburg-Wilmersdorf",
        "bezirk": "Charlottenburg-Wilmersdorf",
    },
    {
        "area_id": "ortsteil:0401",
        "level": "ortsteil",
        "name": "Charlottenburg",
        "bezirk": "Charlottenburg-Wilmersdorf",
    },
    {
        "area_id": "bezirk:05",
        "level": "bezirk",
        "name": "Spandau",
        "bezirk": "Spandau",
    },
    {
        "area_id": "bezirk:06",
        "level": "bezirk",
        "name": "Steglitz-Zehlendorf",
        "bezirk": "Steglitz-Zehlendorf",
    },
    {
        "area_id": "bezirk:07",
        "level": "bezirk",
        "name": "Tempelhof-Schöneberg",
        "bezirk": "Tempelhof-Schöneberg",
    },
    {"area_id": "bezirk:08", "level": "bezirk", "name": "Neukölln", "bezirk": "Neukölln"},
    {"area_id": "ortsteil:0801", "level": "ortsteil", "name": "Neukölln", "bezirk": "Neukölln"},
    {
        "area_id": "bezirk:09",
        "level": "bezirk",
        "name": "Treptow-Köpenick",
        "bezirk": "Treptow-Köpenick",
    },
    {
        "area_id": "bezirk:10",
        "level": "bezirk",
        "name": "Marzahn-Hellersdorf",
        "bezirk": "Marzahn-Hellersdorf",
    },
    {"area_id": "bezirk:11", "level": "bezirk", "name": "Lichtenberg", "bezirk": "Lichtenberg"},
    {
        "area_id": "bezirk:12",
        "level": "bezirk",
        "name": "Reinickendorf",
        "bezirk": "Reinickendorf",
    },
]
PARENTS = {
    "ortsteil:0101": "bezirk:01",
    "ortsteil:0102": "bezirk:01",
    "ortsteil:0103": "bezirk:01",
    "ortsteil:0104": "bezirk:01",
    "ortsteil:0105": "bezirk:01",
    "ortsteil:0106": "bezirk:01",
    "ortsteil:0201": "bezirk:02",
    "ortsteil:0202": "bezirk:02",
    "ortsteil:0301": "bezirk:03",
    "ortsteil:0401": "bezirk:04",
    "ortsteil:0801": "bezirk:08",
}
_BY_ID = {area["area_id"]: area for area in AREAS}


class ClarifyRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    answer: str = Field(min_length=1, max_length=500)


def _fixture_dir() -> Path:
    frontend = os.environ.get("RISALITE_FRONTEND_DIR")
    if frontend:
        return Path(frontend).expanduser() / "fixtures"
    return Path(__file__).resolve().parents[3] / "frontend" / "fixtures"


def _read_json(name: str) -> dict:
    return json.loads((_fixture_dir() / name).read_text(encoding="utf-8"))


def _read_jsonl(name: str) -> list[dict]:
    path = _fixture_dir() / name
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _approved_hypothesis_report(hypotheses: list[dict], evidence: list[dict]) -> str:
    if not hypotheses:
        return "No approved hypotheses are available in the demo fixture."

    evidence_numbers = {entry["evidence_id"]: i for i, entry in enumerate(evidence, 1)}
    summary = (
        f"Of the approved hypotheses, **{hypotheses[0]['name']}** scores highest "
        f"({hypotheses[0]['score']:.1f}/10)"
    )
    summary += "".join(
        f", followed by {hypothesis['name']} ({hypothesis['score']:.1f}/10)"
        for hypothesis in hypotheses[1:]
    )
    statements = []
    for hypothesis in hypotheses:
        claims = hypothesis.get("for", [])
        claim = claims[0] if claims else {}
        claim_text = claim.get("text") or "No supporting claim is available in the fixture"
        evidence_id = claim.get("evidence_id")
        citation = ""
        if evidence_id:
            citation = (
                f" [{evidence_numbers[evidence_id]}]"
                if evidence_id in evidence_numbers
                else f" [{evidence_id}]"
            )
        statements.append(f"{hypothesis['name']}: {claim_text}{citation}.")
    return f"{summary}.\n\n" + "\n\n".join(statements)


def _area_matches(text: str) -> list[dict]:
    lowered = text.casefold()
    matches = []
    for area in AREAS:
        name = area["name"].casefold()
        if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", lowered):
            matches.append(area)
    ids = {area["area_id"] for area in matches}
    for area in list(matches):
        parent = PARENTS.get(area["area_id"])
        if parent and parent not in ids:
            matches.append(_BY_ID[parent])
            ids.add(parent)
    matches.sort(key=lambda area: (0 if area["level"] == "ortsteil" else 1, area["area_id"]))
    return matches


def _shape(question: str) -> str:
    lowered = question.casefold()
    address = re.search(
        r"\b[\wäöüß-]+(?:\s+[\wäöüß-]+){0,3}\s+"
        r"(?:straße|strasse|str\.?|platz|damm|allee|ufer|weg|ring)\s+\d+[a-z]?\b",
        lowered,
    )
    if address or re.search(r"\b(storefront|this space)\b", lowered):
        return "point"
    if re.search(r"\b(kitas?|kindergarten)\b", lowered):
        return "gap"
    return "location"


def _plan(shape: str, matches: list[dict]) -> dict:
    plan_response = _read_json("plan_point.json" if shape == "point" else "plan_location.json")
    plan = copy.deepcopy(plan_response["plan"])
    plan["sub_questions"] = plan.get("sub_questions", [])[:3]
    if shape != "point":
        selected = next((area for area in matches if area["level"] == "ortsteil"), matches[0])
        plan["area"] = {
            "area_id": selected["area_id"],
            "level": selected["level"],
            "name": selected["name"],
            "plr_count": plan.pop("area_cells", 19),
        }
        plan["area_candidates"] = [
            {key: area[key] for key in ("area_id", "level", "name", "bezirk")} for area in matches
        ]
        if selected["area_id"] not in {area["area_id"] for area in plan["area_candidates"]}:
            plan["area_candidates"].append(selected)
        if shape == "gap" or selected["area_id"] not in SUPPORTED_AREAS:
            plan["note"] = FIXTURE_NOTICE
        else:
            plan.pop("note", None)
    return plan


def _response(run: dict) -> dict:
    return {
        "run_id": run["run_id"],
        "status": run["status"],
        "clarify_question": run.get("clarify_question"),
        "plan": run.get("plan"),
        "mode": run.get("mode", "fixtures"),
        "question": run["question"],
        "role": run["role"],
        "profile": run["profile"],
        "shape": run["shape"],
        "classification": run["classification"],
    }


def _make_room_for_run() -> bool:
    while len(RUNS) >= MAX_RUNS:
        oldest_terminal = min(
            (run_id for run_id, run in RUNS.items() if run["status"] in {"done", "failed"}),
            key=lambda run_id: RUNS[run_id].get("created_at", 0),
            default=None,
        )
        if oldest_terminal is not None:
            RUNS.pop(oldest_terminal)
            continue

        now = time.monotonic()
        oldest_idle = min(
            (
                run_id
                for run_id, run in RUNS.items()
                if run["status"] in {"needs_clarification", "awaiting_approval"}
                and now - run.get("created_at", 0) > IDLE_TTL
            ),
            key=lambda run_id: RUNS[run_id].get("created_at", 0),
            default=None,
        )
        if oldest_idle is not None:
            RUNS.pop(oldest_idle)
            continue

        oldest_abandoned = min(
            (
                run_id
                for run_id, run in RUNS.items()
                if run["status"] in {"approved", "running"}
                and isinstance(run.get("approved_at"), (int, float))
                and now - run["approved_at"] > IDLE_TTL
            ),
            key=lambda run_id: RUNS[run_id]["approved_at"],
            default=None,
        )
        if oldest_abandoned is None:
            return False
        RUNS.pop(oldest_abandoned)
    return True


def _store_run(run: dict) -> bool:
    with RUNS_LOCK:
        if not _make_room_for_run():
            return False
        RUNS[run["run_id"]] = run
        return True


def _run_locked(function):
    if asyncio.iscoroutinefunction(function):

        @wraps(function)
        async def async_wrapper(*args, **kwargs):
            with RUNS_LOCK:
                return await function(*args, **kwargs)

        return async_wrapper

    @wraps(function)
    def wrapper(*args, **kwargs):
        with RUNS_LOCK:
            return function(*args, **kwargs)

    return wrapper


def _live_enabled() -> bool:
    return (
        os.environ.get("RISALITE_LIVE") == "1"
        and bool(os.environ.get("NEBIUS_API_KEY"))
        and bool(os.environ.get("TAVILY_API_KEY"))
    )


async def _execute_live_run(run: dict, run_dir: Path) -> None:
    gateway = tavily = verify_tavily = checker = None
    try:
        from app.connectors.nebius import NebiusConfig, NebiusGateway
        from app.connectors.tavily import TavilyClient, TavilyConfig
        from app.domain.research import ResearchBudget
        from app.evidence.factcheck import FactChecker
        from app.research.live import run_live

        budget = ResearchBudget(6, 6, 14, 200_000)
        verify_budget = ResearchBudget(0, 100, 0, 200_000)
        gateway = NebiusGateway(
            NebiusConfig.from_env(
                model=os.environ.get("NEBIUS_MODEL", "nvidia/nemotron-3-super-120b-a12b")
            ),
            budget,
        )
        tavily = TavilyClient(TavilyConfig.from_env(), budget)
        verify_tavily = TavilyClient(TavilyConfig.from_env(), verify_budget)
        checker = FactChecker(research=verify_tavily)
        result = await run_live(
            run_dir,
            run_id=run["run_id"],
            question=run["question"],
            area_name=((run.get("plan") or {}).get("area") or {}).get("name"),
            role=run.get("role"),
            gateway=gateway,
            tavily=tavily,
            checker=checker,
        )
    except Exception as exc:
        message = str(exc) or type(exc).__name__
        for key in ("NEBIUS_API_KEY", "TAVILY_API_KEY"):
            secret = os.environ.get(key)
            if secret:
                message = message.replace(secret, "[redacted]")
        result = {"run_id": run["run_id"], "status": "failed", "error": message[:1000]}
    finally:
        if checker is not None:
            try:
                await checker.aclose()
            except Exception:
                pass
        if verify_tavily is not None:
            try:
                await verify_tavily.aclose()
            except Exception:
                pass
        if tavily is not None:
            try:
                await tavily.aclose()
            except Exception:
                pass
        if gateway is not None:
            try:
                await gateway.aclose()
            except Exception:
                pass
    run_dir.mkdir(parents=True, exist_ok=True)
    marker_path = run_dir / "result.json"
    if not marker_path.exists():
        from app.evidence.trace import TraceWriter

        trace = TraceWriter(run_dir, run["run_id"])
        trace.emit("error", "orchestrator", result.get("error", "Live run failed."))
        trace.emit(
            "status",
            "orchestrator",
            "Research run failed.",
            detail={"summary": "error", "status": "failed"},
        )
        marker_path.write_text(json.dumps(result, ensure_ascii=False) + "\n", encoding="utf-8")
    with RUNS_LOCK:
        if run["run_id"] in RUNS:
            run["status"] = result.get("status", "failed")
            run["live_error"] = result.get("error")


def _path_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _live_trace(run: dict, run_dir: Path) -> list[dict]:
    approved = {
        "run_id": run["run_id"],
        "seq": 0,
        "ts": datetime.now(UTC).isoformat(),
        "type": "approved",
        "agent": "orchestrator",
        "why": "The user approved the research plan.",
        "detail": run.get("approval", {}),
        "model": None,
        "tokens_in": None,
        "tokens_out": None,
        "seconds": 0.0,
    }
    events = [approved]
    for item in _path_jsonl(run_dir / "trace.jsonl"):
        events.append({**item, "seq": int(item.get("seq", len(events) - 1)) + 1})
    return events


def _apply_layer_approvals(cells: dict, approved_layers: list[dict]) -> dict:
    for layer in cells["layers"]:
        layer["enabled"] = True
    for approved_layer in approved_layers:
        for layer in cells["layers"]:
            if layer["id"] == approved_layer["id"]:
                layer["enabled"] = approved_layer["enabled"]
                layer["weight_default"] = approved_layer["weight"]
    return cells


def _live_result_payload(run: dict, run_dir: Path) -> dict | None:
    marker_path = run_dir / "result.json"
    if not marker_path.exists():
        return None
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    status = marker.get("status", "failed")
    shape = run["shape"]
    area_id = ((run.get("plan") or {}).get("area") or {}).get("area_id")
    supported = area_id in SUPPORTED_AREAS
    result_doc = _read_json(f"result_{'point' if shape == 'point' else 'location'}.json")
    latest_entries = {}
    for entry in _path_jsonl(run_dir / "evidence.jsonl"):
        latest_entries[entry.get("evidence_id")] = entry
    web_entries = list(latest_entries.values())
    citable = [entry for entry in web_entries if entry_status(entry) != "unsupported"]
    unsupported = [entry for entry in web_entries if entry_status(entry) == "unsupported"]
    fixture_evidence = (
        [
            entry
            for entry in _read_jsonl("evidence.jsonl")
            if entry.get("method") not in {"web", "tavily", "tavily-search", "tavily-extract"}
            and "example-" not in entry_url_for_fixture(entry)
        ]
        if supported
        else []
    )
    evidence = citable + unsupported + fixture_evidence
    evidence_ids = {entry.get("evidence_id") for entry in evidence}
    result_doc.pop("synthetic", None)
    cells = (
        _apply_layer_approvals(
            _read_json("cells.json"),
            run.get("approval", {}).get("layers", []),
        )
        if supported
        else None
    )
    if cells is not None:
        for cell in cells.get("cells", []):
            for key in ("modifiers", "risks"):
                cell[key] = [
                    item
                    for item in cell.get(key, [])
                    if isinstance(item, dict) and item.get("evidence_id") in evidence_ids
                ]
    trace = _live_trace(run, run_dir)
    findings = _path_jsonl(run_dir / "findings.jsonl")
    result_doc.update(
        {
            "run_id": run["run_id"],
            "status": status,
            "report_md": (run_dir / "report.md").read_text(encoding="utf-8")
            if (run_dir / "report.md").exists()
            else "",
            "evidence": evidence,
            "findings": findings,
            "trace": trace,
            "metrics": json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
            if (run_dir / "metrics.json").exists()
            else {},
            "cells": cells,
            "cells_geojson": _read_json("cells.geojson") if supported else None,
            "candidates": None,
            "fixture_notice": (
                "The heatmap layers are demo fixtures; the web research, citations and trace are live."
            ),
            "question": run["question"],
            "role": run.get("role"),
            "shape": shape,
            "plan": run.get("plan"),
            "mode": "live",
        }
    )
    if status == "failed":
        result_doc["error"] = marker.get("error") or run.get("live_error") or "Research run failed."
    return result_doc


def entry_url_for_fixture(entry: dict) -> str:
    return entry.get("url") or entry.get("source_url") or ""


def _get_run(run_id: str, authorization: str | None) -> dict:
    run = RUNS.get(run_id)
    token = authorization.removeprefix("Bearer ") if authorization else ""
    if not run or not secrets.compare_digest(
        run["token_hash"], hashlib.sha256(token.encode()).hexdigest()
    ):
        raise HTTPException(status_code=404, detail={"error": "Run not found"})
    return run


@router.post("/research")
def research(body: ResearchRequest):
    question = body.question.strip()
    if not question:
        raise HTTPException(status_code=422, detail="question is required")
    with RUNS_LOCK:
        if not _make_room_for_run():
            return JSONResponse(
                status_code=503,
                content={"detail": "fixture run store is full"},
            )
        run_id = str(uuid4())
        token = secrets.token_urlsafe(24)
        shape = _shape(question)
        matches = _area_matches(question)
        run = {
            "run_id": run_id,
            "token_hash": hashlib.sha256(token.encode()).hexdigest(),
            "question": question,
            "role": body.role,
            "profile": body.profile,
            "mode": "live" if _live_enabled() else "fixtures",
            "shape": shape,
            "area_matches": matches,
            "classification": {
                "shape": shape,
                "why": "Fixture classifier selected a point, gap, or location research path.",
                "area": matches[0]["name"] if matches else None,
            },
            "status": "needs_clarification"
            if shape != "point" and not matches
            else "awaiting_approval",
            "clarify_question": "Which Berlin district or neighbourhood should I research?"
            if shape != "point" and not matches
            else None,
            "plan": _plan(shape, matches) if shape == "point" or matches else None,
            "created_at": time.monotonic(),
            "created_at_utc": datetime.now(UTC).isoformat(),
        }
        if not _store_run(run):
            return JSONResponse(
                status_code=503,
                content={"detail": "fixture run store is full"},
            )
        return {**_response(run), "run_token": token}


@router.post("/runs/{run_id}/clarify")
@_run_locked
def clarify(
    run_id: str,
    payload: dict,
    authorization: str | None = Header(default=None),
):
    run = _get_run(run_id, authorization)
    try:
        body = ClarifyRequest.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="A non-empty answer is required") from exc
    if run["status"] != "needs_clarification":
        raise HTTPException(status_code=409, detail="Run is not awaiting clarification")
    run["created_at"] = time.monotonic()
    run["question"] = f"{run['question']} {body.answer}"
    run["area_matches"] = _area_matches(run["question"])
    if run["shape"] == "point" or run["area_matches"]:
        run["status"] = "awaiting_approval"
        run["clarify_question"] = None
        run["plan"] = _plan(run["shape"], run["area_matches"])
    else:
        run["status"] = "needs_clarification"
        run["clarify_question"] = "Which Berlin district or neighbourhood should I research?"
    token = authorization.removeprefix("Bearer ") if authorization else ""
    return {**_response(run), "run_token": token}


@router.get("/runs/{run_id}/areas")
def search_areas(
    run_id: str,
    q: str = Query(default=""),
    authorization: str | None = Header(default=None),
):
    _get_run(run_id, authorization)
    value = q.casefold()
    return [area for area in AREAS if value in area["name"].casefold()]


@router.post("/runs/{run_id}/approve")
@_run_locked
async def approve(
    run_id: str,
    payload: dict,
    http_request: Request,
    authorization: str | None = Header(default=None),
):
    run = _get_run(run_id, authorization)
    if run["status"] != "awaiting_approval":
        raise HTTPException(status_code=409, detail="Run is not awaiting approval")
    try:
        approval_request = ApproveRequest.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="Invalid approval payload") from exc
    body = approval_request.model_dump(exclude_none=True)
    if len(body.get("subquestions", [])) > 3:
        raise HTTPException(status_code=422, detail="At most three subquestions may be approved")
    plan = run["plan"] or {}
    layer_ids = {layer["id"] for layer in plan.get("layers", [])}
    for layer in body.get("layers", []):
        if layer.get("id") not in layer_ids:
            raise HTTPException(status_code=422, detail=f"Unknown layer id: {layer.get('id')}")
        weight = layer.get("weight")
        if not isinstance(weight, (int, float)) or not 0 <= weight <= 1:
            raise HTTPException(status_code=422, detail="Layer weights must be between 0 and 1")
    area_id = body.get("area_id")
    if area_id is not None and area_id not in _BY_ID:
        raise HTTPException(status_code=422, detail=f"Unknown area_id: {area_id}")
    if run["shape"] == "point" and "hypothesis_ids" in payload:
        hypothesis_ids = body.get("hypothesis_ids")
        planned_ids = {hypothesis["id"] for hypothesis in plan.get("hypotheses", [])}
        if not hypothesis_ids or any(
            hypothesis_id not in planned_ids for hypothesis_id in hypothesis_ids
        ):
            raise HTTPException(
                status_code=422,
                detail="Approved hypothesis_ids must be non-empty and present in the plan",
            )
    run["created_at"] = time.monotonic()
    if area_id and plan.get("area") and area_id != plan["area"]["area_id"]:
        selected = _BY_ID[area_id]
        plan["area"] = {
            "area_id": selected["area_id"],
            "level": selected["level"],
            "name": selected["name"],
            "plr_count": plan["area"].get("plr_count", 19),
        }
        plan["area_candidates"] = [
            {key: area[key] for key in ("area_id", "level", "name", "bezirk")}
            for area in run["area_matches"]
        ]
        if area_id not in {area["area_id"] for area in plan["area_candidates"]}:
            plan["area_candidates"].append(selected)
        if run["shape"] == "gap" or area_id not in SUPPORTED_AREAS:
            plan["note"] = FIXTURE_NOTICE
        else:
            plan.pop("note", None)
        run["status"] = "awaiting_approval"
        run["plan"] = plan
        return {**_response(run), "status": "awaiting_approval", "plan": plan}

    run["approval"] = body
    run["status"] = "approved"
    run["approved_at"] = time.monotonic()
    if run.get("mode") == "live":
        run["status"] = "running"
        area_id = area_id or (plan.get("area") or {}).get("area_id")
        run["live_area_id"] = area_id
        run_dir = (Path(http_request.app.state.runs_dir) / run_id).resolve()
        run["live_run_dir"] = str(run_dir)
        task = asyncio.create_task(_execute_live_run(run, run_dir))
        LIVE_TASKS.add(task)
        task.add_done_callback(LIVE_TASKS.discard)
    return {"status": "approved"}


def _event_type(old: dict) -> str:
    kind = old.get("type", "status")
    allowed = {
        "status",
        "classify",
        "clarify",
        "area",
        "plan",
        "approved",
        "layer",
        "score",
        "search",
        "extract",
        "finding_accepted",
        "finding_rejected",
        "model_call",
        "tool_error",
        "budget_exhausted",
        "merge",
        "skeptic_risk",
        "fact_check",
        "review",
        "rescore",
        "write",
        "metrics",
        "error",
    }
    aliases = {
        "done": "status",
        "reject": "finding_rejected",
        "verify": "fact_check",
        "replan": "plan",
        "tool:wfs": "layer",
    }
    value = aliases.get(kind, kind)
    return value if value in allowed else "status"


def _events(run: dict) -> list[dict]:
    raw = _read_jsonl("trace_point.jsonl" if run["shape"] == "point" else "trace.jsonl")
    result = []
    for item in raw:
        event_type = _event_type(item)
        detail = item.get("detail") or item.get("text") or {}
        if event_type == "approved":
            detail = run.get("approval", detail)
        result.append(
            {
                "run_id": run["run_id"],
                "seq": len(result),
                "ts": item.get("ts") or datetime.now(UTC).isoformat(),
                "type": event_type,
                "agent": item.get("agent") or item.get("role", "system").lower().replace(" ", ":"),
                "why": item.get("why", ""),
                "detail": detail,
                "model": item.get("model"),
                "tokens_in": item.get("tokens_in", item.get("tokens", 0)),
                "tokens_out": item.get("tokens_out", 0),
                "seconds": item.get("seconds", 0.0),
                **({"synthetic": True} if item.get("synthetic", True) else {}),
            }
        )
    approval_event = {
        "run_id": run["run_id"],
        "seq": 0,
        "ts": datetime.now(UTC).isoformat(),
        "type": "approved",
        "agent": "orchestrator",
        "why": "The user approved the research plan.",
        "detail": run.get("approval", {}),
        "model": None,
        "tokens_in": None,
        "tokens_out": None,
        "seconds": 0.0,
        "synthetic": True,
    }
    if result:
        result.insert(0, approval_event)
    else:
        result = [approval_event]
    for seq, event in enumerate(result):
        event["seq"] = seq
        event["run_id"] = run["run_id"]
        if event["type"] == "approved":
            event["detail"] = run.get("approval", {})
    return result


@router.get("/runs/{run_id}/events")
async def events(
    run_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
):
    run = _get_run(run_id, authorization)
    after = int(last_event_id) if last_event_id and last_event_id.isdigit() else -1
    if run.get("mode") == "live":
        if run["status"] not in {"approved", "running", "done", "failed"}:
            raise HTTPException(status_code=409, detail="Run has not been approved")
        run_dir = Path(run["live_run_dir"])

        async def live_stream():
            last_sent = after
            while True:
                trace = _live_trace(run, run_dir)
                for event in trace:
                    if event["seq"] <= last_sent:
                        continue
                    yield f"id: {event['seq']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                    last_sent = event["seq"]
                terminal = any(
                    event.get("type") == "status"
                    and (
                        (event.get("detail") or {}).get("summary") in {"done", "error"}
                        or (event.get("detail") or {}).get("status") == "failed"
                    )
                    for event in trace
                )
                if terminal:
                    break
                await asyncio.sleep(0.5)

        return StreamingResponse(live_stream(), media_type="text/event-stream")
    if run["status"] not in {"approved", "running"}:
        raise HTTPException(status_code=409, detail="Run has not been approved")
    trace = _events(run)
    delay = max(0.0, float(os.environ.get("RISALITE_REPLAY_DELAY", "0.25")))

    async def stream():
        for event in trace:
            if event["seq"] <= after:
                continue
            if delay:
                await asyncio.sleep(delay)
            yield f"id: {event['seq']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@router.get("/runs/{run_id}/result")
def result(run_id: str, authorization: str | None = Header(default=None)):
    run = _get_run(run_id, authorization)
    if run.get("mode") == "live":
        if run["status"] not in {"approved", "running", "done", "failed"}:
            return JSONResponse(status_code=202, content={"status": run["status"]})
        payload = _live_result_payload(run, Path(run["live_run_dir"]))
        if payload is None:
            return JSONResponse(status_code=202, content={"status": "running"})
        run["status"] = payload["status"]
        run["result"] = payload
        return payload
    if run["status"] == "done":
        return run["result"]
    if run["status"] not in {"approved", "running"}:
        return JSONResponse(status_code=202, content={"status": run["status"]})
    trace = _events(run)
    delay = max(0.0, float(os.environ.get("RISALITE_REPLAY_DELAY", "0.25")))
    if time.monotonic() - run.get("approved_at", time.monotonic()) < len(trace) * delay:
        return JSONResponse(status_code=202, content={"status": "running"})

    result_doc = _result_payload(run)
    run["status"] = "done"
    run["result"] = result_doc
    return result_doc


def _result_payload(run: dict) -> dict:
    trace = _events(run)
    shape = run["shape"]
    area = (run.get("plan") or {}).get("area") or {}
    area_id = area.get("area_id")
    fixture_notice = (
        FIXTURE_NOTICE
        if shape == "gap" or (shape == "location" and area_id not in SUPPORTED_AREAS)
        else None
    )
    result_doc = _read_json(f"result_{'point' if shape == 'point' else 'location'}.json")
    cells = _apply_layer_approvals(
        _read_json("cells.json"),
        run.get("approval", {}).get("layers", []),
    )
    findings_name = "findings_point.jsonl" if shape == "point" else "findings.jsonl"
    findings_path = _fixture_dir() / findings_name
    findings = _read_jsonl(findings_name) if findings_path.exists() else []
    evidence = _read_jsonl("evidence.jsonl")
    candidates = _read_json("candidates.json") if shape == "point" else None
    report_md = result_doc.get("report_md", "")
    approval = run.get("approval", {})
    if candidates is not None and "hypothesis_ids" in approval:
        original_hypotheses = candidates["hypotheses"]
        approved_ids = set(approval["hypothesis_ids"])
        candidates["hypotheses"] = [
            hypothesis for hypothesis in original_hypotheses if hypothesis["id"] in approved_ids
        ]
        if len(candidates["hypotheses"]) < len(original_hypotheses):
            report_md = _approved_hypothesis_report(candidates["hypotheses"], evidence)
            fixture_notice = HYPOTHESIS_NOTICE
    result_doc.update(
        {
            "run_id": run["run_id"],
            "status": "done",
            "report_md": report_md,
            "cells": cells if shape != "point" else None,
            "evidence": evidence,
            "findings": findings,
            "metrics": _read_json("metrics_point.json" if shape == "point" else "metrics.json")
            if (_fixture_dir() / "metrics.json").exists()
            else {},
            "question": run["question"],
            "role": run.get("role") or result_doc.get("role"),
            "shape": shape,
            "plan": run["plan"],
            "fixture_notice": fixture_notice,
            "cells_geojson": _read_json("cells.geojson"),
            "candidates": candidates,
            "trace": trace,
        }
    )
    return result_doc


def _renumber_fixture_citations(report_md: str, evidence: list[dict]) -> str:
    def renumber(match: re.Match) -> str:
        index = int(match.group(1)) - 1
        if index < 0 or index >= len(evidence):
            return match.group(0)
        number = evidence[index].get("n")
        return f"[{number}]" if number is not None else ""

    return re.sub(r"\[(\d+)\]", renumber, report_md)


def _export_cells(cells: dict | None) -> dict | None:
    if cells is None:
        return None
    return {
        **cells,
        "cells": [
            {
                **cell,
                "unknowns": [
                    unknown if isinstance(unknown, dict) else {"text": unknown}
                    for unknown in cell.get("unknowns", [])
                ],
            }
            for cell in cells.get("cells", [])
        ],
    }


@router.get("/runs/{run_id}/export/{fmt}")
def export_run(
    request: Request,
    run_id: str,
    fmt: str,
    authorization: str | None = Header(default=None),
):
    run = _get_run(run_id, authorization)
    if fmt not in EXPORT_FORMATS:
        raise NotFoundError("Unknown export format", "UNKNOWN_FORMAT")
    unknown = set(request.query_params) - {"layers", "mode"}
    if unknown:
        raise ConnectorError(
            "UNSUPPORTED_FILTER", "Unsupported query filters: " + ", ".join(sorted(unknown))
        )
    mode = request.query_params.get("mode")
    if mode is not None and mode not in {"present", "future"}:
        raise ConnectorError("INVALID_REQUEST", "mode must be present or future")
    layers_value = request.query_params.get("layers")
    layers = (
        frozenset(layer.strip() for layer in layers_value.split(",") if layer.strip())
        if layers_value is not None
        else None
    )

    if run.get("mode") == "live":
        if run["status"] not in {"approved", "running", "done", "failed"}:
            raise HTTPException(status_code=409, detail="run not finished")
        payload = _live_result_payload(run, Path(run["live_run_dir"]))
        if payload is None:
            raise HTTPException(status_code=409, detail="run not finished")
        run["status"] = payload["status"]
        run["result"] = payload
    elif run["status"] == "done":
        payload = run["result"]
    else:
        if run["status"] not in {"approved", "running"}:
            raise HTTPException(status_code=409, detail="run not finished")
        trace = _events(run)
        delay = max(0.0, float(os.environ.get("RISALITE_REPLAY_DELAY", "0.25")))
        if time.monotonic() - run.get("approved_at", time.monotonic()) < len(trace) * delay:
            raise HTTPException(status_code=409, detail="run not finished")
        payload = _result_payload(run)
        run["status"] = "done"
        run["result"] = payload

    metrics = payload.get("metrics") or {}
    evidence = normalize_evidence(payload.get("evidence") or [])
    summary = payload.get("report_md", "")
    if run["shape"] == "point":
        summary = _renumber_fixture_citations(summary, evidence)
    if payload.get("fixture_notice") and run.get("mode") != "live":
        summary = (
            f"> **Demo fixture, not a computed answer:** {payload['fixture_notice']}\n\n" + summary
        )
    plan = dict(run.get("plan") or {})
    approved_subquestions = (run.get("approval") or {}).get("subquestions")
    plan["subquestions"] = [
        {
            "id": subquestion["id"],
            "text": subquestion["text"],
            "approved": approved_subquestions is None or subquestion["id"] in approved_subquestions,
        }
        for subquestion in plan.get("sub_questions", [])
    ]
    trace = payload.get("trace") or _events(run)
    trace = [
        {
            **event,
            "role": event.get("role") or event.get("agent"),
            "tokens": (event.get("tokens_in") or 0) + (event.get("tokens_out") or 0),
        }
        for event in trace
    ]
    bundle = RunBundle(
        run={
            "run_id": run["run_id"],
            "question": run["question"],
            "role": run.get("role"),
            "shape": run["shape"],
            "created_at": run.get("created_at_utc") or datetime.now(UTC).isoformat(),
            "plan": plan,
            "summary": summary,
            **(
                {
                    "report_md": payload.get("report_md", ""),
                    "findings": payload.get("findings") or [],
                }
                if run.get("mode") == "live"
                else {}
            ),
            "models": {
                role: value.get("model")
                for role, value in metrics.items()
                if isinstance(value, dict) and value.get("model")
            },
        },
        cells=_export_cells(payload.get("cells")),
        geojson=payload.get("cells_geojson"),
        candidates=payload.get("candidates"),
        evidence=evidence,
        trace=trace,
    )
    mix = Mix(layers_on=layers, mode=mode)
    media_type, renderer = EXPORT_FORMATS[fmt]
    if fmt.startswith("report."):
        body = renderer(bundle, mix=mix if run["shape"] != "point" else None)
    else:
        body = renderer(bundle)
    if fmt == "sources.csv":
        body = "\ufeff" + body
    disposition = "inline" if fmt == "report.html" else "attachment"
    headers = {"Content-Disposition": f'{disposition}; filename="risalite-{run_id}-{fmt}"'}
    return Response(content=body, media_type=media_type, headers=headers)
