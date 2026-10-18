"""Opt-in integration test against the pinned Berlin forecast workbooks."""

import asyncio
import json
import os
from pathlib import Path

import pytest

from app.connectors.population_forecast import PopulationForecastConnector
from app.domain import DatasetConfig, FetchRequest

BACKEND = Path(__file__).parents[2]
CONFIG = BACKEND / "config/datasets/berlin_population_forecast.json"


@pytest.mark.live
@pytest.mark.skipif(os.environ.get("RUN_LIVE_TESTS") != "1", reason="set RUN_LIVE_TESTS=1")
@pytest.mark.asyncio
async def test_population_forecast_live_pinned_workbooks_normalize(tmp_path):
    values = json.loads(CONFIG.read_text())
    values["allowed_hosts"] = tuple(values["allowed_hosts"])
    config = DatasetConfig(**values)
    assert config.timeout_seconds <= 60
    connector = PopulationForecastConnector()
    raw = await asyncio.wait_for(
        connector.fetch(
            FetchRequest(
                config=config,
                staging_dir=str(tmp_path),
                snapshot_id="live-forecast",
            )
        ),
        timeout=60,
    )
    pins = config.parser_options["assets"]
    assert [asset.sha256 for asset in raw.assets] == [pin["sha256"] for pin in pins]
    assert [asset.size_bytes for asset in raw.assets] == [pin["size_bytes"] for pin in pins]
    batch = connector.normalize(raw, config)
    assert connector.validate(batch).valid
    assert batch.rejected_rows == 0
    assert {record.scenario for record in batch.records} == {"middle", "upper", "lower"}
    assert (
        len({record.area_code for record in batch.records if record.hierarchy == "prognoseraum"})
        == 58
    )
