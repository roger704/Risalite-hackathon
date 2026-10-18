"""Offline acquisition tests for failure, retry and provider-host boundaries."""

import hashlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest

from app.domain import ConfigError, DatasetConfig, SourceUnavailableError
from app.infrastructure.http import HTTPFetcher


def config(**overrides):
    source = DatasetConfig(
        dataset_id="test",
        provider="Synthetic",
        adapter="lor",
        landing_url="https://provider.example/",
        resource_url="https://provider.example/data",
        allowed_hosts=("provider.example",),
        license_id="CC0-1.0",
        license_url="https://creativecommons.org/publicdomain/zero/1.0/",
        attribution="Synthetic test data",
        boundary_edition="synthetic",
    )
    return replace(source, **overrides)


@pytest.mark.asyncio
async def test_stream_hash_and_metadata(tmp_path):
    content = b"some synthetic data\n"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200,
                content=content,
                headers={"content-type": "text/csv; charset=utf-8", "etag": '"edition"'},
            )
        )
    ) as client:
        asset = await HTTPFetcher(client).download(config(), tmp_path / "data")
    assert asset.sha256 == hashlib.sha256(content).hexdigest()
    assert asset.size_bytes == len(content)
    assert asset.media_type == "text/csv"
    assert asset.etag == '"edition"'


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
async def test_transient_failure_retries_at_most_three_times(tmp_path, monkeypatch, status):
    calls, delays = [], []

    async def sleep(delay):
        delays.append(delay)

    def respond(request):
        calls.append(request)
        return httpx.Response(status, headers={"retry-after": "2"})

    monkeypatch.setattr("app.infrastructure.http.asyncio.sleep", sleep)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(SourceUnavailableError, match="three attempts"):
            await HTTPFetcher(client).download(config(), tmp_path / "data")
    assert len(calls) == 3
    assert delays == [2.0, 2.0]


@pytest.mark.asyncio
async def test_network_failure_retries_then_succeeds(tmp_path, monkeypatch):
    calls = []

    async def sleep(delay):
        pass

    def respond(request):
        calls.append(request)
        if len(calls) < 3:
            raise httpx.ReadTimeout("Synthetic timeout", request=request)
        return httpx.Response(200, content=b"recovered")

    monkeypatch.setattr("app.infrastructure.http.asyncio.sleep", sleep)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        asset = await HTTPFetcher(client).download(config(), tmp_path / "data")
    assert len(calls) == 3
    assert asset.size_bytes == 9


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,content,headers,limit,match",
    [
        (404, b"missing", {}, 100, "HTTP 404"),
        (200, b"<html>landing page</html>", {"content-type": "text/csv"}, 100, "HTML content"),
        (200, b"landing page", {"content-type": "text/html"}, 100, "HTML page"),
        (200, b"oversize", {}, 2, "byte limit"),
        (200, b"", {}, 100, "empty file"),
    ],
)
async def test_permanent_errors_do_not_retry_and_remove_partial_files(
    tmp_path, status, content, headers, limit, match
):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(status, content=content, headers=headers)

    path = tmp_path / "data"
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(SourceUnavailableError, match=match):
            await HTTPFetcher(client).download(config(max_download_bytes=limit), path)
    assert len(calls) == 1
    assert not path.exists()


@pytest.mark.asyncio
async def test_redirect_revalidates_host_even_if_injected_client_follows_redirects(tmp_path):
    calls = []

    def respond(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://unreviewed.example/data"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), follow_redirects=True
    ) as client:
        with pytest.raises(ConfigError, match="allowed provider host"):
            await HTTPFetcher(client).download(config(), tmp_path / "data")
    assert calls == ["https://provider.example/data"]


def test_retry_after_http_date_and_long_delay_budget():
    fetcher = HTTPFetcher(backoff=0.1)
    date = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=10), usegmt=True)
    assert 8 <= fetcher._retry_delay(date, 0) <= 10
    assert fetcher._retry_delay("not a date", 1) == 0.2
    with pytest.raises(SourceUnavailableError, match="ingestion budget"):
        fetcher._retry_delay("3600", 0)


@pytest.mark.parametrize(
    "url",
    [
        "http://provider.example/data",
        "https://user:secret@provider.example/data",
        "https://provider.example:444/data",
        "https://provider.example:invalid/data",
        "https://localhost/data",
        "https://127.0.0.1/data",
        "https://[::1]/data",
    ],
)
def test_url_policy_rejects_local_malformed_and_credentialed_urls(url):
    with pytest.raises(ConfigError):
        HTTPFetcher.validate_url(url, ("provider.example", "localhost", "127.0.0.1", "::1"))
