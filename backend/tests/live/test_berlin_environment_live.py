"""Opt-in bounded contract checks against each reviewed Umweltgerechtigkeit WFS layer."""

import json
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import pytest

from app.connectors.berlin_wfs import page_url, parse_page
from app.domain import DatasetConfig
from app.infrastructure.http import HTTPFetcher

CONFIG = Path(__file__).parents[2] / "config/datasets/berlin_environment.json"


@pytest.mark.live
async def test_berlin_environment_live_layers_match_reviewed_schema(tmp_path):
    values = json.loads(CONFIG.read_text())
    values["allowed_hosts"] = tuple(values["allowed_hosts"])
    config = DatasetConfig(**values)
    fetcher = HTTPFetcher()

    for layer in config.parser_options["layers"]:
        query = [
            (key, value)
            for key, value in parse_qsl(urlsplit(config.resource_url).query)
            if key.upper() != "TYPENAMES"
        ]
        query.append(("TYPENAMES", layer["typename"]))
        url = urlunsplit(
            (
                urlsplit(config.resource_url).scheme,
                urlsplit(config.resource_url).netloc,
                urlsplit(config.resource_url).path,
                urlencode(query),
                "",
            )
        )
        request_url = page_url(url, 0, 1)
        asset = await fetcher.download(
            replace(config, resource_url=request_url),
            tmp_path / f"{layer['typename'].split(':')[-1]}-live.json",
        )
        collection = parse_page(
            Path(asset.path).read_bytes(),
            {"expected_crs": config.parser_options["expected_crs"]},
        )
        assert collection["numberMatched"] == 542
        assert len(collection["features"]) == 1
        feature = collection["features"][0]
        properties = feature["properties"]
        assert set(layer["expected_properties"]).issubset(properties)

        validation = layer.get("validation")
        if validation:
            known_categories = set(validation["count_labels"].values())
        else:
            known_categories = {
                item["label"]
                for indicator in layer["indicators"]
                if indicator["source_field"] == "kategorie"
                for item in indicator["classes"]
            }
        known_markers = {
            marker
            for indicator in layer["indicators"]
            if indicator["source_field"] == "kategorie"
            for marker in indicator.get("missing_markers", {})
        }
        category = properties["kategorie"]
        assert (
            category in known_categories
            or (category is None and "null" in known_markers)
            or (category == "-" and "-" in known_markers)
        )
