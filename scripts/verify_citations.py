#!/usr/bin/env python
"""Verify the citations of a research run directory.

Usage (from the repository root):

    cd backend && uv run python ../scripts/verify_citations.py ../runs/<id> [--threshold 0.9]
    cd backend && uv run python ../scripts/verify_citations.py ../frontend/fixtures --offline

Checks that every report citation maps to a ledger entry, every cited URL resolves and
every quote re-matches its source. Uses Tavily ``extract`` when ``TAVILY_API_KEY`` is set,
otherwise a direct HTTP GET. ``--offline`` matches quotes against the stored passage only.
Exits 1 when the score is below the threshold.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.connectors.tavily import TavilyClient, TavilyConfig  # noqa: E402
from app.evidence.factcheck import FactChecker, datasvc_snapshot_reader  # noqa: E402
from app.evidence.judge import DEFAULT_THRESHOLD, judge_run  # noqa: E402


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--offline", action="store_true", help="no network; stored text only")
    parser.add_argument("--json", action="store_true", help="print the full JSON report")
    args = parser.parse_args(argv)
    if not args.run_dir.is_dir():
        parser.error(f"not a directory: {args.run_dir}")

    research = None
    if not args.offline and os.environ.get("TAVILY_API_KEY"):
        research = TavilyClient(TavilyConfig.from_env())
    checker = FactChecker(
        research=research, snapshot_reader=datasvc_snapshot_reader(), offline=args.offline
    )
    try:
        report = await judge_run(args.run_dir, checker, args.threshold)
    finally:
        await checker.aclose()
        if research is not None:
            await research.aclose()

    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        for check in report.checks:
            mark = "skip" if check.score is None else f"{check.score:.1f}"
            print(f"{mark:>4}  {check.check:<8} {check.id:<24} {check.detail}")
        verdict = "PASS" if report.passed else "FAIL"
        print(f"\n{verdict}: score {report.score:.2f} (threshold {report.threshold})")
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
