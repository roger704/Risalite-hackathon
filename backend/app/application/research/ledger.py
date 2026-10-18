"""Persistent evidence and finding stores for research runs."""

import json
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

from app.domain.research import EvidencePage, WebEvidence


def _append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + "\n")


class EvidenceLedger:
    def __init__(self, run_dir: Path):
        self.run_dir = Path(run_dir)
        self._evidence: dict[str, WebEvidence] = {}
        self.path = self.run_dir / "evidence.jsonl"

    def add_page(self, page: EvidencePage) -> list[WebEvidence]:
        added: list[WebEvidence] = []
        for evidence in page.evidence:
            previous = self._evidence.get(evidence.evidence_id)
            if previous is not None:
                evidence = self._merge(previous, evidence)
                unchanged = replace(
                    evidence,
                    retrieved_at=previous.retrieved_at,
                    provider_request_id=previous.provider_request_id,
                )
                if unchanged == previous:
                    continue
            self._evidence[evidence.evidence_id] = evidence
            _append_jsonl(self.path, evidence.to_dict())
            added.append(evidence)
        return added

    @staticmethod
    def _merge(previous: WebEvidence, current: WebEvidence) -> WebEvidence:
        return replace(
            previous,
            kind=current.kind,
            title=current.title or previous.title,
            query=current.query or previous.query,
            retrieved_at=current.retrieved_at,
            published_at=current.published_at or previous.published_at,
            published_at_raw=current.published_at_raw or previous.published_at_raw,
            snippet=current.snippet or previous.snippet,
            passage=current.passage or previous.passage,
            passage_locator=current.passage_locator or previous.passage_locator,
            extraction_status=current.extraction_status,
            failure_reason=current.failure_reason,
            content_hash=current.content_hash or previous.content_hash,
            provider_request_id=current.provider_request_id or previous.provider_request_id,
            score=current.score if current.score is not None else previous.score,
        )

    def get(self, evidence_id: str) -> WebEvidence | None:
        return self._evidence.get(evidence_id)

    def has(self, evidence_id: str) -> bool:
        return evidence_id in self._evidence

    def all(self) -> list[WebEvidence]:
        return list(self._evidence.values())


class FindingStore:
    def __init__(self, run_dir: Path):
        self.run_dir = Path(run_dir)
        self.path = self.run_dir / "findings.jsonl"
        self.accepted: list[dict[str, Any]] = []
        self.rejected: list[dict[str, Any]] = []

    def accept(
        self,
        claim: str,
        quote: str,
        evidence_ids: list[str],
        claim_type: str,
        localization: str | None,
    ) -> dict[str, Any]:
        finding = {
            "finding_id": f"finding_{uuid.uuid4().hex}",
            "claim": claim,
            "quote": quote,
            "evidence_ids": evidence_ids,
            "claim_type": claim_type,
            "localization": localization,
        }
        self.accepted.append(finding)
        _append_jsonl(self.path, finding)
        return finding

    def reject(self, finding: dict[str, Any]) -> None:
        self.rejected.append(finding)
