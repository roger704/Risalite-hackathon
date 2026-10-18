"""Opt-in smoke test against the reviewed Berlin WFS endpoint."""

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from shapely.geometry import shape

from app.connectors.berlin_wfs import BerlinWFSConnector, page_url, parse_page
from app.domain import DatasetConfig, RawAsset, RawBundle
from app.infrastructure.http import HTTPFetcher

CONFIG = Path(__file__).parents[2] / "config/datasets/berlin_lor_names.json"


@pytest.mark.live
async def test_berlin_wfs_live_single_feature_maps_to_berlin_area(tmp_path):
    values = json.loads(CONFIG.read_text())
    values["allowed_hosts"] = tuple(values["allowed_hosts"])
    config = DatasetConfig(**values)
    url = page_url(config.resource_url, 0, 1)
    asset = await HTTPFetcher().download(
        replace(config, resource_url=url), tmp_path / "live-page.json"
    )
    collection = parse_page(Path(asset.path).read_bytes(), config.parser_options)
    assert collection["numberMatched"] == 542
    assert len(collection["features"]) == 1
    properties = collection["features"][0]["properties"]
    assert set(config.parser_options["expected_properties"]).issubset(properties)

    one_feature = dict(collection, numberMatched=1, features=collection["features"][:1])
    single_path = tmp_path / "single-feature.json"
    single_path.write_text(json.dumps(one_feature))
    single_asset = RawAsset(
        path=str(single_path),
        sha256=hashlib.sha256(single_path.read_bytes()).hexdigest(),
        size_bytes=single_path.stat().st_size,
        resource_url=url,
    )
    single_config = replace(
        config,
        parser_options={**config.parser_options, "expected_count": 1},
    )
    batch = BerlinWFSConnector().normalize(
        RawBundle(
            assets=(single_asset,),
            retrieved_at=datetime.now(timezone.utc),
            snapshot_id="live-single-feature",
            request_fingerprint="live",
        ),
        single_config,
    )
    assert len(batch.records) == 1
    geometry = shape(batch.records[0].geometry)
    west, south, east, north = geometry.bounds
    assert 13.0 <= west <= east <= 13.9
    assert 52.3 <= south <= north <= 52.8
