"""Citation-integrity judge used by ``scripts/verify_citations.py``.

Checks, per run directory:

1. every ``[n]`` in ``report.md`` maps to a citable ledger entry ``e<n>``;
2. every citable ledger URL resolves (HTTP 200 or 3xx, redirects not followed);
3. every citable quote is found on re-fetch (via :class:`FactChecker`).

Score = (passes + 0.5 × partial quote matches) / checks.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from app.connectors.tavily import validate_public_url
from app.domain.research import UnsafeURLError
from app.evidence.citations import citation_numbers
from app.evidence.factcheck import FactChecker
from app.evidence.ledger import EvidenceLedger

DEFAULT_THRESHOLD = 0.9


@dataclass(frozen=True)
class JudgeRow:
    check: str
    target: str
    outcome: str  # pass | partial | fail
    detail: str

    @property
    def points(self) -> float:
        return {"pass": 1.0, "partial": 0.5}.get(self.outcome, 0.0)


@dataclass(frozen=True)
class JudgeReport:
    rows: tuple[JudgeRow, ...]

    @property
    def score(self) -> float:
        return sum(row.points for row in self.rows) / len(self.rows) if self.rows else 0.0

    def table(self) -> str:
        widths = [8, 6, 8]
        header = (
            f"{'check':<{widths[0]}}  {'target':<{widths[1]}}  {'outcome':<{widths[2]}}  detail"
        )
        lines = [header, "-" * len(header)]
        for row in self.rows:
            lines.append(
                f"{row.check:<{widths[0]}}  {row.target:<{widths[1]}}  "
                f"{row.outcome:<{widths[2]}}  {row.detail}"
            )
        lines.append(f"\nscore: {self.score * 100:.1f}% ({len(self.rows)} checks)")
        return "\n".join(lines)


async def _resolves(client: httpx.AsyncClient, url: str, resolver: Any = None) -> tuple[bool, str]:
    try:
        await validate_public_url(url, resolver)
    except UnsafeURLError as exc:
        return False, f"url rejected ({exc.message})"
    try:
        async with client.stream("GET", url, follow_redirects=False) as response:
            status = response.status_code
    except httpx.HTTPError as exc:
        return False, type(exc).__name__
    return 200 <= status < 400, f"HTTP {status}"


async def judge_run(run_dir: Path | str, client: httpx.AsyncClient, checker: FactChecker):
    run_dir = Path(run_dir)
    entries = EvidenceLedger(run_dir).entries()
    citable = {e.citation_number: e for e in entries if e.status != "unsupported"}
    rows: list[JudgeRow] = []
    report = run_dir / "report.md"
    text = report.read_text(encoding="utf-8") if report.exists() else ""
    text = text.split("\n## Could not verify\n")[0]
    if not report.exists():
        rows.append(JudgeRow("citation", "-", "fail", "report.md missing"))
    for number in sorted(set(citation_numbers(text))):
        entry = citable.get(number)
        detail = f"→ e{number} ({entry.method})" if entry else f"no citable ledger entry e{number}"
        rows.append(JudgeRow("citation", f"[{number}]", "pass" if entry else "fail", detail))
    checked = list(citable.values())
    for entry in checked:
        ok, detail = await _resolves(client, entry.url, checker.resolver)
        rows.append(
            JudgeRow("url", entry.evidence_id, "pass" if ok else "fail", f"{detail} {entry.url}")
        )
    for entry, result in zip(checked, await checker.check(checked)):
        outcome = {"verified": "pass", "partial": "partial"}.get(result.status, "fail")
        rows.append(JudgeRow("quote", entry.evidence_id, outcome, result.reason))
    return JudgeReport(tuple(rows))
