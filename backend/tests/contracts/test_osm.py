"""OSM connector contracts for synthetic XML, generated PBF, and Overpass JSON."""

import hashlib
import json
import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import httpx
import osmium
import pytest
from shapely.geometry import Polygon, shape

from app.connectors.osm import OSMConnector
from app.domain import ConfigError, DatasetConfig, FetchRequest, RawAsset, RawBundle, SchemaError
from app.infrastructure.http import HTTPFetcher

BACKEND = Path(__file__).parents[2]
FIXTURES = Path(__file__).parents[1] / "fixtures/osm"


def configuration(dataset="osm_pois", **overrides):
    values = json.loads((BACKEND / f"config/datasets/{dataset}.json").read_text())
    options = dict(values["parser_options"])
    options.update(min_expected_counts={"coffee_shop": 2}, max_unlocatable_ratio=0.2)
    options.update(overrides)
    values["parser_options"] = options
    return DatasetConfig(**values)


def bundle(path, snapshot_id="synthetic-osm"):
    return RawBundle(
        assets=(
            RawAsset(
                path=str(path),
                sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                size_bytes=path.stat().st_size,
                resource_url="https://example.org/synthetic-osm",
            ),
        ),
        retrieved_at=datetime.now(timezone.utc),
        snapshot_id=snapshot_id,
        request_fingerprint="synthetic-osm",
    )


def make_pbf(tmp_path):
    target = tmp_path / "pois.osm.pbf"
    writer = osmium.SimpleWriter(str(target))
    try:
        for obj in osmium.FileProcessor(FIXTURES / "pois.osm"):
            writer.add(obj)
    finally:
        writer.close()
    return target


@pytest.mark.parametrize("source_kind", ["xml", "pbf"])
def test_osm_ids_geometry_categories_and_quality(source_kind, tmp_path):
    source = FIXTURES / "pois.osm" if source_kind == "xml" else make_pbf(tmp_path)
    connector = OSMConnector()
    batch = connector.normalize(bundle(source), configuration())
    report = connector.validate(batch)
    records = {record.record_id: record for record in batch.records}

    assert report.valid
    assert report.rejected_rows == 0
    assert "node/10" in records and "way/10" in records
    assert records["node/10"].source_record_id == records["node/10"].record_id
    assert records["way/10"].source_record_id == records["way/10"].record_id
    assert records["way/10"].source_geometry_type == "way_polygon"
    polygon = Polygon(
        [
            (13.400, 52.520),
            (13.401, 52.520),
            (13.401, 52.521),
            (13.400, 52.521),
            (13.400, 52.520),
        ]
    )
    assert polygon.contains(shape(records["way/10"].geometry))
    assert records["way/30"].source_geometry_type == "way_line"
    assert records["relation/50"].source_geometry_type == "relation_multipolygon"
    assert records["relation/50"].quality_flags == ()
    relation_polygon = Polygon(
        [
            (13.400, 52.532),
            (13.401, 52.532),
            (13.401, 52.533),
            (13.400, 52.533),
            (13.400, 52.532),
        ]
    )
    assert relation_polygon.contains(shape(records["relation/50"].geometry))
    assert records["relation/60"].source_geometry_type == "relation_unassembled"
    assert set(records["relation/60"].quality_flags) >= {
        "geometry_assembly_failed",
        "position_from_member_coordinates",
    }
    assert records["relation/80"].source_geometry_type == "relation_unassembled"
    assert "geometry_assembly_failed" in records["relation/80"].quality_flags
    assert "relation/70" not in records
    assert any(
        "1 selected elements had no locatable geometry: relation/70" in w for w in report.warnings
    )
    assert batch.rejected_rows == 0

    assert records["node/2"].canonical_category == "coffee_shop"
    assert records["node/2"].original_category == "cuisine=coffee_shop"
    assert records["node/3"].canonical_category == "coffee_retail"
    assert records["node/4"].canonical_category == "bakery"
    assert records["node/5"].canonical_category is None
    assert records["node/5"].original_category == "amenity=cafe;bar"
    assert "category_unmapped" in records["node/5"].quality_flags
    assert "node/6" not in records
    assert records["node/1"].name == "Synthetic Café"
    assert records["node/1"].attributes["opening_hours"] == "Mo-Fr 08:00-18:00"
    assert records["node/1"].attributes["website"] == "https://example.org/cafe"
    assert "phone" not in records["node/1"].attributes
    assert "addr:street" not in records["node/1"].attributes
    assert all(record.category_mapping_version == "osm_v1" for record in records.values())
    category_line = next(w for w in report.warnings if w.startswith("category_counts:"))
    assert "coffee_shop=8" in category_line
    assert "coffee_retail=1" in category_line
    assert "unmapped=1" in category_line
    assert any("geometry_assembly_failed: 2" == w for w in report.warnings)


