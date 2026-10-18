"""Append-only evidence ledger at ``runs/<id>/evidence.jsonl`` (Deep_Research_Spec §9.2).

Web entries come from Tavily ``WebEvidence`` (``ev_…`` ids); dataset entries get a
deterministic ``ds_…`` id. Fact-check status changes are appended as new revisions of the
same id; readers take the latest revision.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from app.contracts.evidence import EvidenceEntry
from app.domain.errors import ValidationError
from app.domain.research import EvidenceKind, ExtractionStatus, WebEvidence

LEDGER_FILE = "evidence.jsonl"
STATUSES = {"unverified", "verified", "partial", "unsupported"}


def value_hash(value: Any) -> str:
    """SHA-256 of the canonical JSON form of a dataset value."""
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def entry_status(entry: dict[str, Any]) -> str:
    status = entry.get("status") or entry.get("verification_status") or "unverified"
    return status if status in STATUSES else "unverified"


def entry_url(entry: dict[str, Any]) -> str:
    return (
        entry.get("url")
        or entry.get("source_url")
        or entry.get("original_url")
        or entry.get("canonical_url")
        or ""
    )


def entry_text(entry: dict[str, Any]) -> str:
    """Stored source text for an entry (passage beats snippet)."""
    return entry.get("passage") or entry.get("snippet") or ""


def web_entry(evidence: WebEvidence) -> dict[str, Any]:
    if evidence.extraction_status is ExtractionStatus.FAILED:
        raise ValidationError("Failed web evidence cannot enter the ledger")
    data = evidence.to_dict()
    extracted = evidence.kind is EvidenceKind.EXTRACTED_PAGE
    data.update(
        url=evidence.original_url,
        method="tavily-extract" if extracted else "tavily-search",
        status="unverified",
    )
    return data


def dataset_entry(
    *,
    dataset_id: str,
    snapshot_id: str,
    layer_id: str,
    source_url: str,
    value: Any,
    query: dict[str, Any] | None = None,
    quote: str | None = None,
    title: str | None = None,
    retrieved_at: str | None = None,
    tier: str = "official",
) -> dict[str, Any]:
    """Ledger entry for a value computed from a pinned local snapshot (``method: dataset``)."""
    digest = value_hash([dataset_id, snapshot_id, layer_id, query or {}])
    return {
        "evidence_id": f"ds_{digest[:20]}",
        "method": "dataset",
        "dataset_id": dataset_id,
        "snapshot_id": snapshot_id,
        "layer_id": layer_id,
        "url": source_url,
        "source_url": source_url,
        "query": dict(query or {}),
        "value_hash": value_hash(value),
        "quote": quote,
        "title": title,
        "tier": tier,
        "confidence": "high",
        "retrieved_at": retrieved_at or utc_now(),
        "status": "unverified",
    }


class EvidenceLedger:
    def __init__(self, run_dir: Path | str):
        self.run_dir = Path(run_dir)
        self.path = self.run_dir / LEDGER_FILE

    def history(self) -> list[dict[str, Any]]:
        """Every line in file order, including superseded revisions.

        Reads are lenient (older fixtures carry numeric confidences); writes validate.
        """
        if not self.path.exists():
            return []
        rows = []
        for number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError as exc:
                raise ValidationError(f"{self.path}:{number}: invalid ledger entry") from exc
            if isinstance(row, dict) and not row.get("evidence_id") and row.get("id"):
                row["evidence_id"] = str(row["id"])
            if not isinstance(row, dict) or not row.get("evidence_id"):
                raise ValidationError(f"{self.path}:{number}: entry without evidence_id")
            rows.append(row)
        return rows

    def entries(self) -> list[dict[str, Any]]:
        """Latest revision per ``evidence_id``, in order of first appearance."""
        latest: dict[str, dict[str, Any]] = {}
        for row in self.history():
            latest[row["evidence_id"]] = row
        return list(latest.values())

    def get(self, evidence_id: str) -> dict[str, Any] | None:
        return next((e for e in self.entries() if e["evidence_id"] == evidence_id), None)

    def _write(self, entry: dict[str, Any]) -> dict[str, Any]:
        EvidenceEntry.model_validate(entry)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        clean = {k: v for k, v in entry.items() if v is not None}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(clean, ensure_ascii=False) + "\n")
        return clean

    def append(self, entry: dict[str, Any]) -> dict[str, Any]:
        """Append an entry; an id already in the ledger returns the stored entry unchanged."""
        existing = self.get(entry["evidence_id"])
        return existing if existing is not None else self._write(entry)

    def extend(self, entries: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        return [self.append(entry) for entry in entries]

    def add_web_evidence(self, evidence: WebEvidence) -> dict[str, Any]:
        return self.append(web_entry(evidence))

    def add_dataset_value(self, **kwargs: Any) -> dict[str, Any]:
        return self.append(dataset_entry(**kwargs))

    def set_status(
        self, evidence_id: str, status: str, fact_check: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if status not in STATUSES:
            raise ValidationError(f"Unknown status: {status}")
        current = self.get(evidence_id)
        if current is None:
            raise ValidationError(f"Unknown evidence_id: {evidence_id}")
        revision = {
            **current,
            "status": status,
            "verification_status": status,
            "fact_check": fact_check,
            "revision": int(current.get("revision", 0)) + 1,
        }
        return self._write(revision)
