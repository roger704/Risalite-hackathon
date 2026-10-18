"""Opt-in bounded smoke test for the reviewed Berlin ALKIS WFS service."""

import json
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import pytest
from shapely.geometry import shape

from app.connectors.berlin_alkis_parcels import BerlinALKISParcelsConnector
from app.domain import DatasetConfig, FetchRequest

CONFIG = Path(__file__).parents[2] / "config/datasets/berlin_alkis_parcels.json"
LIVE_BBOX = [389000, 5818000, 389100, 5818100]


@pytest.mark.live
async def test_berlin_alkis_parcels_live_bounded_wfs_pages_and_normalizes(tmp_path):
    values = json.loads(CONFIG.read_text())
    values["allowed_hosts"] = tuple(values["allowed_hosts"])
    options = dict(values["parser_options"])
    options.update(
        extent_bbox=LIVE_BBOX,
        page_size=10,
        max_pages=10,
        expected_count_range=[1, 100],
    )
    parts = urlsplit(values["resource_url"])
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["BBOX"] = ",".join(map(str, LIVE_BBOX)) + ",urn:ogc:def:crs:EPSG::25833"
    values["resource_url"] = urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )
    config = DatasetConfig(**{**values, "parser_options": options})
    connector = BerlinALKISParcelsConnector()
    raw = await connector.fetch(
        FetchRequest(
            config=config,
            staging_dir=str(tmp_path / "staging"),
            snapshot_id="live-alkis-parcels",
        )
    )
    batch = connector.normalize(raw, config)

    assert 1 <= len(batch.records) <= 100
    assert len(raw.assets) >= 2
    assert all(record.record_type == "parcel" for record in batch.records)
    assert len({record.source_record_id for record in batch.records}) == len(batch.records)
    assert len({record.parcel_id for record in batch.records}) == len(batch.records)
    assert all(record.cadastral_district_code for record in batch.records)
    assert all(shape(record.geometry).geom_type == "MultiPolygon" for record in batch.records)
    assert connector.validate(batch).valid
