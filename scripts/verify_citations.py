#!/usr/bin/env python3
"""Judge a Risalite run directory for citation integrity.

Usage (from the repository root):

    cd backend && uv run python ../scripts/verify_citations.py ../runs/<id> [--threshold 0.9]

Checks every ``[n]`` in ``report.md`` maps to ledger entry ``e<n>``, every ledger URL resolves
(HTTP 200/3xx) and every quote is found on re-fetch (Tavily ``extract`` when
``TAVILY_API_KEY`` is set, else a plain HTTP GET; WFS re-query; local snapshot re-read).
Prints a table and a % score; exits 1 when the score is below the threshold.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import httpx  # noqa: E402

from app.evidence.factcheck import FactChecker, datasvc_snapshot_reader  # noqa: E402
from app.evidence.judge import DEFAULT_THRESHOLD, judge_run  # noqa: E402


async def main(
    argv: list[str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    resolver=None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--data-dir", type=Path, default=None, help="DataService snapshot store")
    args = parser.parse_args(argv)
    if not (args.run_dir / "evidence.jsonl").exists():
        print(f"{args.run_dir}: no evidence.jsonl", file=sys.stderr)
        return 2
    research = None
    async with httpx.AsyncClient(
        timeout=30.0, follow_redirects=True, transport=transport
    ) as client:
        if os.environ.get("TAVILY_API_KEY", "").strip():
            from app.connectors.tavily import TavilyClient, TavilyConfig
            from app.domain.research import ResearchBudget

            research = TavilyClient(
                TavilyConfig(api_key=os.environ["TAVILY_API_KEY"], max_passage_chars=200_000),
                ResearchBudget(0, 100, 0, 0),
            )
        checker = FactChecker(
            research=research,
            http=client,
            snapshot_reader=datasvc_snapshot_reader(args.data_dir),
            resolver=resolver,
        )
        try:
            report = await judge_run(args.run_dir, client, checker)
        finally:
            if research is not None:
                await research.aclose()
    print(report.table())
    passed = report.score >= args.threshold
    print(f"threshold: {args.threshold * 100:.0f}% → {'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
