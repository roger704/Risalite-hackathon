"""Append-only ``runs/<id>/trace.jsonl`` writer in the §9.1 event shape."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.contracts.events import ResearchEvent
from app.evidence.ledger import utc_now

TRACE_FILE = "trace.jsonl"


class TraceWriter:
    def __init__(self, run_dir: Path | str, run_id: str | None = None):
        self.run_dir = Path(run_dir)
        self.path = self.run_dir / TRACE_FILE
        self.run_id = run_id or self.run_dir.name
        self._seq = self._next_seq()

    def _next_seq(self) -> int:
        if not self.path.exists():
            return 0
        seqs = [
            json.loads(line).get("seq", -1)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        return max(seqs, default=-1) + 1

    def emit(self, type: str, agent: str, why: str, **fields: Any) -> dict[str, Any]:
        event = ResearchEvent(
            seq=self._seq,
            run_id=self.run_id,
            ts=utc_now(),
            type=type,
            agent=agent,
            why=why,
            **fields,
        ).model_dump(exclude_none=True)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        self._seq += 1
        return event
