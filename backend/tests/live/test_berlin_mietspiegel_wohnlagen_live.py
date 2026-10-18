"""Opt-in smoke check for the reviewed Mietspiegel 2026 WFS layer."""

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from app.connectors.berlin_wfs import page_url, parse_page
from app.domain import DatasetConfig
from app.infrastructure.http import HTTPFetcher

CONFIG = Path(__file__).parents[2] / "config/datasets/berlin_mietspiegel_wohnlagen.json"


@pytest.mark.live
@pytest.mark.skipif(os.environ.get("RUN_LIVE_TESTS") != "1", reason="opt-in live test")
async def test_berlin_mietspiegel_wohnlagen_live_single_feature(tmp_path):
    values = json.loads(CONFIG.read_text())
    values["allowed_hosts"] = tuple(values["allowed_hosts"])
    config = DatasetConfig(**values)
    url = page_url(config.resource_url, 0, 1)
    asset = await HTTPFetcher().download(
        replace(config, resource_url=url), tmp_path / "wohnlage-live-page.json"
    )
    collection = parse_page(Path(asset.path).read_bytes(), config.parser_options)

    assert 380_000 <= collection["numberMatched"] <= 430_000
    assert len(collection["features"]) == 1
    feature = collection["features"][0]
    assert set(feature["properties"]) == set(config.parser_options["expected_properties"])
    assert feature["properties"]["wol"] in config.parser_options["classes"]
    key = feature["properties"]["schluessel"]
    house_number = feature["properties"]["hnr"]
    assert key[5:] == house_number
    assert feature["id"] == f"{config.parser_options['typename'].split(':')[-1]}.{key}"
