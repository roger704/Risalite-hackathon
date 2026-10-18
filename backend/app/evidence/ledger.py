"""Append-only evidence ledger at ``runs/<id>/evidence.jsonl``.

Ids are stable per run (``e1``, ``e2``, ...) and map one-to-one to ``[n]`` citations in
``report.md``. Status changes (fact-check) are appended as new revisions of the same id;
readers take the latest revision.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from app.contracts.evidence import Confidence, FactCheck, LedgerEntry, SourceRef, Status, Tier
from app.domain.errors import ValidationError
from app.domain.research import EvidenceKind, ExtractionStatus, WebEvidence

LEDGER_FILE = "evidence.jsonl"
MAX_DEFAULT_QUOTE_CHARS = 500


def value_hash(value: Any) -> str:
    """SHA-256 of the canonical JSON form of a local-snapshot value."""
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class EvidenceLedger:
    def __init__(self, run_dir: Path | str):
        self.run_dir = Path(run_dir)
        self.path = self.run_dir / LEDGER_FILE

    # -- reading -------------------------------------------------------------------------
    def history(self) -> list[LedgerEntry]:
        """Every line in file order, including superseded revisions."""
        if not self.path.exists():
            return []
        entries = []
        with self.path.open(encoding="utf-8") as handle:
            for number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    entries.append(LedgerEntry.model_validate_json(line))
                except ValueError as exc:
                    raise ValidationError(f"{self.path}:{number}: invalid ledger entry") from exc
        return entries

    def entries(self) -> list[LedgerEntry]:
        """Latest revision per ``evidence_id``, in order of first appearance."""
        latest: dict[str, LedgerEntry] = {}
        for entry in self.history():
            latest[entry.evidence_id] = entry
        return list(latest.values())

    def get(self, evidence_id: str) -> LedgerEntry | None:
        return next((e for e in self.entries() if e.evidence_id == evidence_id), None)

    def next_id(self) -> str:
        numbers = [entry.citation_number for entry in self.history()]
        return f"e{max(numbers, default=0) + 1}"

    # -- writing -------------------------------------------------------------------------
    def _write(self, entry: LedgerEntry) -> LedgerEntry:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(entry.model_dump_json(exclude_none=True) + "\n")
        return entry

    def append(
        self,
        *,
        url: str,
        quote: str,
        method: str,
        tier: Tier,
        confidence: Confidence,
        retrieved_at: datetime | None = None,
        **fields: Any,
    ) -> LedgerEntry:
        """Append a new entry; identical (url, quote, method, source_ref) returns the existing id."""
        values = {
            "url": url,
            "quote": quote,
            "method": method,
            "tier": tier,
            "confidence": confidence,
            "retrieved_at": retrieved_at or _utc_now(),
            **fields,
        }
        candidate = LedgerEntry.model_validate({"evidence_id": "e1", **values})
        for existing in self.entries():
            if (existing.url, existing.quote, existing.method, existing.source_ref) == (
                candidate.url,
                candidate.quote,
                candidate.method,
                candidate.source_ref,
            ):
                return existing
        return self._write(candidate.model_copy(update={"evidence_id": self.next_id()}))

    def extend(self, items: Iterable[Mapping[str, Any]]) -> list[LedgerEntry]:
        return [self.append(**dict(item)) for item in items]

    def set_status(
        self, evidence_id: str, status: Status, fact_check: FactCheck | None = None
    ) -> LedgerEntry:
        current = self.get(evidence_id)
        if current is None:
            raise ValidationError(f"Unknown evidence_id: {evidence_id}")
        revision = current.model_copy(
            update={"status": status, "fact_check": fact_check, "revision": current.revision + 1}
        )
        return self._write(LedgerEntry.model_validate(revision.model_dump()))

    # -- converters ----------------------------------------------------------------------
    def add_web_evidence(
        self,
        evidence: WebEvidence,
        *,
        tier: Tier,
        confidence: Confidence,
        quote: str | None = None,
        claim_type: str | None = None,
    ) -> LedgerEntry:
        return self.append(
            **web_evidence_fields(
                evidence, tier=tier, confidence=confidence, quote=quote, claim_type=claim_type
            )
        )

    def add_snapshot_value(self, **kwargs: Any) -> LedgerEntry:
        return self.append(**snapshot_fields(**kwargs))


def web_evidence_fields(
    evidence: WebEvidence,
    *,
    tier: Tier,
    confidence: Confidence,
    quote: str | None = None,
    claim_type: str | None = None,
) -> dict[str, Any]:
    """Ledger fields for a Tavily ``WebEvidence``. The quote must be verbatim from the page."""
    if evidence.extraction_status is ExtractionStatus.FAILED:
        raise ValidationError("Failed web evidence cannot enter the ledger")
    text = evidence.passage if evidence.kind is EvidenceKind.EXTRACTED_PAGE else None
    text = text or evidence.snippet or evidence.passage
    if quote is None:
        if not text:
            raise ValidationError("Web evidence has no passage or snippet to quote")
        quote = text.strip()[:MAX_DEFAULT_QUOTE_CHARS]
    method = "tavily-extract" if evidence.kind is EvidenceKind.EXTRACTED_PAGE else "tavily-search"
    return {
        "url": evidence.original_url,
        "quote": quote,
        "method": method,
        "tier": tier,
        "confidence": confidence,
        "retrieved_at": evidence.retrieved_at,
        "title": evidence.title,
        "claim_type": claim_type,
        "source_ref": SourceRef(
            query=evidence.query,
            content_hash=evidence.content_hash,
            provider_request_id=evidence.provider_request_id,
            web_evidence_id=evidence.evidence_id,
        ),
    }


def snapshot_fields(
    *,
    url: str,
    dataset_id: str,
    snapshot_id: str,
    query: Mapping[str, Any] | None,
    value: Any,
    quote: str,
    retrieved_at: datetime,
    tier: Tier = "official",
    confidence: Confidence = "high",
    title: str | None = None,
    claim_type: str | None = None,
) -> dict[str, Any]:
    """Ledger fields for a value read from a local ``DataService`` snapshot.

    ``url`` is the snapshot manifest's ``resource_url`` (the public source it was built from).
    """
    return {
        "url": url,
        "quote": quote,
        "method": "local-snapshot",
        "tier": tier,
        "confidence": confidence,
        "retrieved_at": retrieved_at,
        "title": title,
        "claim_type": claim_type,
        "source_ref": SourceRef(
            dataset_id=dataset_id,
            snapshot_id=snapshot_id,
            query=dict(query or {}),
            value=value,
            content_hash=value_hash(value),
        ),
    }


def snapshot_fields_from_manifest(
    manifest: Mapping[str, Any], *, query: Mapping[str, Any] | None, value: Any, quote: str, **kw
) -> dict[str, Any]:
    """Same as :func:`snapshot_fields`, taking provenance from a ``DataService.manifest`` dict."""
    retrieved_at = manifest["retrieved_at"]
    if isinstance(retrieved_at, str):
        retrieved_at = datetime.fromisoformat(retrieved_at.replace("Z", "+00:00"))
    return snapshot_fields(
        url=manifest["resource_url"],
        dataset_id=manifest["dataset_id"],
        snapshot_id=manifest["snapshot_id"],
        query=query,
        value=value,
        quote=quote,
        retrieved_at=retrieved_at,
        **kw,
    )
