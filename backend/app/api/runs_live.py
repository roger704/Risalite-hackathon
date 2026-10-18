"""Live run API (same routes, bearer run token and SSE behaviour as ``runs_fixtures``)."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from app.api.runs_fixtures import ClarifyRequest
from app.application.runs import Deps, RunError, RunService
from app.contracts.api import ApproveRequest, ResearchRequest

router = APIRouter()
_SERVICE: RunService | None = None
POLL_SECONDS = 0.1


def runs_dir() -> Path:
    configured = os.environ.get("RISALITE_RUNS_DIR", "").strip()
    return Path(configured).expanduser() if configured else Path("data/runs")


def default_deps() -> Deps:
    from app import research
    from app.application.area_data import FixtureAreaData, SummaryAreaData
    from app.evidence.factcheck import FactChecker
    from app.scoring import v2

    if os.environ.get("RISALITE_AREA_DATA", "summary") in {"fixtures", "fixture"}:
        port = FixtureAreaData.from_frontend_fixtures(
            Path(__file__).resolve().parents[3] / "frontend" / "fixtures"
        )
    else:
        from app.application.service import DataService

        port = SummaryAreaData.from_data_service(DataService())
    try:
        from app.application import t1
    except ImportError:
        t1 = None
    t1_readers = None
    if t1 is not None and os.environ.get("RISALITE_T1_READERS", "snapshot") == "static":
        from app.application.t1.demo import demo_readers

        t1_readers = demo_readers

    def config():
        return research.ResearchConfig()

    def checker():
        return FactChecker(
            offline=config().fake or os.environ.get("RISALITE_FACTCHECK_OFFLINE") == "1"
        )

    return Deps(
        scoring=v2,
        research=research,
        port=port,
        checker_factory=checker,
        config_factory=config,
        t1=t1,
        t1_readers=t1_readers,
    )


def configure(service: RunService | None) -> None:
    global _SERVICE
    _SERVICE = service


def service() -> RunService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = RunService(default_deps(), runs_dir())
    return _SERVICE


def _call(function, *args):
    try:
        return function(*args)
    except RunError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc


def _get(run_id: str, authorization: str | None) -> dict:
    return _call(service().get, run_id, authorization)


@router.post("/research")
async def research(body: ResearchRequest):
    try:
        return await service().create(body.question, body.role, body.profile, body.template_hint)
    except RunError as exc:
        return JSONResponse(status_code=exc.status, content={"detail": exc.detail})


@router.post("/runs/{run_id}/clarify")
def clarify(run_id: str, payload: dict, authorization: str | None = Header(default=None)):
    run = _get(run_id, authorization)
    try:
        body = ClarifyRequest.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="A non-empty answer is required") from exc
    token = authorization.removeprefix("Bearer ") if authorization else ""
    return _call(service().clarify, run, body.answer, token)


@router.get("/runs/{run_id}/areas")
def search_areas(
    run_id: str, q: str = Query(default=""), authorization: str | None = Header(default=None)
):
    _get(run_id, authorization)
    return service().search_areas(q)


@router.post("/runs/{run_id}/approve")
async def approve(run_id: str, payload: dict, authorization: str | None = Header(default=None)):
    run = _get(run_id, authorization)
    if run["status"] != "awaiting_approval":
        raise HTTPException(status_code=409, detail="Run is not awaiting approval")
    try:
        request = ApproveRequest.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="Invalid approval payload") from exc
    return _call(service().approve, run, request.model_dump(exclude_none=True), set(payload))


@router.get("/runs/{run_id}/events")
async def events(
    run_id: str,
    authorization: str | None = Header(default=None),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
):
    run = _get(run_id, authorization)
    if run["status"] in {"needs_clarification", "awaiting_approval"}:
        raise HTTPException(status_code=409, detail="Run has not been approved")
    log = service().log(run_id)
    after = int(last_event_id) if last_event_id and last_event_id.isdigit() else -1

    async def stream():
        nonlocal after
        idle = 0.0
        while True:
            pending = log.after(after)
            for event in pending:
                after = event["seq"]
                yield f"id: {event['seq']}\ndata: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
            if run["status"] in {"done", "failed"} and not log.after(after):
                return
            await asyncio.sleep(POLL_SECONDS)
            idle = 0.0 if pending else idle + POLL_SECONDS
            if idle >= 15:
                idle = 0.0
                yield ": keep-alive\n\n"

    return StreamingResponse(
        stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
    )


@router.get("/runs/{run_id}/result")
def result(run_id: str, authorization: str | None = Header(default=None)):
    status, body = service().result(_get(run_id, authorization))
    return JSONResponse(status_code=status, content=json.loads(json.dumps(body, default=str)))
