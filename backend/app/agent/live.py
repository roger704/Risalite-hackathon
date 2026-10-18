import os
from pathlib import Path

from app.agent.events import EventBus
from app.agent.pipeline import Orchestrator
from app.agent.stand_ins import FixtureCellScorer, PassThroughFactChecker
from app.connectors.nebius import NebiusConfig, NebiusGateway
from app.connectors.tavily import TavilyClient, TavilyConfig
from app.domain.research import ResearchBudget


class NebiusModelClient:
    records_budget = True

    def __init__(self, gateway: NebiusGateway, model: str):
        self.gateway = gateway
        self.model = model

    def model_name(self, role: str) -> str:
        return self.model

    async def structured(self, role, operation, context, output_model, evidence=()):
        before = self.gateway.budget.tokens_used
        result = await getattr(self.gateway, operation)(context, output_model, evidence)
        return result, self.gateway.budget.tokens_used - before


def default_runs_dir() -> Path:
    configured = os.environ.get("RISALITE_RUNS_DIR")
    return (
        Path(configured).expanduser()
        if configured
        else Path(__file__).resolve().parents[3] / "runs"
    )


def build_orchestrator(
    runs_dir: Path | str | None = None, mock: bool | None = None
) -> Orchestrator:
    from app.agent.mock import MockModelClient, MockResearch

    use_mock = mock if mock is not None else os.environ.get("RISALITE_MOCK") == "1"
    run_path = Path(runs_dir) if runs_dir is not None else default_runs_dir()

    def budget_factory():
        return ResearchBudget(12, 10, 12, 60000)

    if use_mock:
        model = MockModelClient()
        research = MockResearch()
    else:
        model = None
        research = None
        budget = budget_factory()
        if os.environ.get("NEBIUS_API_KEY") and os.environ.get("NEBIUS_MODEL"):
            config = NebiusConfig.from_env()
            model = NebiusModelClient(NebiusGateway(config, budget), config.model)
        if os.environ.get("TAVILY_API_KEY"):
            research = TavilyClient(TavilyConfig.from_env(), budget)
    return Orchestrator(
        runs_dir=run_path,
        bus=EventBus(),
        model=model,
        research=research,
        scorer_factory=FixtureCellScorer,
        fact_checker=PassThroughFactChecker(),
        budget_factory=budget_factory,
    )
