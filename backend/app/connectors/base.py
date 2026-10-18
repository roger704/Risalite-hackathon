"""Shared acquisition for file-based adapters; normalization stays source-specific."""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from app.domain import ConfigError, DatasetConfig, FetchRequest, RawBundle, SourceHealth
from app.infrastructure.http import HTTPFetcher


class FileConnector:
    adapter_version = "1.0"

    def __init__(self, fetcher: HTTPFetcher | None = None):
        self.fetcher = fetcher or HTTPFetcher()

    async def fetch(self, request: FetchRequest) -> RawBundle:
        config = request.config
        if not config.enabled or config.access_validation == "blocked":
            raise ConfigError("Dataset is disabled or blocked")
        suffix = Path(urlsplit(config.resource_url).path).suffix or ".bin"
        asset = await self.fetcher.download(config, Path(request.staging_dir) / f"source{suffix}")
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "url": config.resource_url,
                    "source_edition": config.source_edition,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        return RawBundle(
            assets=(asset,),
            retrieved_at=datetime.now(timezone.utc),
            snapshot_id=request.snapshot_id,
            request_fingerprint=fingerprint,
        )

    async def probe(self, config: DatasetConfig) -> SourceHealth:
        # A local configuration check is deliberately not reported as a live reachability check.
        try:
            self.fetcher.validate_url(config.resource_url, config.allowed_hosts)
        except ConfigError as exc:
            return SourceHealth(
                status="unavailable", checked_at=datetime.now(timezone.utc), message=str(exc)
            )
        return SourceHealth(
            status="unvalidated",
            checked_at=datetime.now(timezone.utc),
            message="URL policy checked; provider reachability requires an ingestion",
        )
