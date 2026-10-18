"""Bounded, opt-in schema smoke test for Berlin development-plan WFS layers."""

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from app.connectors.berlin_wfs import page_url, parse_page
from app.domain import DatasetConfig
from app.infrastructure.http import HTTPFetcher

CONFIG = Path(__file__).parents[2] / "config/datasets/berlin_planning.json"


@pytest.mark.live
async def test_berlin_planning_live_two_features_per_layer(tmp_path):
    if os.environ.get("RUN_LIVE_TESTS") != "1":
        pytest.skip("Set RUN_LIVE_TESTS=1 to contact the Berlin WFS")
    values = json.loads(CONFIG.read_text())
    values["allowed_hosts"] = tuple(values["allowed_hosts"])
    config = DatasetConfig(**values)
    options = config.parser_options
    fetcher = HTTPFetcher()
    for layer in options["layers"]:
        parts = config.resource_url.split("TYPENAMES=", 1)
        resource_url = parts[0] + "TYPENAMES=" + layer["typename"]
        if "&" in parts[1]:
            resource_url += "&" + parts[1].split("&", 1)[1]
        page = page_url(resource_url, 0, 2)
        asset = await fetcher.download(
            replace(config, resource_url=page),
            tmp_path / f"{layer['typename'].split(':')[-1]}.json",
        )
        collection = parse_page(Path(asset.path).read_bytes(), options)
        assert len(collection["features"]) == 2
        assert layer["expected_count_range"][0] <= collection["numberMatched"]
        assert collection["numberMatched"] <= layer["expected_count_range"][1]
        for feature in collection["features"]:
            properties = feature["properties"]
            assert set(layer["expected_properties"]).issubset(properties)
            assert properties["bp_rechtsstand"] in options["status_map"]
