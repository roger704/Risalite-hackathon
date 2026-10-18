"""Judge-facing citation check for a finished run (``scripts/verify_citations.py``).

1. every citation marker in the report maps to a citable ledger entry;
2. every cited URL resolves (skipped offline);
3. every cited quote re-matches its source (stored text when offline).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.evidence.citations import citable, citation_tokens
from app.evidence.factcheck import FactChecker
from app.evidence.ledger import EvidenceLedger, entry_text, entry_url

DEFAULT_THRESHOLD = 0.9
SCORES = {"verified": 1.0, "partial": 0.5, "unsupported": 0.0}


@dataclass(frozen=True)
class Check:
    check: str  # citation | url | quote
    id: str
    score: float | None  # None = skipped
    detail: str


@dataclass
class JudgeReport:
    run_dir: str
    threshold: float
    checks: list[Check] = field(default_factory=list)

    @property
    def score(self) -> float:
        scored = [c.score for c in self.checks if c.score is not None]
        return sum(scored) / len(scored) if scored else 0.0

    @property
    def passed(self) -> bool:
        return self.score >= self.threshold

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_dir": self.run_dir,
            "threshold": self.threshold,
            "score": round(self.score, 3),
            "passed": self.passed,
            "checks": [asdict(c) for c in self.checks],
        }


def read_report(run_dir: Path) -> str:
    """``report.md``, else ``report_md`` from ``result*.json`` in the run directory."""
    path = run_dir / "report.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    for result in sorted(run_dir.glob("result*.json")):
        report = json.loads(result.read_text(encoding="utf-8")).get("report_md")
        if report:
            return report
    return ""


async def judge_run(
    run_dir: Path | str, checker: FactChecker, threshold: float = DEFAULT_THRESHOLD
) -> JudgeReport:
    run_dir = Path(run_dir)
    report = JudgeReport(str(run_dir), threshold)
    table = citable(EvidenceLedger(run_dir).entries())
    tokens = list(dict.fromkeys(citation_tokens(read_report(run_dir))))
    if not tokens:
        report.checks.append(Check("citation", "-", 0.0, "report has no citation markers"))
    cited: dict[str, dict[str, Any]] = {}
    for token in tokens:
        entry = table.get(token)
        if entry is None:
            report.checks.append(Check("citation", token, 0.0, "no citable ledger entry"))
            continue
        cited.setdefault(entry["evidence_id"], entry)
        report.checks.append(Check("citation", token, 1.0, entry["evidence_id"]))

    await checker.prefetch(cited.values())
    for eid, entry in cited.items():
        url = entry_url(entry)
        if checker.offline or entry.get("method") == "dataset":
            report.checks.append(Check("url", eid, None, "skipped"))
        elif not url:
            report.checks.append(Check("url", eid, 0.0, "no URL"))
        else:
            try:
                response = await checker.get_public(url)
                ok = response.status_code < 400
                detail = f"HTTP {response.status_code}"
            except Exception as exc:
                ok, detail = False, f"{type(exc).__name__}"
            report.checks.append(Check("url", eid, 1.0 if ok else 0.0, f"{url} {detail}"))
        if checker.offline and (entry.get("method") == "dataset" or not entry_text(entry)):
            report.checks.append(Check("quote", eid, None, "skipped: no stored text offline"))
            continue
        result = await checker.check_entry(entry)
        report.checks.append(Check("quote", eid, SCORES[result.status], result.reason))
    return report
