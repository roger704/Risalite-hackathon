"""Opt-in full-file smoke test for the reviewed Zensus download."""

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.connectors.zensus import ZensusGridConnector
from app.domain import RawBundle
from app.infrastructure.http import HTTPFetcher

CONFIG = Path(__file__).parents[2] / "config/datasets/zensus_grid.json"
EXPECTED_SHA256 = "1a8428d31eab264c8717bfdcbf40fbd66c217edb2e2a98d0e67741bd8a1a941a"


@pytest.mark.live
async def test_zensus_grid_live_download_and_normalize(tmp_path):
    if os.environ.get("RUN_LIVE_TESTS") != "1":
        pytest.skip("Set RUN_LIVE_TESTS=1 to download the pinned Zensus ZIP")
    import json

    from app.domain import DatasetConfig

    values = json.loads(CONFIG.read_text())
    values["allowed_hosts"] = tuple(values["allowed_hosts"])
    config = DatasetConfig(**values)
    asset = await HTTPFetcher().download(config, tmp_path / "zensus.zip")
    assert asset.sha256 == EXPECTED_SHA256
    raw = RawBundle(
        assets=(asset,),
        retrieved_at=datetime.now(timezone.utc),
        snapshot_id="live-zensus-grid",
        request_fingerprint="live",
    )
    batch = ZensusGridConnector().normalize(raw, config)
    assert len(batch.records) >= 50000
    assert all(record.resolution_m == 100 for record in batch.records)
