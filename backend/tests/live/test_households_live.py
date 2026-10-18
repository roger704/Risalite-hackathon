"""Opt-in end-to-end normalization of the pinned official household workbook."""

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.connectors.households import HouseholdsConnector
from app.domain import DatasetConfig, RawBundle
from app.infrastructure.http import HTTPFetcher

CONFIG = Path(__file__).parents[2] / "config/datasets/berlin_households.json"


@pytest.mark.live
async def test_households_live_pinned_workbook_normalizes_official_totals(tmp_path):
    if os.environ.get("RUN_LIVE_TESTS") != "1":
        pytest.skip("Set RUN_LIVE_TESTS=1 to download the official workbook")
    config = DatasetConfig(**json.loads(CONFIG.read_text()))
    asset = await HTTPFetcher().download(config, tmp_path / "official-households.xlsx")
    bundle = RawBundle(
        assets=(asset,),
        retrieved_at=datetime.now(timezone.utc),
        snapshot_id="live-households",
        request_fingerprint=hashlib.sha256(asset.sha256.encode()).hexdigest(),
    )
    connector = HouseholdsConnector()
    batch = connector.normalize(bundle, config)
    assert not batch.rejections
    assert connector.validate(batch).valid
    assert len(batch.records) == 542 * 27
    totals = {}
    for record in batch.records:
        if record.metric_code in {
            "households_total",
            "households_with_children",
        }:
            totals[record.metric_code] = totals.get(record.metric_code, 0) + record.value
    assert totals == {
        "households_total": 2_231_314,
        "households_with_children": 376_366,
    }
