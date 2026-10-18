"""Offline normalization, ambiguity, and paging tests for the local address index."""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from app.application.address_search import AddressIndex, normalize_street, parse_query
from app.connectors.berlin_addresses import BerlinAddressConnector
from app.domain import DatasetConfig, RawAsset, RawBundle, record_to_dict

BACKEND = Path(__file__).parents[2]
FIXTURES = BACKEND / "tests/fixtures/berlin_addresses"
CONFIG = BACKEND / "config/datasets/berlin_addresses.json"


def address_index():
    values = json.loads(CONFIG.read_text())
    values["allowed_hosts"] = tuple(values["allowed_hosts"])
    options = values["parser_options"]
    options.pop("expected_count_range")
    options.update(page_size=3, max_pages=10, expected_count=6)
    config = DatasetConfig(**values)
    assets = []
    for path in sorted(FIXTURES.glob("page-*.json")):
        assets.append(
            RawAsset(
                path=str(path),
                sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                size_bytes=path.stat().st_size,
                resource_url=f"https://gdi.berlin.de/{path.name}",
            )
        )
    bundle = RawBundle(
        assets=tuple(assets),
        retrieved_at=datetime.now(timezone.utc),
        snapshot_id="search-fixture",
        request_fingerprint="synthetic",
    )
    records = BerlinAddressConnector().normalize(bundle, config).records
    return AddressIndex((index, record_to_dict(record)) for index, record in enumerate(records))


def test_street_normalization_handles_umlauts_ss_and_street_abbreviations():
    assert normalize_street("Müllerstraße") == normalize_street("Muellerstrasse")
    assert normalize_street("müllerstr.") == normalize_street("MUELLER STR")
    assert normalize_street("Wienerstr.") == normalize_street("Wiener Straße")
    assert normalize_street("WIENER STR") == "wienerstrasse"
    assert normalize_street("Karl-Marx-Allee") == normalize_street("Karl Marx Allee")
    assert normalize_street("Straße des 17. Juni") == "strassedes17juni"


def test_query_parsing_handles_house_suffix_postcode_and_embedded_numbers():
    assert parse_query("wiener str 12a") == parse_query("Wiener Straße 12 A")
    assert parse_query("Wiener Straße 12 A").house_number_suffix == "A"
    with_postcode = parse_query("Lindenstraße 10, 10999 Berlin")
    assert (with_postcode.house_number, with_postcode.postcode) == (10, "10999")
    embedded = parse_query("Straße des 17. Juni 135")
    assert embedded.street_key == "strassedes17juni"
    assert embedded.house_number == 135


def test_exact_suffix_variant_ambiguity_filters_and_street_search():
    index = address_index()

    total, results, ambiguous, _ = index.search(parse_query("Wienerstr. 12a"))
    assert total == 1
    assert results[0][0].source_record_id == "1002"
    assert results[0][1] == "exact"
    assert not ambiguous

    only_suffix = AddressIndex([(0, record_to_dict(results[0][0]))])
    total, variant, ambiguous, _ = only_suffix.search(parse_query("Wiener Straße 12"))
    assert total == 1
    assert variant[0][1] == "house_number_suffix_variant"
    assert not ambiguous

    total, results, ambiguous, candidates = index.search(parse_query("Lindenstraße 10"))
    assert total == 2
    assert ambiguous
    assert {result.source_record_id for result, _ in results} == {"1003", "1004"}
    assert candidates == [
        {"postcode": "10969", "district_name": "Friedrichshain-Kreuzberg"},
        {"postcode": "12555", "district_name": "Treptow-Köpenick"},
    ]
    assert index.search(parse_query("Lindenstraße 10"), postcode="10969")[0] == 1
    assert index.search(parse_query("Lindenstraße 10"), district="treptow-köpenick")[0] == 1

    total, results, _, _ = index.search(parse_query("Müllerstraße"))
    assert total == 1
    assert results[0][0].source_record_id == "1005"
    assert results[0][1] == "street"


def test_postcode_filter_precedes_house_number_suffix_preference():
    source_index = address_index()
    rows = []
    for row in source_index._row_records.values():
        if row["source_record_id"] not in {"1003", "1004"}:
            continue
        row = dict(row)
        if row["source_record_id"] == "1004":
            row["house_number_suffix"] = "A"
        rows.append((len(rows), row))
    index = AddressIndex(rows)

    total, results, _, _ = index.search(parse_query("Lindenstraße 10"), postcode="12555")
    assert total == 1
    assert results[0][0].source_record_id == "1004"
    assert results[0][1] == "house_number_suffix_variant"

    total, results, _, _ = index.search(parse_query("Lindenstraße 10"))
    assert total == 1
    assert results[0][0].source_record_id == "1003"
    assert results[0][1] == "exact"


def test_prefix_empty_result_and_deterministic_pagination():
    index = address_index()
    total, results, _, _ = index.search(parse_query("Wiene"))
    assert total == 3
    assert {match_type for _, match_type in results} == {"street_prefix"}

    total, results, _, _ = index.search(parse_query("Wiener Straße"))
    assert total == 3
    assert {match_type for _, match_type in results} == {"street"}

    total, results, _, _ = index.search(parse_query("Wiene"), limit=1, offset=0)
    assert total == 3
    assert results[0][0].source_record_id == "S297"
    second = index.search(parse_query("Wiene"), limit=1, offset=1)[1][0][0]
    third = index.search(parse_query("Wiene"), limit=1, offset=2)[1][0][0]
    assert [second.source_record_id, third.source_record_id] == ["1001", "1002"]
    assert index.search(parse_query("No such address"))[0] == 0
