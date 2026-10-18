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
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.contracts.api import ApproveRequest, ResearchRequest

router = APIRouter()
RUNS: dict[str, dict] = {}

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

    answer: str = Field(min_length=1)


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
    if re.search(r"\b(kita|kindergarten)\b", lowered):
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
    return plan


def _response(run: dict) -> dict:
    return {
        "run_id": run["run_id"],
        "status": run["status"],
        "clarify_question": run.get("clarify_question"),
        "plan": run.get("plan"),
        "mode": "fixtures",
        "question": run["question"],
        "role": run["role"],
        "profile": run["profile"],
        "shape": run["shape"],
        "classification": run["classification"],
    }


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
    }
    RUNS[run_id] = run
    return {**_response(run), "run_token": token}


@router.post("/runs/{run_id}/clarify")
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
def approve(
    run_id: str,
    payload: dict,
    authorization: str | None = Header(default=None),
):
    run = _get_run(run_id, authorization)
    if run["status"] != "awaiting_approval":
        raise HTTPException(status_code=409, detail="Run is not awaiting approval")
    try:
        request = ApproveRequest.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="Invalid approval payload") from exc
    body = request.model_dump(exclude_none=True)
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
        plan["note"] = "fixture data covers Kreuzberg only; other areas use the same fixture data."
        run["status"] = "awaiting_approval"
        run["plan"] = plan
        return {**_response(run), "status": "awaiting_approval", "plan": plan}

    run["approval"] = body
    run["status"] = "approved"
    run["approved_at"] = time.monotonic()
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
    authorization: str | None = Header(default=None),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
):
    run = _get_run(run_id, authorization)
    if run["status"] not in {"approved", "running"}:
        raise HTTPException(status_code=409, detail="Run has not been approved")
    trace = _events(run)
    after = int(last_event_id) if last_event_id and last_event_id.isdigit() else -1
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
    if run["status"] == "done":
        return run["result"]
    if run["status"] not in {"approved", "running"}:
        return JSONResponse(status_code=202, content={"status": run["status"]})
    trace = _events(run)
    delay = max(0.0, float(os.environ.get("RISALITE_REPLAY_DELAY", "0.25")))
    if time.monotonic() - run.get("approved_at", time.monotonic()) < len(trace) * delay:
        return JSONResponse(status_code=202, content={"status": "running"})
    shape = run["shape"]
    result_doc = _read_json(f"result_{'point' if shape == 'point' else 'location'}.json")
    cells = _read_json("cells.json")
    for layer in cells["layers"]:
        layer["enabled"] = True
    for approved_layer in run.get("approval", {}).get("layers", []):
        for layer in cells["layers"]:
            if layer["id"] == approved_layer["id"]:
                layer["enabled"] = approved_layer["enabled"]
                layer["weight_default"] = approved_layer["weight"]
    findings_name = "findings_point.jsonl" if shape == "point" else "findings.jsonl"
    findings_path = _fixture_dir() / findings_name
    findings = _read_jsonl(findings_name) if findings_path.exists() else []
    evidence = _read_jsonl("evidence.jsonl")
    result_doc.update(
        {
            "run_id": run["run_id"],
            "status": "done",
            "report_md": result_doc.get("report_md", ""),
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
            "cells_geojson": _read_json("cells.geojson"),
            "candidates": _read_json("candidates.json") if shape == "point" else None,
            "trace": trace,
        }
    )
    run["status"] = "done"
    run["result"] = result_doc
    return result_doc