def test_minimum_count_empty_result_and_out_of_bbox_are_invalid(tmp_path):
    source = FIXTURES / "pois.osm"
    connector = OSMConnector()
    batch = connector.normalize(
        bundle(source), configuration(min_expected_counts={"coffee_shop": 9})
    )
    report = connector.validate(batch)
    assert not report.valid
    assert any("coffee_shop count 8 is below minimum 9" in error for error in report.errors)

    empty_source = tmp_path / "empty.osm"
    empty_source.write_text(
        '<osm version="0.6"><node id="1" lat="52.5" lon="13.4">'
        '<tag k="amenity" v="bench"/></node></osm>'
    )
    empty_connector = OSMConnector()
    empty_batch = empty_connector.normalize(bundle(empty_source), configuration())
    empty_report = empty_connector.validate(empty_batch)
    assert not empty_report.valid
    assert any("no usable places" in error for error in empty_report.errors)

    outside_source = tmp_path / "outside.osm"
    outside_source.write_text(
        (FIXTURES / "pois.osm").read_text().replace('lon="13.40500"', 'lon="14.00000"', 1)
    )
    outside_connector = OSMConnector()
    outside_batch = outside_connector.normalize(bundle(outside_source), configuration())
    outside_report = outside_connector.validate(outside_batch)
    assert not outside_report.valid
    assert any("node/1 is outside allowed_bbox" in error for error in outside_report.errors)


def test_mapping_hash_is_enforced():
    config = configuration()
    mismatch = replace(
        config,
        parser_options={**config.parser_options, "category_mapping_sha256": "0" * 64},
    )
    with pytest.raises(ConfigError, match="SHA-256 mismatch"):
        OSMConnector().normalize(bundle(FIXTURES / "pois.osm"), mismatch)


def test_overpass_elements_centers_remark_and_timestamp(tmp_path):
    connector = OSMConnector()
    config = configuration("osm_pois_overpass", max_unlocatable_ratio=0.2)
    batch = connector.normalize(bundle(FIXTURES / "overpass.json"), config)
    report = connector.validate(batch)
    records = {record.record_id: record for record in batch.records}
    assert report.valid
    assert "node/101" in records and "way/101" in records
    assert records["node/101"].source_geometry_type == "node"
    assert records["way/101"].source_geometry_type == "way_center"
    assert "position_from_overpass_center" in records["way/101"].quality_flags
    assert records["way/101"].canonical_category == "coffee_retail"
    assert records["relation/202"].canonical_category == "coffee_shop"
    assert any("timestamp_osm_base: 2026-10-18T04:47:00Z" in w for w in report.warnings)

    with pytest.raises(SchemaError, match="contains a remark"):
        connector.normalize(bundle(FIXTURES / "overpass_remark.json"), config)
    malformed = tmp_path / "overpass_no_elements.json"
    malformed.write_text('{"version": 0.6}')
    try:
        with pytest.raises(SchemaError, match="missing its elements"):
            connector.normalize(bundle(malformed), config)
    finally:
        malformed.unlink()


@pytest.mark.asyncio
async def test_overpass_fetch_uses_the_reviewed_pinned_url(tmp_path):
    config = configuration("osm_pois_overpass")
    urls = []

    def respond(request):
        urls.append(str(request.url))
        return httpx.Response(200, json={"elements": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        connector = OSMConnector(fetcher=HTTPFetcher(client))
        await connector.fetch(
            FetchRequest(config=config, staging_dir=str(tmp_path), snapshot_id="overpass-test")
        )
    assert urls == [config.resource_url]


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("RUN_LIVE_TESTS") != "1",
    reason="set RUN_LIVE_TESTS=1 to contact Geofabrik",
)
@pytest.mark.asyncio
async def test_geofabrik_pinned_source_supports_a_bounded_range_request():
    config = configuration()
    async with httpx.AsyncClient(follow_redirects=True, timeout=40) as client:
        async with client.stream(
            "GET", config.resource_url, headers={"Range": "bytes=0-63"}
        ) as response:
            assert response.status_code in {200, 206}
            content_range = response.headers.get("content-range")
            total = (
                int(content_range.rsplit("/", 1)[1])
                if content_range and "/" in content_range
                else int(response.headers.get("content-length", "0"))
            )
            prefix = b""
            async for chunk in response.aiter_bytes():
                prefix += chunk[: max(0, 64 - len(prefix))]
                if len(prefix) >= 64:
                    break
    assert 0 < total <= config.max_download_bytes
    assert b"OSMHeader" in prefix
