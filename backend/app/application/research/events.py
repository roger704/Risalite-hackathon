"""Append-only event stream for research runs."""

import inspect
import json
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class EventSink:
    def __init__(
        self,
        run_dir: Path,
        run_id: str | None = None,
        callback: Callable[[dict[str, Any]], Any] | None = None,
    ):
        self.run_dir = Path(run_dir)
        self.run_id = run_id or f"run_{uuid.uuid4().hex}"
        self.callback = callback
        self.path = self.run_dir / "trace.jsonl"
        self.seq = 0
        self.events: list[dict[str, Any]] = []

    async def emit(
        self,
        event_type: str,
        *,
        agent: str | None = None,
        why: str | None = None,
        detail: dict[str, Any] | None = None,
        model: str | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        seconds: float | None = None,
    ) -> dict[str, Any]:
        self.seq += 1
        event = {
            "run_id": self.run_id,
            "seq": self.seq,
            "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "type": event_type,
            "agent": agent,
            "why": why,
            "detail": detail,
            "model": model,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "seconds": seconds,
        }
        self.run_dir.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        self.events.append(event)
        if self.callback is not None:
            result = self.callback(event)
            if inspect.isawaitable(result):
                await result
        return event
