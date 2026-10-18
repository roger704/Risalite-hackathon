"""Opt-in bounded smoke test against the official Kita WFS (kita:kita)."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from app.connectors.berlin_wfs import page_url, parse_page
from app.domain import DatasetConfig
from app.infrastructure.http import HTTPFetcher

CONFIG = Path(__file__).parents[2] / "config/datasets/berlin_kitas.json"


@pytest.mark.live
async def test_berlin_kitas_live_first_page_matches_reviewed_mapping(tmp_path):
    values = json.loads(CONFIG.read_text())
    values["allowed_hosts"] = tuple(values["allowed_hosts"])
    config = DatasetConfig(**values)
    url = page_url(config.resource_url, 0, 5)
    asset = await HTTPFetcher().download(replace(config, resource_url=url), tmp_path / "page.json")
    collection = parse_page(Path(asset.path).read_bytes(), config.parser_options)
    low, high = config.parser_options["expected_count_range"]
    assert low <= collection["numberMatched"] <= high
    assert len(collection["features"]) == 5
    for feature in collection["features"]:
        assert feature["geometry"]["type"] == "Point"
        properties = feature["properties"]
        assert set(config.parser_options["expected_properties"]) == set(properties)
        assert properties["e_art"] in config.parser_options["category_mapping"]
