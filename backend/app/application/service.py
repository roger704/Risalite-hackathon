"""Snapshot ingestion and local queries; no provider calls occur in read methods."""

import base64
import fcntl
import hashlib
import json
import os
import re
import shutil
import uuid
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

from app.domain import (
    PUBLIC_REDISTRIBUTION_STATUSES,
    Area,
    AreaObservation,
    ConfigError,
    ConnectorError,
    DatasetConfig,
    FetchRequest,
    IncompatibleSnapshotError,
    NotFoundError,
    RawAsset,
    RawBundle,
    SnapshotManifest,
    manifest_to_dict,
    record_to_dict,
)
from app.infrastructure.catalog import Catalog
from app.infrastructure.storage import read_record_page, read_records, write_records

BACKEND_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_ROOT.parent


def default_data_dir() -> Path:
    configured = os.environ.get("LOCATION_DATA_DIR", "").strip()
    return Path(configured).expanduser() if configured else REPO_ROOT / "data"


def _hash(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


class DataService:
    def __init__(
        self,
        data_dir: Path | None = None,
        config_dir: Path | None = None,
        connectors: dict | None = None,
    ):
        self.root = Path(data_dir or default_data_dir()).resolve()
        self.catalog = Catalog(self.root)
        self.configs = {}
        for path in sorted(Path(config_dir or BACKEND_ROOT / "config" / "datasets").glob("*.json")):
            try:
                values = json.loads(path.read_text())
                values["allowed_hosts"] = tuple(values["allowed_hosts"])
                config = DatasetConfig(**values)
            except (ValueError, KeyError, TypeError) as exc:
                raise ConfigError(f"Invalid registry file: {path.name}") from exc
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", config.dataset_id):
                raise ConfigError("Dataset ID must be a safe registry identifier")
            if config.dataset_id in self.configs:
                raise ConfigError(f"Duplicate registered dataset: {config.dataset_id}")
            self.configs[config.dataset_id] = config
        self.connectors = dict(connectors) if connectors is not None else {}
        self._lazy_connectors = connectors is None
        self._address_indexes = {}

    def _connector(self, adapter: str):
        if adapter not in self.connectors:
            if not self._lazy_connectors:
                raise ConfigError(f"Adapter is not implemented: {adapter}")
            from app.connectors.registry import load_connector

            self.connectors[adapter] = load_connector(adapter)
        return self.connectors[adapter]

    @staticmethod
    def _parent_dataset(config) -> str | None:
        if config.adapter == "population":
            return "berlin_lor"
        return config.parser_options.get("parent_dataset")

    def _config(self, dataset_id: str):
        if dataset_id not in self.configs:
            raise NotFoundError(f"Unknown dataset: {dataset_id}", "UNKNOWN_DATASET")
        return self.configs[dataset_id]

    @contextmanager
    def _lock(self, dataset_id: str):
        directory = self.root / "locks"
        directory.mkdir(exist_ok=True)
        with (directory / f"{dataset_id}.lock").open("a") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ConnectorError(
                    "INGESTION_BUSY", "This dataset is already being ingested", True
                ) from exc
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    async def ingest(
        self, dataset_id: str, local_file: Path | None = None, parent_snapshot_id: str | None = None
    ) -> SnapshotManifest:
        config = self._config(dataset_id)
        if not config.enabled or config.access_validation == "blocked":
            raise ConfigError("Dataset is disabled or blocked")
        if config.redistribution_status not in PUBLIC_REDISTRIBUTION_STATUSES:
            raise ConfigError("Public snapshot ingestion requires reviewed redistribution terms")
        connector = self._connector(config.adapter)
        parent_dataset = self._parent_dataset(config)
        if parent_snapshot_id and not parent_dataset:
            raise ConfigError("This dataset does not accept a parent snapshot")
        if (
            parent_dataset
            and config.adapter != "population"
            and not hasattr(connector, "check_parent")
        ):
            raise ConfigError("Connectors with a parent dataset must implement check_parent")
        with self._lock(dataset_id):
            job_id = uuid.uuid4().hex
            stage = self.root / "staging" / job_id
            stage.mkdir(parents=True)
            self.catalog.begin_job(job_id, dataset_id)
            try:
                parent = (
                    self.catalog.get(parent_dataset, parent_snapshot_id) if parent_dataset else None
                )
                parent_ids = (parent.snapshot_id,) if parent else ()
                snapshot_id = f"{dataset_id}-{job_id}"
                if local_file is None:
                    raw = await connector.fetch(
                        FetchRequest(
                            config=config,
                            staging_dir=str(stage),
                            snapshot_id=snapshot_id,
                        )
                    )
                else:
                    raw = self._local_bundle(config, Path(local_file), stage, snapshot_id)
                config_hash = _hash(asdict(config))
                fingerprint = _hash(
                    {
                        "config": config_hash,
                        "adapter_version": connector.adapter_version,
                        "raw_hashes": [asset.sha256 for asset in raw.assets],
                        "acquisition_mode": "local_file" if local_file else "provider_download",
                        "parent_snapshot_ids": parent_ids,
                    }
                )
                cached = self.catalog.cached(dataset_id, fingerprint)
                if cached and self._snapshot_available(cached):
                    self.catalog.activate(cached, job_id)
                    self._cleanup_stage(stage)
                    return cached
                batch = connector.normalize(raw, config)
                quality = connector.validate(batch)
                (stage / "quality.json").write_text(json.dumps(asdict(quality), indent=2))
                if not quality.valid or batch.rejected_rows:
                    raise ConnectorError(
                        "VALIDATION_FAILED", "; ".join(quality.errors) or "Import rejected rows"
                    )
                if any(
                    item.source_id != dataset_id or item.snapshot_id != snapshot_id
                    for item in batch.records
                ):
                    raise ConnectorError(
                        "VALIDATION_FAILED",
                        "Adapter emitted records for a different dataset/snapshot",
                    )
                if len({item.record_id for item in batch.records}) != len(batch.records):
                    raise ConnectorError(
                        "VALIDATION_FAILED", "Adapter emitted duplicate record IDs"
                    )
                coverage = {
                    "extraction_complete": True,
                    "record_count": len(batch.records),
                    "acquisition_mode": "local_file" if local_file else "provider_download",
                }
                if parent and config.adapter == "population":
                    coverage.update(self._validate_parent(config, batch.records, parent))
                elif parent:
                    coverage.update(
                        connector.check_parent(config, batch.records, self._load_records(parent))
                    )
                if not (parent and config.adapter == "population"):
                    geometries = [
                        item.geometry for item in batch.records if hasattr(item, "geometry")
                    ]
                    if geometries:
                        from shapely.geometry import shape
                        from shapely.ops import unary_union

                        coverage["bbox"] = list(
                            unary_union([shape(item) for item in geometries]).bounds
                        )
                    if batch.records and all(isinstance(item, Area) for item in batch.records):
                        coverage["area_count"] = len(batch.records)
                normalized_relative = f"normalized/{dataset_id}/{snapshot_id}/records.parquet"
                write_records(batch.records, stage / "records.parquet")
                normalized_path = self._asset_path(normalized_relative)
                normalized_path.parent.mkdir(parents=True)
                raw_dir = self.root / "raw" / dataset_id / snapshot_id
                raw_dir.mkdir(parents=True)
                saved_assets = []
                for index, asset in enumerate(raw.assets):
                    destination = raw_dir / f"{index}-{Path(asset.path).name}"
                    shutil.copyfile(asset.path, destination)
                    saved_assets.append(
                        replace(asset, path=str(destination.relative_to(self.root)))
                    )
                shutil.move(str(stage / "records.parquet"), normalized_path)
                manifest = SnapshotManifest(
                    snapshot_id=snapshot_id,
                    dataset_id=dataset_id,
                    adapter_version=connector.adapter_version,
                    landing_url=config.landing_url,
                    resource_url=config.resource_url,
                    retrieved_at=raw.retrieved_at,
                    source_edition=config.source_edition,
                    reference_period=config.reference_period,
                    valid_from=config.parser_options.get("valid_from"),
                    valid_to=config.parser_options.get("valid_to"),
                    boundary_edition=config.boundary_edition,
                    raw_assets=tuple(saved_assets),
                    request_fingerprint=fingerprint,
                    parser_config_hash=config_hash,
                    quality_report=quality,
                    coverage=coverage,
                    license_id=config.license_id,
                    license_url=config.license_url,
                    attribution=config.attribution,
                    redistribution_status=config.redistribution_status,
                    normalized_assets=(normalized_relative,),
                    parent_snapshot_ids=parent_ids,
                )
                normalized_path.with_name("manifest.json").write_text(
                    json.dumps(manifest_to_dict(manifest), indent=2, ensure_ascii=False),
                )
                self.catalog.publish(manifest, job_id)
                self._cleanup_stage(stage)
                return manifest
            except BaseException as exc:
                # Publication and successful job completion share one transaction.
                # An interrupt after commit must never relabel a published import as failed.
                if self.catalog.job_succeeded(job_id):
                    raise
                self.catalog.finish_job(
                    job_id, "failed" if isinstance(exc, Exception) else "aborted", error=exc
                )
                (stage / "failure.json").write_text(
                    json.dumps(
                        {
                            "code": getattr(exc, "code", "INTERNAL_ERROR"),
                            "message": str(exc)[:1000],
                        }
                    )
                )
                raise

    @staticmethod
    def _cleanup_stage(stage: Path):
        try:
            shutil.rmtree(stage)
        except OSError:
            # Cleanup is housekeeping, not publication. A later run can remove leftovers.
            pass

    def _local_bundle(self, config, source, stage, snapshot_id):
        if not source.is_file():
            raise ConfigError("Local source file does not exist")
        target = stage / f"source{source.suffix.lower()}"
        digest, size = hashlib.sha256(), 0
        with source.open("rb") as reader, target.open("wb") as writer:
            for chunk in iter(lambda: reader.read(65536), b""):
                size += len(chunk)
                if size > config.max_download_bytes:
                    raise ConfigError("Local file exceeds configured size limit")
                digest.update(chunk)
                writer.write(chunk)
        if size == 0:
            raise ConfigError("Local source file is empty")
        return RawBundle(
            assets=(
                RawAsset(
                    path=str(target),
                    sha256=digest.hexdigest(),
                    size_bytes=size,
                    resource_url=config.resource_url,
                ),
            ),
            retrieved_at=datetime.now(timezone.utc),
            snapshot_id=snapshot_id,
            request_fingerprint=_hash({"resource_url": config.resource_url, "mode": "local"}),
        )

    def _asset_path(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root):
            raise ConfigError("Snapshot asset path escapes the data directory")
        return path

    def _load_records(self, manifest):
        return read_records(self._snapshot_path(manifest))

    def _snapshot_path(self, manifest) -> Path:
        if len(manifest.normalized_assets) != 1:
            raise ConnectorError("SOURCE_UNAVAILABLE", "Snapshot storage is incomplete")
        path = self._asset_path(manifest.normalized_assets[0])
        if not path.is_file():
            raise ConnectorError("SOURCE_UNAVAILABLE", "Published snapshot asset is missing")
        return path

    def _snapshot_available(self, manifest) -> bool:
        return (
            manifest.quality_report.valid
            and len(manifest.normalized_assets) == 1
            and all(self._asset_path(asset).is_file() for asset in manifest.normalized_assets)
        )

    def _validate_parent(self, config, records, parent):
        if parent.boundary_edition != config.boundary_edition:
            raise IncompatibleSnapshotError("Population and LOR boundary editions differ")
        areas = self._load_records(parent)
        keys = {
            (record.hierarchy, record.area_code, record.boundary_edition)
            for record in areas
            if isinstance(record, Area)
        }
        observed = {
            (record.hierarchy, record.area_code, record.boundary_edition)
            for record in records
            if isinstance(record, AreaObservation)
        }
        if not observed or keys != observed:
            missing = len(keys - observed)
            unmatched = len(observed - keys)
            raise IncompatibleSnapshotError(
                f"Population/LOR coverage differs: {missing} missing areas, {unmatched} unmatched areas"
            )
        return {
            "area_count": len(observed),
            "lor_snapshot_id": parent.snapshot_id,
            "join_method": "exact hierarchy, boundary scheme, and complete area-code set",
            "limitations": [
                "Code compatibility does not certify unchanged polygon boundaries between source editions."
            ],
        }

    def _freshness(self, config, manifest):
        days = config.parser_options.get("refresh_after_days")
        if days is None:
            return "unknown"
        age = (datetime.now(timezone.utc) - manifest.retrieved_at).total_seconds() / 86400
        return "stale" if age > days else "current"

    def list_datasets(self):
        result = []
        for config in self.configs.values():
            try:
                manifest = self.catalog.get(config.dataset_id)
            except NotFoundError:
                manifest = None
            result.append(
                {
                    "dataset_id": config.dataset_id,
                    "provider": config.provider,
                    "adapter": config.adapter,
                    "enabled": config.enabled,
                    "access_validation": config.access_validation,
                    "availability": "ready"
                    if manifest and self._snapshot_available(manifest)
                    else "unavailable",
                    "freshness": self._freshness(config, manifest) if manifest else "unknown",
                    "latest_snapshot_id": manifest.snapshot_id if manifest else None,
                    "reference_period": manifest.reference_period
                    if manifest
                    else config.reference_period,
                    "coverage": manifest.coverage if manifest else {},
                    "license_id": config.license_id,
                    "attribution": config.attribution,
                }
            )
        return result

    def manifest(self, dataset_id: str, snapshot_id: str):
        self._config(dataset_id)
        return manifest_to_dict(self.catalog.get(dataset_id, snapshot_id))

    def records(
        self,
        dataset_id: str,
        snapshot_id: str,
        area_code: str | None = None,
        metric_code: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ):
        config = self._config(dataset_id)
        if not 1 <= limit <= 1000:
            raise ConnectorError("INVALID_FILTER", "limit must be between 1 and 1000")
        if metric_code is not None and config.adapter != "population":
            raise ConnectorError(
                "INVALID_FILTER", "metric_code is unsupported for boundary records"
            )
        manifest = self.catalog.get(dataset_id, snapshot_id)
        query_key = _hash([dataset_id, snapshot_id, area_code, metric_code])
        offset = 0
        if cursor:
            try:
                if len(cursor) > 2048:
                    raise ValueError("oversized cursor")
                payload = json.loads(base64.urlsafe_b64decode(cursor.encode()))
                offset = payload["offset"]
                if payload["query"] != query_key or type(offset) is not int or offset < 0:
                    raise ValueError("cursor does not match query")
            except (ValueError, KeyError, TypeError, UnicodeError) as exc:
                raise ConnectorError(
                    "INVALID_CURSOR", "Invalid cursor for this snapshot and query"
                ) from exc
        if metric_code is None:
            total, page = read_record_page(
                self._snapshot_path(manifest), area_code=area_code, offset=offset, limit=limit
            )
        else:
            records = sorted(
                (
                    record
                    for record in self._load_records(manifest)
                    if (area_code is None or getattr(record, "area_code", None) == area_code)
                    and getattr(record, "metric_code", None) == metric_code
                ),
                key=lambda record: record.record_id,
            )
            total, page = len(records), records[offset : offset + limit]
        if offset > total:
            raise ConnectorError("INVALID_CURSOR", "Cursor is beyond the matching records")
        next_offset = offset + len(page)
        next_cursor = (
            base64.urlsafe_b64encode(
                json.dumps({"query": query_key, "offset": next_offset}).encode()
            ).decode()
            if next_offset < total
            else None
        )
        return {
            "schema_version": "1.0",
            "dataset_id": dataset_id,
            "snapshot_id": snapshot_id,
            "availability": "ready",
            "freshness": self._freshness(config, manifest),
            "retrieved_at": manifest_to_dict(manifest)["retrieved_at"],
            "reference_period": manifest.reference_period,
            "coverage": manifest.coverage,
            "records": [record_to_dict(record) for record in page],
            "count": len(page),
            "next_cursor": next_cursor,
            "warnings": list(manifest.quality_report.warnings),
            "attribution": [
                {
                    "text": manifest.attribution,
                    "url": manifest.landing_url,
                    "license_id": manifest.license_id,
                    "license_url": manifest.license_url,
                }
            ],
        }

    def search_addresses(
        self,
        dataset_id: str,
        snapshot_id: str,
        q: str,
        postcode: str | None = None,
        district: str | None = None,
        limit: int = 10,
        cursor: str | None = None,
    ):
        from app.application.address_search import AddressIndex, normalize_street, parse_query

        config = self._config(dataset_id)
        if config.adapter != "berlin_wfs_addresses":
            raise ConnectorError(
                "INVALID_FILTER", "Address search is only available for address datasets"
            )
        if not 1 <= limit <= 50:
            raise ConnectorError("INVALID_FILTER", "limit must be between 1 and 50")
        try:
            parsed = parse_query(q)
        except ValueError as exc:
            raise ConnectorError(
                "INVALID_FILTER", "The address query has no searchable street"
            ) from exc
        manifest = self.catalog.get(dataset_id, snapshot_id)
        effective_postcode = postcode if postcode is not None else parsed.postcode
        query_key = _hash(
            [
                dataset_id,
                snapshot_id,
                parsed.street_key,
                parsed.house_number,
                parsed.house_number_suffix,
                effective_postcode,
                normalize_street(district) if district else None,
            ]
        )
        offset = 0
        if cursor:
            try:
                if len(cursor) > 2048:
                    raise ValueError("oversized cursor")
                payload = json.loads(base64.urlsafe_b64decode(cursor.encode()))
                offset = payload["offset"]
                if payload["query"] != query_key or type(offset) is not int or offset < 0:
                    raise ValueError("cursor does not match query")
            except (ValueError, KeyError, TypeError, UnicodeError) as exc:
                raise ConnectorError(
                    "INVALID_CURSOR", "Invalid cursor for this snapshot and query"
                ) from exc
        index = self._address_indexes.get(snapshot_id)
        if index is None:
            index = AddressIndex.from_parquet(self._snapshot_path(manifest))
            self._address_indexes[snapshot_id] = index
            while len(self._address_indexes) > 2:
                self._address_indexes.pop(next(iter(self._address_indexes)))
        else:
            self._address_indexes.pop(snapshot_id)
            self._address_indexes[snapshot_id] = index
        total, page, ambiguous, candidates = index.search(
            parsed,
            postcode=postcode,
            district=district,
            offset=offset,
            limit=limit,
        )
        if offset > total:
            raise ConnectorError("INVALID_CURSOR", "Cursor is beyond the matching addresses")
        next_offset = offset + len(page)
        next_cursor = (
            base64.urlsafe_b64encode(
                json.dumps({"query": query_key, "offset": next_offset}).encode()
            ).decode()
            if next_offset < total
            else None
        )
        outside_ids = set(manifest.coverage.get("outside_supplied_area_record_ids", ()))
        parent_snapshot_id = (
            manifest.parent_snapshot_ids[0] if manifest.parent_snapshot_ids else None
        )
        results = []
        for record, match_type in page:
            area_link = None
            if record.area_code is not None and parent_snapshot_id is not None:
                area_link = {
                    "area_code": record.area_code,
                    "area_name": record.area_name,
                    "hierarchy": config.parser_options.get("hierarchy", "planungsraum"),
                    "boundary_edition": manifest.boundary_edition,
                    "lor_snapshot_id": parent_snapshot_id,
                    "validated": record.record_id not in outside_ids,
                }
            results.append(
                {
                    "record": record_to_dict(record),
                    "match_type": match_type,
                    "point": {
                        "lon": record.geometry["coordinates"][0],
                        "lat": record.geometry["coordinates"][1],
                        "crs": "EPSG:4326",
                    },
                    "stable_id": record.record_id,
                    "source_record_id": record.source_record_id,
                    "area_link": area_link,
                }
            )
        return {
            "schema_version": "1.0",
            "dataset_id": dataset_id,
            "snapshot_id": snapshot_id,
            "retrieved_at": manifest.retrieved_at,
            "source_edition": manifest.source_edition,
            "reference_period": manifest.reference_period,
            "query": {
                "q": q,
                "street_key": parsed.street_key,
                "house_number": parsed.house_number,
                "house_number_suffix": parsed.house_number_suffix,
                "postcode": effective_postcode,
                "district": district,
            },
            "results": results,
            "total": total,
            "count": len(page),
            "next_cursor": next_cursor,
            "ambiguous": ambiguous,
            "candidates": candidates,
            "notice": (
                "An address result marks an official address point only; it never implies vacant "
                "premises, commercial availability, rental terms, or permitted use."
            ),
            "warnings": list(manifest.quality_report.warnings),
            "attribution": [
                {
                    "text": manifest.attribution,
                    "url": manifest.landing_url,
                    "license_id": manifest.license_id,
                    "license_url": manifest.license_url,
                }
            ],
        }
