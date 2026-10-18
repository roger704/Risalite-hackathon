"""Run event log: ``ResearchEvent`` with seq/ts/run_id, kept in memory and in ``events.jsonl``."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, get_args

from app.contracts.events import ResearchEvent
from app.evidence.ledger import utc_now

EVENT_TYPES = frozenset(get_args(ResearchEvent.model_fields["type"].annotation))
EVENTS_FILE = "events.jsonl"
TRACE_FILE = "trace.jsonl"
_FIELDS = {"why", "model", "tokens_in", "tokens_out", "seconds"}


class EventLog:
    def __init__(self, run_dir: Path, run_id: str):
        self.run_dir = Path(run_dir)
        self.run_id = run_id
        self.events: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def emit(
        self, type: str, agent: str, detail: dict | str | None = None, **fields: Any
    ) -> dict[str, Any]:
        extra = {k: v for k, v in fields.items() if v is not None}
        with self._lock:
            event = ResearchEvent(
                seq=len(self.events),
                run_id=self.run_id,
                ts=utc_now(),
                type=type if type in EVENT_TYPES else "status",
                agent=agent,
                detail=detail if detail is not None else {},
                **extra,
            ).model_dump(exclude_none=True)
            if event["type"] != type:
                event["original_type"] = type
            self.events.append(event)
            self.run_dir.mkdir(parents=True, exist_ok=True)
            line = json.dumps(event, ensure_ascii=False, default=str) + "\n"
            for name in (EVENTS_FILE, TRACE_FILE):
                with (self.run_dir / name).open("a", encoding="utf-8") as handle:
                    handle.write(line)
        return event

    def emitter(self, agent: str):
        """``research.Emit`` adapter: ``(type, payload)``; payload keys why/model/tokens/seconds/agent
        are lifted to event fields, the rest becomes ``detail``."""

        def emit(type: str, payload: dict | None = None) -> None:
            payload = dict(payload or {})
            fields = {k: payload.pop(k) for k in list(payload) if k in _FIELDS}
            who = str(payload.pop("agent", agent))
            detail = payload.pop("detail", payload)
            self.emit(type, who, detail, **fields)

        return emit

    def after(self, seq: int) -> list[dict[str, Any]]:
        with self._lock:
            return [e for e in self.events if e["seq"] > seq]


def metrics(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per role (agent prefix): model, calls, tokens_in/out, seconds; plus search/extract counts."""
    result: dict[str, dict[str, Any]] = {}
    for event in events:
        role = str(event.get("agent", "system")).split(":")[0]
        item = result.setdefault(
            role, {"model": None, "calls": 0, "tokens_in": 0, "tokens_out": 0, "seconds": 0.0, "searches": 0, "extracts": 0}
        )
        if event.get("model"):
            item["model"] = event["model"]
        if event["type"] == "model_call":
            item["calls"] += 1
        if event["type"] == "search":
            item["searches"] += 1
        if event["type"] == "extract":
            item["extracts"] += 1
        item["tokens_in"] += int(event.get("tokens_in") or 0)
        item["tokens_out"] += int(event.get("tokens_out") or 0)
        item["seconds"] = round(item["seconds"] + float(event.get("seconds") or 0.0), 3)
    return result
