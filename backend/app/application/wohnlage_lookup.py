"""Local address-to-Wohnlage lookup and join coverage utilities."""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pyarrow.compute as pc
import pyarrow.dataset as ds

from app.domain import ConnectorError, NotFoundError

JOIN_METHOD = (
    "Exact provider key: five-digit street_code + zero-padded three-digit house_number "
    "+ optional uppercase house_number_suffix equals WFS schluessel."
)
NOTICE = (
    "Wohnlage is the official Mietspiegel 2026 classification of an address's wider "
    "residential surroundings. It is not income, purchasing power, commercial suitability, "
    "footfall, shop rent, or a rent amount."
)
REGISTRY_EDITION_WARNING = (
    "Registry configuration has moved to a different edition; response metadata is taken "
    "from the pinned snapshot."
)


def address_join_key(
    street_code: str | None, house_number: int | None, suffix: str | None = None
) -> str | None:
    if not isinstance(street_code, str) or not re.fullmatch(r"\d{5}", street_code):
        return None
    if type(house_number) is not int or not 0 <= house_number <= 999:
        return None
    if suffix is not None:
        if not isinstance(suffix, str) or not re.fullmatch(r"[A-Za-z]{1,2}", suffix):
            return None
        suffix = suffix.upper()
    return f"{street_code}{house_number:03d}{suffix or ''}"


def _table_for_record(path: Path, record_id: str):
    dataset = ds.dataset(path, format="parquet")
    return dataset.to_table(
        columns=["record_json"],
        filter=ds.field("record_id") == record_id,
    )


def _decode_rows(table):
    return [json.loads(value) for value in table.column("record_json").to_pylist()]


