import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import AsyncIterator

from app.contracts.events import ResearchEvent


class EventBus:
    def __init__(self):
        self._subscribers: dict[str, set[asyncio.Queue]] = {}

    def subscribe(self, run_id: str) -> asyncio.Queue:
        queue = asyncio.Queue()
        self._subscribers.setdefault(run_id, set()).add(queue)
        return queue

    def unsubscribe(self, run_id: str, queue: asyncio.Queue) -> None:
        self._subscribers.get(run_id, set()).discard(queue)

    async def publish(self, event: ResearchEvent) -> None:
        for queue in tuple(self._subscribers.get(event.run_id, ())):
            await queue.put(event)


class TraceWriter:
    def __init__(self, run_dir: Path, run_id: str, bus: EventBus):
        self.path = run_dir / "trace.jsonl"
        self.run_id = run_id
        self.bus = bus
        self.seq = 0
        if self.path.exists():
            lines = [line for line in self.path.read_text(encoding="utf-8").splitlines() if line]
            if lines:
                self.seq = ResearchEvent.model_validate_json(lines[-1]).seq

    async def emit(
        self,
        stage: str,
        role: str,
        type: str,
        text: str,
        why: str,
        model: str | None = None,
        tokens: int = 0,
        seconds: float = 0.0,
        data: dict | None = None,
    ) -> ResearchEvent:
        self.seq += 1
        event = ResearchEvent(
            seq=self.seq,
            run_id=self.run_id,
            ts=datetime.now(timezone.utc),
            stage=stage,
            role=role,
            type=type,
            text=text,
            why=why,
            model=model,
            tokens=tokens,
            seconds=seconds,
            data=data,
        )
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(event.model_dump_json() + "\n")
        await self.bus.publish(event)
        return event


@asynccontextmanager
async def timed() -> AsyncIterator[dict[str, float]]:
    result: dict[str, float] = {}
    start = perf_counter()
    try:
        yield result
    finally:
        result["seconds"] = perf_counter() - start
