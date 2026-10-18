"""Opt-in live completeness smoke test for Berlin's ALKIS building WFS."""

import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
import pytest
from shapely.geometry import shape

from app.connectors.berlin_alkis_buildings import BerlinALKISBuildingsConnector
from app.connectors.berlin_wfs import page_url, parse_page
from app.domain import DatasetConfig, FetchRequest

BACKEND = Path(__file__).parents[2]
CONFIG = BACKEND / "config/datasets/berlin_alkis_buildings.json"


def _replace_filter(url: str, value: str) -> str:
    parts = urlsplit(url)
    query = [
        (key, item)
        for key, item in parse_qsl(parts.query, keep_blank_values=True)
        if key.upper() != "CQL_FILTER"
    ]
    query.append(("CQL_FILTER", value))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


@pytest.mark.live
@pytest.mark.skipif(os.getenv("RUN_LIVE_TESTS") != "1", reason="set RUN_LIVE_TESTS=1")
def test_live_extent_hits_and_single_page_normalization(tmp_path):
    values = json.loads(CONFIG.read_text())
    values["allowed_hosts"] = tuple(values["allowed_hosts"])
    config = DatasetConfig(**values)
    options = config.parser_options

    async def get_collection(url):
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
            return parse_page(response.content, options)

    async def get_hits(url):
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
            collection = response.json()
            assert collection.get("type") == "FeatureCollection"
            assert type(collection.get("numberMatched")) is int
            return collection

    async def run():
        hit_collection = await get_hits(page_url(config.resource_url, 0, 0))
        expected_min, expected_max = options["expected_count_range"]
        hit_count = hit_collection["numberMatched"]
        assert expected_min <= hit_count <= expected_max

        sample_collection = await get_collection(page_url(config.resource_url, 0, 1))
        sample_geometry = shape(sample_collection["features"][0]["geometry"])
        center = sample_geometry.representative_point()
        bbox = [
            round(center.x - 2, 2),
            round(center.y - 2, 2),
            round(center.x + 2, 2),
            round(center.y + 2, 2),
        ]
        small_extent = {
            **options["extent"],
            "name": "live_single_page_sample",
            "bbox_25833": bbox,
        }
        small_filter = (
            f"bezeich='AX_Gebaeude' AND BBOX(geom,{','.join(str(value) for value in bbox)})"
        )
        small_options = {
            **options,
            "extent": small_extent,
            "expected_count_range": [1, options["page_size"]],
            "max_pages": 1,
        }
        small_config = replace(
            config,
            resource_url=_replace_filter(config.resource_url, small_filter),
            parser_options=small_options,
        )
        small_hits = await get_hits(page_url(small_config.resource_url, 0, 0))
        small_count = small_hits["numberMatched"]
        assert 1 <= small_count <= options["page_size"]

        connector = BerlinALKISBuildingsConnector()
        request = FetchRequest(
            config=small_config,
            staging_dir=str(tmp_path / "staging"),
            snapshot_id="live-alkis-buildings",
        )
        bundle = await connector.fetch(request)
        batch = connector.normalize(bundle, small_config)
        assert len(batch.records) == small_count
        assert len(batch.records) == len(set(record.source_record_id for record in batch.records))
        assert len(bundle.assets) == 1
        assert connector.validate(batch).valid

    asyncio.run(run())
