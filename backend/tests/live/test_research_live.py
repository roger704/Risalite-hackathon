import os

import pytest

from app.application.research_outputs import ResearchPlan
from app.connectors.nebius import NebiusConfig, NebiusGateway
from app.connectors.tavily import TavilyClient, TavilyConfig
from app.domain.research import ResearchBudget, ResearchSearch


@pytest.mark.live
@pytest.mark.asyncio
@pytest.mark.skipif(not os.getenv("TAVILY_API_KEY"), reason="TAVILY_API_KEY is not configured")
async def test_live_tavily_search():
    connector = TavilyClient(
        TavilyConfig.from_env(max_results=1),
        ResearchBudget(max_searches=1, max_extract_urls=0, max_inference_calls=0, max_tokens=0),
    )
    async with connector:
        page = await connector.search(ResearchSearch("official Berlin city information"))
    assert page.evidence
    assert len(page.evidence) <= 1


@pytest.mark.live
@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("NEBIUS_API_KEY") or not os.getenv("NEBIUS_MODEL"),
    reason="NEBIUS_API_KEY and NEBIUS_MODEL are not configured",
)
async def test_live_nebius_plan():
    gateway = NebiusGateway(
        NebiusConfig.from_env(max_completion_tokens=256, max_schema_retries=0),
        ResearchBudget(max_searches=0, max_extract_urls=0, max_inference_calls=1, max_tokens=256),
    )
    async with gateway:
        result = await gateway.plan(
            {
                "candidates": [{"candidate_id": "berlin-example"}],
                "gaps": ["current public transport accessibility"],
            },
            ResearchPlan,
        )
    assert result.candidates