def _snapshot_year(manifest) -> str:
    valid_from = getattr(manifest, "valid_from", None)
    if not isinstance(valid_from, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", valid_from):
        raise ConnectorError(
            "SNAPSHOT_INCOMPATIBLE",
            "Pinned Wohnlage snapshot has a missing or malformed valid_from date",
        )
    try:
        date.fromisoformat(valid_from)
    except ValueError as exc:
        raise ConnectorError(
            "SNAPSHOT_INCOMPATIBLE",
            "Pinned Wohnlage snapshot has a missing or malformed valid_from date",
        ) from exc
    return valid_from[:4]


def _snapshot_edition(path: Path):
    table = ds.dataset(path, format="parquet").head(1, columns=["record_json"])
    rows = _decode_rows(table)
    return rows[0].get("edition") if rows else None


def _typename_from_url(url: str | None) -> str | None:
    if not isinstance(url, str):
        return None
    values = parse_qs(urlparse(url).query).get("TYPENAMES")
    return values[0] if values else None


def _snapshot_typename(manifest) -> str | None:
    for asset in getattr(manifest, "raw_assets", ()):
        typename = _typename_from_url(getattr(asset, "resource_url", None))
        if typename:
            return typename
    return _typename_from_url(getattr(manifest, "resource_url", None))


def _snapshot_records(service, dataset_id: str, snapshot_id: str):
    try:
        manifest = service.catalog.get(dataset_id, snapshot_id)
    except NotFoundError as exc:
        raise NotFoundError(
            f"No published {dataset_id} snapshot is available for this lookup",
            "SNAPSHOT_NOT_FOUND",
        ) from exc
    return manifest, service._snapshot_path(manifest)


def _candidate_rows(path: Path, prefix: str, limit: int = 10) -> list[dict]:
    dataset = ds.dataset(path, format="parquet")
    scanner = dataset.scanner(
        columns=["record_id"],
        filter=pc.starts_with(ds.field("record_id"), pattern=prefix),
        batch_size=limit,
    )
    selected_ids = []
    for batch in scanner.to_batches():
        selected_ids.extend(batch.column("record_id").to_pylist()[: limit - len(selected_ids)])
        if len(selected_ids) == limit:
            break
    if not selected_ids:
        return []
    rows = dataset.to_table(
        columns=["record_id", "record_json"],
        filter=ds.field("record_id").isin(selected_ids),
    )
    by_id = dict(zip(rows.column("record_id").to_pylist(), _decode_rows(rows), strict=True))
    return [by_id[value] for value in selected_ids if value in by_id]


def _public_record(row: dict | None):
    return row


def lookup_wohnlage(
    service,
    dataset_id: str,
    snapshot_id: str,
    *,
    address_key: str | None = None,
    street_code: str | None = None,
    house_number: str | int | None = None,
    house_number_suffix: str | None = None,
    address_snapshot_id: str | None = None,
    address_id: str | None = None,
) -> dict:
    config = service._config(dataset_id)
    if config.adapter != "berlin_wfs_wohnlagen":
        raise ConnectorError(
            "INVALID_REQUEST", f"Wohnlage lookup is not supported for dataset: {dataset_id}"
        )
    manifest, wohnlage_path = _snapshot_records(service, dataset_id, snapshot_id)
    year = _snapshot_year(manifest)
    options = config.parser_options
    config_matches_snapshot = type(options.get("edition_year")) is int and options[
        "edition_year"
    ] == int(year)
    snapshot_warnings = list(manifest.quality_report.warnings)
    evidence_typename = _snapshot_typename(manifest)
    if evidence_typename is None and config_matches_snapshot:
        evidence_typename = options.get("typename")
    source_links = (
        options.get("source_links", [])
        if config_matches_snapshot
        else [manifest.landing_url]
        if manifest.landing_url
        else []
    )
    limitations = options.get("limitations", []) if config_matches_snapshot else snapshot_warnings
    response_warnings = list(snapshot_warnings)
    if not config_matches_snapshot:
        response_warnings.append(REGISTRY_EDITION_WARNING)
    base_response = {
        "schema_version": "1.0",
        "dataset_id": dataset_id,
        "snapshot_id": snapshot_id,
        "retrieved_at": manifest.retrieved_at.isoformat().replace("+00:00", "Z"),
        "source_edition": manifest.source_edition,
        "edition": options.get("edition") if config_matches_snapshot else None,
        "valid_from": manifest.valid_from,
        "valid_to": manifest.valid_to,
        "join_method": JOIN_METHOD,
        "source_links": source_links,
        "limitations": limitations,
        "notice": NOTICE,
        "attribution": manifest.attribution,
        "license": {
            "id": manifest.license_id,
            "url": manifest.license_url,
        },
        "warnings": response_warnings,
        "evidence": {
            "raw_file_hashes": [asset.sha256 for asset in manifest.raw_assets],
        },
        "candidates": [],
        "record": None,
        "address": None,
        "wohnlage_class": None,
    }
    if evidence_typename:
        base_response["evidence"]["typename"] = evidence_typename

    mode_address_key = address_key is not None
    mode_street = (
        street_code is not None or house_number is not None or house_number_suffix is not None
    )
    mode_address = address_snapshot_id is not None or address_id is not None
    if sum((mode_address_key, mode_street, mode_address)) != 1:
        raise ConnectorError(
            "INVALID_REQUEST",
            "Supply exactly one of address_key, street_code/house_number, or "
            "address_snapshot_id/address_id",
        )

    query = {
        "address_key": address_key,
        "street_code": street_code,
        "house_number": house_number,
        "house_number_suffix": house_number_suffix,
        "address_snapshot_id": address_snapshot_id,
        "address_id": address_id,
    }
    input_address = None
    if mode_address:
        if not address_snapshot_id or not address_id:
            raise ConnectorError(
                "INVALID_REQUEST", "address_snapshot_id and address_id must be supplied together"
            )
        _, address_path = _snapshot_records(
            service, options.get("address_dataset", "berlin_addresses"), address_snapshot_id
        )
        address_row_id = (
            address_id if address_id.startswith("address:") else f"address:{address_id}"
        )
        address_rows = _table_for_record(address_path, address_row_id)
        address_values = _decode_rows(address_rows)
        if not address_values:
            raise NotFoundError("Address record was not found in the requested snapshot")
        input_address = address_values[0]
        query.update(
            {
                "address_id": input_address.get("source_record_id"),
                "address_snapshot_id": address_snapshot_id,
            }
        )
        base_response["address"] = {
            "address_snapshot_id": address_snapshot_id,
            "address_id": input_address.get("source_record_id"),
            "record_id": input_address.get("record_id"),
            "street_name": input_address.get("street_name"),
            "street_code": input_address.get("street_code"),
            "house_number": input_address.get("house_number"),
            "house_number_suffix": input_address.get("house_number_suffix"),
            "postcode": input_address.get("postcode"),
            "address_type": input_address.get("address_type"),
        }
        street_code = input_address.get("street_code")
        house_number = input_address.get("house_number")
        house_number_suffix = input_address.get("house_number_suffix")

    if mode_address_key:
        if not re.fullmatch(r"\d{8}[A-Z]{0,2}", address_key or ""):
            raise ConnectorError("INVALID_REQUEST", "address_key has an invalid format")
        key = address_key
        query = {"address_key": address_key}
    else:
        try:
            numeric_house_number = int(house_number) if house_number is not None else None
        except (TypeError, ValueError):
            numeric_house_number = None
        key = address_join_key(street_code, numeric_house_number, house_number_suffix)

    base_response["query"] = query
    if key is None:
        snapshot_edition = _snapshot_edition(wohnlage_path)
        if snapshot_edition is not None:
            base_response["edition"] = snapshot_edition
        base_response["join_status"] = "not_joinable"
        base_response["join_method"] = JOIN_METHOD
        base_response["warnings"].append(
            "The supplied address has no joinable five-digit street code and house number."
        )
        return base_response

    record_id = f"wohnlage:{year}:{key}"
    rows = _table_for_record(wohnlage_path, record_id)
    found = _decode_rows(rows)
    candidates: list[dict] = []
    if not found:
        snapshot_edition = _snapshot_edition(wohnlage_path)
        if snapshot_edition is not None:
            base_response["edition"] = snapshot_edition
        prefix = f"wohnlage:{year}:{key[:8]}"
        candidates = _candidate_rows(wohnlage_path, prefix)
        base_response["candidates"] = candidates
        base_response["join_status"] = "not_found"
        base_response["evidence"]["lookup_key"] = key
        return base_response

    record = found[0]
    base_response["edition"] = record.get("edition")
    base_response["evidence"].update(
        {
            "schluessel": record["address_key"],
            "original_wol_label": record.get("source_label"),
        }
    )
    if evidence_typename:
        base_response["evidence"]["provider_feature_id"] = (
            f"{evidence_typename.split(':')[-1]}.{record['address_key']}"
        )
    mismatch = False
    if input_address is not None:
        address_street = input_address.get("street_name")
        location_street = record.get("street_name")
        address_postcode = input_address.get("postcode")
        location_postcode = record.get("postcode")
        mismatch = (
            address_street is not None
            and location_street is not None
            and address_street.casefold() != location_street.casefold()
        ) or (
            address_postcode is not None
            and location_postcode is not None
            and address_postcode != location_postcode
        )
    if mismatch:
        base_response["join_status"] = "ambiguous"
        base_response["candidates"] = [record]
        base_response["warnings"].append(
            "The exact join key matched, but street name or postcode disagrees; no class was selected."
        )
        return base_response
    base_response["record"] = _public_record(record)
    base_response["wohnlage_class"] = record.get("wohnlage_class")
    base_response["join_status"] = (
        "matched" if record.get("class_status") == "assigned" else "matched_unclassified"
    )
    return base_response


def address_join_report(wohnlage_parquet: str | Path, address_parquet: str | Path) -> dict:
    """Stream the two normalized snapshots and report exact-key join coverage."""
    wohnlage_ds = ds.dataset(wohnlage_parquet, format="parquet")
    address_ds = ds.dataset(address_parquet, format="parquet")
    wohnlage_by_key: dict[str, tuple[str | None, str | None]] = {}
    wohnlage_rows = 0
    for batch in wohnlage_ds.scanner(columns=["record_json"], batch_size=65536).to_batches():
        for value in batch.column(0).to_pylist():
            row = json.loads(value)
            key = row.get("address_key")
            if key is not None:
                wohnlage_rows += 1
                wohnlage_by_key[key] = (row.get("street_name"), row.get("postcode"))

    counts: Counter[str] = Counter()
    seen_wohnlage_keys: set[str] = set()
    for batch in address_ds.scanner(columns=["record_json"], batch_size=65536).to_batches():
        for value in batch.column(0).to_pylist():
            row = json.loads(value)
            counts["addresses_total"] += 1
            key = address_join_key(
                row.get("street_code"),
                row.get("house_number"),
                row.get("house_number_suffix"),
            )
            if key is None:
                counts["not_joinable"] += 1
                continue
            counts["joinable"] += 1
            location = wohnlage_by_key.get(key)
            if location is None:
                counts["not_found"] += 1
                continue
            seen_wohnlage_keys.add(key)
            location_street, location_postcode = location
            conflict = (
                row.get("street_name") is not None
                and location_street is not None
                and row["street_name"].casefold() != location_street.casefold()
            ) or (
                row.get("postcode") is not None
                and location_postcode is not None
                and row["postcode"] != location_postcode
            )
            if conflict:
                counts["ambiguous"] += 1
            else:
                counts["matched"] += 1
    counts["wohnlage_rows_without_any_address"] = len(set(wohnlage_by_key) - seen_wohnlage_keys)
    counts["wohnlage_rows_total"] = wohnlage_rows
    return {
        name: counts[name]
        for name in (
            "addresses_total",
            "joinable",
            "matched",
            "not_found",
            "ambiguous",
            "not_joinable",
            "wohnlage_rows_without_any_address",
            "wohnlage_rows_total",
        )
    }
