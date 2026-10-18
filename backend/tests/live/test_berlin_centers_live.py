"""Opt-in bounded smoke test for the two StEP Zentren 2040 WFS layers."""

import hashlib
import json
import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.connectors.berlin_centers import BerlinCentersConnector
from app.connectors.berlin_wfs import page_url, parse_page
from app.domain import DatasetConfig, RawBundle
from app.infrastructure.http import HTTPFetcher

CONFIG = Path(__file__).parents[2] / "config/datasets/berlin_centers.json"


@pytest.mark.live
@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("RUN_LIVE_TESTS") != "1", reason="set RUN_LIVE_TESTS=1 to contact WFS"
)
async def test_berlin_centers_live_requests_one_feature_per_layer_and_normalizes(tmp_path):
    values = json.loads(CONFIG.read_text())
    values["allowed_hosts"] = tuple(values["allowed_hosts"])
    config = DatasetConfig(**values)
    assets = []
    for key, layer in config.parser_options["layers"].items():
        url = page_url(layer["resource_url"], 0, 1)
        single_config = replace(config, resource_url=url)
        asset = await HTTPFetcher().download(single_config, tmp_path / f"{key}-live.json")
        collection = parse_page(
            Path(asset.path).read_bytes(), {"expected_crs": config.parser_options["expected_crs"]}
        )
        minimum, maximum = layer["expected_count_range"]
        assert minimum <= collection["numberMatched"] <= maximum
        assert len(collection["features"]) == 1
        feature = collection["features"][0]
        assert set(feature["properties"]) == set(layer["expected_properties"])
        one_feature = dict(
            collection,
            numberMatched=1,
            numberReturned=1,
            totalFeatures=1,
            features=[feature],
        )
        Path(asset.path).write_text(json.dumps(one_feature, ensure_ascii=False))
        assets.append(
            replace(
                asset,
                sha256=hashlib.sha256(Path(asset.path).read_bytes()).hexdigest(),
                size_bytes=Path(asset.path).stat().st_size,
                resource_url=layer["resource_url"],
            )
        )
    layer_options = {
        key: {**value, "expected_count_range": [1, 1]}
        for key, value in config.parser_options["layers"].items()
    }
    single_config = replace(
        config,
        parser_options={**config.parser_options, "layers": layer_options},
    )
    bundle = RawBundle(
        assets=tuple(assets),
        retrieved_at=datetime.now(timezone.utc),
        snapshot_id="berlin-centers-live",
        request_fingerprint="live-single-features",
    )
    batch = BerlinCentersConnector().normalize(bundle, single_config)
    assert len(batch.records) == 2
