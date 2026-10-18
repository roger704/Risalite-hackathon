import argparse
import asyncio

from app.agent.live import build_orchestrator
from app.contracts.api import ApproveRequest, ResearchRequest


async def run(question: str, mock: bool) -> str:
    orchestrator = build_orchestrator(mock=mock)
    state = await orchestrator.start(ResearchRequest(question=question))
    if state.status == "awaiting_approval":
        await orchestrator.approve(state.run_id, ApproveRequest())
        await orchestrator.execute(state.run_id)
    return str(orchestrator.runs_dir / state.run_id)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Risalite area research agent.")
    parser.add_argument("question")
    parser.add_argument("--mock", action="store_true")
    args = parser.parse_args()
    print(asyncio.run(run(args.question, args.mock)))


if __name__ == "__main__":
    main()
