import asyncio
from typing import Annotated, AsyncIterator

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.agent.events import EventBus
from app.agent.live import build_orchestrator
from app.contracts.api import ApproveRequest, ResearchRequest, RunState

router = APIRouter()


def _orchestrator(request: Request):
    if request.app.state.research_orchestrator is None:
        request.app.state.research_orchestrator = build_orchestrator(
            request.app.state.research_runs_dir,
            request.app.state.research_mock,
        )
    return request.app.state.research_orchestrator


@router.post("/research", response_model=RunState, status_code=201)
async def create_research(body: ResearchRequest, request: Request):
    return await _orchestrator(request).start(body)


@router.post("/runs/{run_id}/approve", response_model=RunState, status_code=202)
async def approve_research(
    run_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    body: ApproveRequest | None = None,
):
    orchestrator = _orchestrator(request)
    try:
        state = await orchestrator.approve(run_id, body)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Unknown run") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    background_tasks.add_task(orchestrator.execute, run_id)
    return state


@router.get("/runs/{run_id}/events")
async def research_events(
    run_id: str,
    request: Request,
    after: Annotated[int | None, Query(ge=0)] = None,
):
    orchestrator = _orchestrator(request)
    state = orchestrator.get_state(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Unknown run")
    bus: EventBus = orchestrator.bus
    queue = bus.subscribe(run_id)
    header = request.headers.get("last-event-id")
    last_seen = int(header) if header and header.isdigit() else after or 0

    async def stream() -> AsyncIterator[str]:
        nonlocal last_seen
        try:
            for event in orchestrator.events(run_id):
                if event.seq <= last_seen:
                    continue
                last_seen = event.seq
                yield f"id: {event.seq}\nevent: {event.stage}\ndata: {event.model_dump_json()}\n\n"
            current = orchestrator.get_state(run_id)
            if current and current.status in {"done", "failed", "needs_clarification"}:
                return
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=20)
                except TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                if event.seq <= last_seen:
                    continue
                last_seen = event.seq
                yield f"id: {event.seq}\nevent: {event.stage}\ndata: {event.model_dump_json()}\n\n"
                if event.stage == "done" or (event.stage == "error" and event.type == "error"):
                    return
        finally:
            bus.unsubscribe(run_id, queue)

    return StreamingResponse(stream(), media_type="text/event-stream")


@router.get("/runs/{run_id}/result")
async def research_result(run_id: str, request: Request):
    result = _orchestrator(request).result(run_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Unknown run")
    return result
