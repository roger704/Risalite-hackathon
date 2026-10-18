"""Opt-in smoke test against the reviewed gdi.berlin ``gruenanlagen`` WFS (one feature per layer)."""

import hashlib
import json
import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.connectors.berlin_green_spaces import BerlinGreenSpacesConnector
from app.connectors.berlin_wfs import page_url, parse_page
from app.domain import DatasetConfig, RawAsset, RawBundle
from app.infrastructure.http import HTTPFetcher

CONFIG = Path(__file__).parents[2] / "config/datasets/berlin_parks.json"

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_TESTS") != "1", reason="set RUN_LIVE_TESTS=1 for provider checks"
)


@pytest.mark.live
async def test_berlin_parks_live_layers_match_reviewed_schema(tmp_path):
    values = json.loads(CONFIG.read_text())
    values["allowed_hosts"] = tuple(values["allowed_hosts"])
    config = DatasetConfig(**values)
    options = config.parser_options
    assets = []
    for layer in options["layers"]:
        url = page_url(layer["resource_url"], 0, 1)
        asset = await HTTPFetcher().download(
            replace(config, resource_url=url), tmp_path / f"{layer['feature_type']}.json"
        )
        collection = parse_page(Path(asset.path).read_bytes(), options)
        low, high = layer["expected_count_range"]
        assert low <= collection["numberMatched"] <= high
        assert len(collection["features"]) == 1
        assert set(layer["expected_properties"]).issubset(collection["features"][0]["properties"])
        single = dict(collection, numberMatched=1)
        path = tmp_path / f"{layer['feature_type']}-single.json"
        path.write_text(json.dumps(single))
        assets.append(
            RawAsset(
                path=str(path),
                sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                size_bytes=path.stat().st_size,
                resource_url=url,
            )
        )
    single_config = replace(
        config,
        parser_options={
            **options,
            "layers": [{**layer, "expected_count_range": [1, 1]} for layer in options["layers"]],
        },
    )
    batch = BerlinGreenSpacesConnector().normalize(
        RawBundle(
            assets=tuple(assets),
            retrieved_at=datetime.now(timezone.utc),
            snapshot_id="live-parks",
            request_fingerprint="live",
        ),
        single_config,
    )
    assert [r.feature_type for r in batch.records] == ["green_space", "playground"]
    assert [r.original_category for r in batch.records] == ["Grünanlage", "Spielplatz"]
    assert all(r.geometry["type"] in {"Polygon", "MultiPolygon"} for r in batch.records)
