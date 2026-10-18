"""Append-only ``runs/<id>/trace.jsonl`` writer for research events."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.contracts.events import ResearchEvent

TRACE_FILE = "trace.jsonl"


class TraceWriter:
    def __init__(self, run_dir: Path | str, run_id: str | None = None):
        self.run_dir = Path(run_dir)
        self.path = self.run_dir / TRACE_FILE
        self.run_id = run_id or self.run_dir.name
        self._seq = len(self.read())

    def read(self) -> list[ResearchEvent]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        return [ResearchEvent.model_validate_json(line) for line in lines if line.strip()]

    def emit(self, role: str, type: str, text: str, why: str, **fields: Any) -> ResearchEvent:
        event = ResearchEvent(
            run_id=self.run_id,
            seq=self._seq,
            ts=datetime.now(timezone.utc),
            role=role,
            type=type,
            text=text,
            why=why,
            **fields,
        )
        self.run_dir.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(event.model_dump_json(exclude_none=True) + "\n")
        self._seq += 1
        return event
