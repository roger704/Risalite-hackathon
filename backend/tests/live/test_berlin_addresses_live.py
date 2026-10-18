"""Opt-in smoke tests for the reviewed official Berlin address WFS."""

import hashlib
import json
import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import pytest
from shapely.geometry import shape

from app.connectors.berlin_addresses import BerlinAddressConnector
from app.connectors.berlin_wfs import page_url, parse_page
from app.domain import DatasetConfig, RawAsset, RawBundle
from app.infrastructure.http import HTTPFetcher

CONFIG = Path(__file__).parents[2] / "config/datasets/berlin_addresses.json"


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("RUN_LIVE_TESTS") != "1",
    reason="set RUN_LIVE_TESTS=1 to contact Berlin WFS",
)
async def test_berlin_addresses_live_single_feature_and_fes_filter(tmp_path):
    values = json.loads(CONFIG.read_text())
    values["allowed_hosts"] = tuple(values["allowed_hosts"])
    config = DatasetConfig(**values)
    fetcher = HTTPFetcher()

    url = page_url(config.resource_url, 0, 1)
    asset = await fetcher.download(replace(config, resource_url=url), tmp_path / "live-page.json")
    collection = parse_page(Path(asset.path).read_bytes(), config.parser_options)
    options = config.parser_options
    assert (
        options["expected_count_range"][0]
        <= collection["numberMatched"]
        <= options["expected_count_range"][1]
    )
    assert len(collection["features"]) == 1
    assert set(collection["features"][0]["properties"]) == set(options["expected_properties"])

    one_feature = dict(collection, numberMatched=1, features=collection["features"][:1])
    single_path = tmp_path / "single-feature.json"
    single_path.write_text(json.dumps(one_feature))
    single_asset = RawAsset(
        path=str(single_path),
        sha256=hashlib.sha256(single_path.read_bytes()).hexdigest(),
        size_bytes=single_path.stat().st_size,
        resource_url=url,
    )
    single_options = dict(options)
    single_options.pop("expected_count_range")
    single_options["expected_count"] = 1
    single_config = replace(config, parser_options=single_options)
    batch = BerlinAddressConnector().normalize(
        RawBundle(
            assets=(single_asset,),
            retrieved_at=datetime.now(timezone.utc),
            snapshot_id="live-single-address",
            request_fingerprint="live",
        ),
        single_config,
    )
    assert len(batch.records) == 1
    west, south, east, north = shape(batch.records[0].geometry).bounds
    assert 13.0 <= west <= east <= 13.9
    assert 52.3 <= south <= north <= 52.8

    filter_xml = (
        '<fes:Filter xmlns:fes="http://www.opengis.net/fes/2.0"><fes:And>'
        "<fes:PropertyIsEqualTo><fes:ValueReference>str_name</fes:ValueReference>"
        "<fes:Literal>Wiener Straße</fes:Literal></fes:PropertyIsEqualTo>"
        "<fes:PropertyIsEqualTo><fes:ValueReference>hnr</fes:ValueReference>"
        "<fes:Literal>12</fes:Literal></fes:PropertyIsEqualTo>"
        "</fes:And></fes:Filter>"
    )
    filtered_url = page_url(config.resource_url, 0, 100)
    parts = urlsplit(filtered_url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query.append(("FILTER", filter_xml))
    filtered_url = urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )
    filtered = await fetcher.download(
        replace(config, resource_url=filtered_url), tmp_path / "filtered-page.json"
    )
    filtered_collection = parse_page(Path(filtered.path).read_bytes(), options)
    features = filtered_collection["features"]
    assert features
    assert any(
        feature["properties"]["str_name"] == "Wiener Straße"
        and feature["properties"]["hnr"] == 12
        and feature["properties"]["plz"] == "10999"
        for feature in features
    )
