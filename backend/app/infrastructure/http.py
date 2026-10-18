"""Bounded downloads from reviewed provider hosts, with injectable HTTP transport."""

import asyncio
import hashlib
import ipaddress
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from app.domain import ConfigError, DatasetConfig, RawAsset, SourceUnavailableError


class HTTPFetcher:
    def __init__(self, client: httpx.AsyncClient | None = None, backoff: float = 0.5):
        self.client = client
        self.backoff = backoff

    @staticmethod
    def validate_url(url: str, allowed_hosts: tuple[str, ...]) -> None:
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError as exc:
            raise ConfigError("Resource URL is malformed") from exc
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username
            or parts.password
            or port not in (None, 443)
            or parts.hostname not in allowed_hosts
        ):
            raise ConfigError("Resource URL must use HTTPS on an explicitly allowed provider host")
        if parts.hostname == "localhost" or parts.hostname.endswith((".local", ".localhost")):
            raise ConfigError("Local resource hosts are not permitted")
        try:
            address = ipaddress.ip_address(parts.hostname)
        except ValueError:
            return
        if not address.is_global:
            raise ConfigError("Private resource addresses are not permitted")

    async def download(self, config: DatasetConfig, target: Path) -> RawAsset:
        if self.client is not None:
            return await self._download(self.client, config, target)
        async with httpx.AsyncClient(
            follow_redirects=False,
            headers={
                "User-Agent": "RisaliteLocationIntelligence/0.1 (github.com/roger704/Risalite-hackathon)"
            },
        ) as client:
            return await self._download(client, config, target)

    async def _download(self, client, config, target):
        self.validate_url(config.resource_url, config.allowed_hosts)
        target.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(3):
            try:
                url = config.resource_url
                for _ in range(6):
                    self.validate_url(url, config.allowed_hosts)
                    async with client.stream(
                        "GET", url, timeout=config.timeout_seconds, follow_redirects=False
                    ) as response:
                        if response.is_redirect:
                            if "location" not in response.headers:
                                raise SourceUnavailableError(
                                    "Provider returned a redirect without a location", False
                                )
                            url = urljoin(url, response.headers["location"])
                            continue
                        if response.status_code == 429 or response.status_code in {
                            500,
                            502,
                            503,
                            504,
                        }:
                            if attempt == 2:
                                raise SourceUnavailableError(
                                    "Provider remained unavailable after three attempts"
                                )
                            delay = self._retry_delay(response.headers.get("retry-after"), attempt)
                            await asyncio.sleep(delay)
                            break
                        if response.status_code != 200:
                            raise SourceUnavailableError(
                                f"Provider returned HTTP {response.status_code}", False
                            )
                        media_type = (
                            response.headers.get("content-type", "").split(";", 1)[0].lower()
                        )
                        if media_type == "text/html":
                            raise SourceUnavailableError(
                                "Expected a data file but received an HTML page", False
                            )
                        digest, size, prefix = hashlib.sha256(), 0, b""
                        with target.open("wb") as stream:
                            async for chunk in response.aiter_bytes():
                                size += len(chunk)
                                if size > config.max_download_bytes:
                                    raise SourceUnavailableError(
                                        "Download exceeded configured byte limit", False
                                    )
                                prefix = (prefix + chunk)[:1024]
                                if (
                                    prefix.lstrip()
                                    .lower()
                                    .startswith((b"<!doctype html", b"<html"))
                                ):
                                    raise SourceUnavailableError(
                                        "Expected a data file but received HTML content", False
                                    )
                                digest.update(chunk)
                                stream.write(chunk)
                        if not size:
                            raise SourceUnavailableError("Provider returned an empty file", False)
                        return RawAsset(
                            path=str(target),
                            sha256=digest.hexdigest(),
                            size_bytes=size,
                            resource_url=str(response.url),
                            media_type=media_type or None,
                            etag=response.headers.get("etag"),
                            last_modified=response.headers.get("last-modified"),
                        )
                else:
                    raise SourceUnavailableError("Too many provider redirects", False)
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                target.unlink(missing_ok=True)
                if attempt == 2:
                    raise SourceUnavailableError(
                        "Provider connection failed after three attempts"
                    ) from exc
                await asyncio.sleep(self.backoff * 2**attempt)
            except Exception:
                target.unlink(missing_ok=True)
                raise
        raise SourceUnavailableError("Provider download failed")

    def _retry_delay(self, header: str | None, attempt: int) -> float:
        if not header:
            return self.backoff * 2**attempt
        try:
            delay = float(header)
        except ValueError:
            try:
                delay = (parsedate_to_datetime(header) - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError):
                return self.backoff * 2**attempt
        # Do not ignore a provider's long Retry-After by retrying too early.
        if delay > 60:
            raise SourceUnavailableError(
                "Provider requested a retry later than this ingestion budget"
            )
        return max(0.0, delay)
