import json
from datetime import datetime, timezone

import httpx
import pytest

from app.connectors.tavily import TavilyClient, TavilyConfig, validate_public_url
from app.domain.errors import ConfigError, ConnectorError, SourceUnavailableError
from app.domain.research import (
    BudgetExceededError,
    ExtractionStatus,
    ResearchBudget,
    ResearchExtract,
    ResearchSearch,
    UnsafeURLError,
)


def config(**changes):
    values = {"api_key": "secret-key"}
    return TavilyConfig(**(values | changes))


async def public_resolver(host, port):
    return ["93.184.216.34"]


def response(data, status=200, headers=None):
    return httpx.Response(status, json=data, headers=headers)


def test_config_separates_http_and_extract_timeouts():
    assert config(timeout_seconds=90).timeout_seconds == 90
    assert config(timeout_seconds=10, extract_timeout_seconds=9.5).extract_timeout_seconds == 9.5
    for invalid in (0.5, 60.1, float("inf"), True):
        with pytest.raises(ConfigError):
            config(timeout_seconds=90, extract_timeout_seconds=invalid)
    for http_timeout, extract_timeout in ((0, None), (30, 30), (30, 40)):
        with pytest.raises(ConfigError):
            config(timeout_seconds=http_timeout, extract_timeout_seconds=extract_timeout)


def test_config_rejects_unallowlisted_base_host_without_exposing_key():
    with pytest.raises(ConfigError) as exc:
        config(base_url="https://attacker.example")
    assert "secret-key" not in str(exc.value)
    assert (
        config(
            base_url="https://custom.example",
            allowed_hosts=("custom.example",),
        ).base_url
        == "https://custom.example"
    )


@pytest.mark.asyncio
async def test_search_maps_fields_caches_per_request_ids_and_sends_auth_header():
    requests = []

    async def handler(request):
        requests.append(request)
        return response(
            {
                "request_id": "req-1",
                "usage": {"credits": 1},
                "results": [
                    {
                        "title": "Title",
                        "url": "https://Example.com/a",
                        "content": "Search snippet",
                        "score": 0.75,
                        "published_date": "Tue, 11 Mar 2025 17:00:00 GMT",
                    }
                ],
            }
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    budget = ResearchBudget(1, 0, 0, 0)
    connector = TavilyClient(
        config(), budget, client=http, clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc)
    )
    first = await connector.search(ResearchSearch("query", candidate_id="one"))
    second = await connector.search(ResearchSearch("query", candidate_id="two"))
    assert len(requests) == 1
    assert requests[0].headers["Authorization"] == "Bearer secret-key"
    item = first.evidence[0]
    assert item.snippet == "Search snippet"
    assert item.passage is None
    assert item.extraction_status is ExtractionStatus.NOT_ATTEMPTED
    assert item.verification_status == "unverified"
    assert item.published_at == datetime(2025, 3, 11, 17, 0, tzinfo=timezone.utc)
    assert second.cached is True
    assert second.evidence[0].candidate_id == "two"
    assert first.evidence[0].candidate_id == "one"
    assert budget.searches_used == 1
    assert "secret-key" not in repr(config())
    await http.aclose()


@pytest.mark.asyncio
async def test_search_cache_preserves_acquisition_timestamp():
    timestamps = iter(
        (
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 2, tzinfo=timezone.utc),
        )
    )

    async def handler(request):
        return response({"results": [{"url": "https://example.com", "content": "snippet"}]})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    connector = TavilyClient(
        config(),
        ResearchBudget(1, 0, 0, 0),
        client=http,
        clock=lambda: next(timestamps),
    )
    first = await connector.search(ResearchSearch("same query"))
    second = await connector.search(ResearchSearch("same query"))
    assert first.evidence[0].retrieved_at == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert second.evidence[0].retrieved_at == first.evidence[0].retrieved_at
    await http.aclose()


@pytest.mark.asyncio
async def test_search_invalid_port_becomes_partial_url_failure():
    async def handler(request):
        return response(
            {"results": [{"url": "https://example.com:99999/page", "content": "snippet"}]}
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    connector = TavilyClient(config(), ResearchBudget(1, 0, 0, 0), client=http)
    page = await connector.search(ResearchSearch("query"))
    assert page.status == "partial"
    assert page.evidence == ()
    assert page.failures == ({"url": "https://example.com:99999/page", "reason": "invalid_url"},)
    await http.aclose()


@pytest.mark.asyncio
async def test_search_budget_exhaustion_does_not_make_second_request():
    requests = 0

    async def handler(request):
        nonlocal requests
        requests += 1
        return response({"results": []})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    connector = TavilyClient(config(), ResearchBudget(1, 0, 0, 0), client=http)
    await connector.search(ResearchSearch("first"))
    with pytest.raises(BudgetExceededError):
        await connector.search(ResearchSearch("second"))
    assert requests == 1
    await http.aclose()


@pytest.mark.asyncio
async def test_extract_maps_partial_provider_results_and_missing_urls():
    urls = ("https://example.com/good", "https://example.com/bad", "https://example.com/missing")

    async def handler(request):
        return response(
            {
                "request_id": "extract-1",
                "usage": {"credits": 2},
                "results": [{"url": urls[0], "raw_content": "Full page"}],
                "failed_results": [{"url": urls[1], "error": "provider says no"}],
            }
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    connector = TavilyClient(
        config(), ResearchBudget(0, 3, 0, 0), client=http, resolver=public_resolver
    )
    page = await connector.extract(ResearchExtract(urls))
    assert page.status == "partial"
    assert page.evidence[0].passage == "Full page"
    assert page.evidence[0].snippet is None
    assert page.evidence[0].extraction_status is ExtractionStatus.OK
    assert page.evidence[0].passage_locator == {
        "type": "char_range",
        "start": 0,
        "end": 9,
        "source_length": 9,
    }
    assert page.evidence[1].failure_reason == "provider says no"
    assert page.evidence[2].failure_reason == "missing_from_provider_response"
    assert {item["reason"] for item in page.failures} == {
        "provider says no",
        "missing_from_provider_response",
    }
    await http.aclose()


@pytest.mark.asyncio
async def test_extract_matches_provider_result_by_canonical_url():
    async def handler(request):
        return response({"results": [{"url": "https://example.com/page", "raw_content": "page"}]})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    connector = TavilyClient(
        config(), ResearchBudget(0, 1, 0, 0), client=http, resolver=public_resolver
    )
    page = await connector.extract(ResearchExtract(("https://example.com/page?utm_source=x",)))
    assert page.status == "complete"
    assert page.evidence[0].extraction_status is ExtractionStatus.OK
    assert page.evidence[0].passage == "page"
    assert page.evidence[0].passage_locator == {
        "type": "char_range",
        "start": 0,
        "end": 4,
        "source_length": 4,
    }
    await http.aclose()


@pytest.mark.asyncio
async def test_extract_empty_content_is_failed_without_losing_other_results():
    urls = (
        "https://example.com/empty",
        "https://example.com/whitespace",
        "https://example.com/full",
    )

    async def handler(request):
        return response(
            {
                "results": [
                    {"url": urls[0], "raw_content": ""},
                    {"url": urls[1], "raw_content": " \t\n "},
                    {"url": urls[2], "raw_content": "Full page"},
                ]
            }
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    connector = TavilyClient(
        config(), ResearchBudget(0, 3, 0, 0), client=http, resolver=public_resolver
    )
    page = await connector.extract(ResearchExtract(urls))
    assert page.status == "partial"
    assert [item.failure_reason for item in page.evidence] == [
        "empty_content",
        "empty_content",
        None,
    ]
    assert page.evidence[2].passage == "Full page"
    assert {item["reason"] for item in page.failures} == {"empty_content"}
    await http.aclose()


@pytest.mark.asyncio
async def test_extract_payload_uses_only_explicit_provider_timeout():
    payloads = []

    async def handler(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        return response({"results": [{"url": payload["urls"][0], "raw_content": "page"}]})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    budget = ResearchBudget(0, 2, 0, 0)
    default_timeout = TavilyClient(
        config(timeout_seconds=90), budget, client=http, resolver=public_resolver
    )
    explicit_timeout = TavilyClient(
        config(timeout_seconds=90, extract_timeout_seconds=12.5),
        budget,
        client=http,
        resolver=public_resolver,
    )
    await default_timeout.extract(ResearchExtract(("https://example.com/default",)))
    await explicit_timeout.extract(ResearchExtract(("https://example.com/explicit",)))
    assert "timeout" not in payloads[0]
    assert payloads[1]["timeout"] == 12.5
    await http.aclose()


@pytest.mark.asyncio
async def test_extract_http_400_failed_results_becomes_failed_page():
    urls = (
        "https://example.com/exact",
        "https://example.com/normalized?utm_source=x",
        "https://example.com/missing",
    )

    async def handler(request):
        return response(
            {
                "detail": {
                    "error": "invalid target",
                    "failed_results": [
                        {"url": urls[0], "error": "not fetched"},
                        {"url": "https://example.com/normalized", "error": "canonical failure"},
                    ],
                }
            },
            status=400,
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    connector = TavilyClient(
        config(), ResearchBudget(0, 3, 0, 0), client=http, resolver=public_resolver
    )
    page = await connector.extract(ResearchExtract(urls))
    assert page.status == "failed"
    assert [item.failure_reason for item in page.evidence] == [
        "not fetched",
        "canonical failure",
        "missing_from_provider_response",
    ]
    assert [item["reason"] for item in page.failures] == [
        "not fetched",
        "canonical failure",
        "missing_from_provider_response",
    ]
    await http.aclose()


@pytest.mark.asyncio
async def test_extract_cache_rebuilds_candidate_ids_and_truncates_passages():
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        return response(
            {"results": [{"url": "https://example.com/page", "raw_content": "full passage"}]}
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    budget = ResearchBudget(0, 1, 0, 0)
    connector = TavilyClient(
        config(max_passage_chars=4), budget, client=http, resolver=public_resolver
    )
    first = await connector.extract(
        ResearchExtract(("https://example.com/page",), candidate_id="first")
    )
    second = await connector.extract(
        ResearchExtract(("https://example.com/page",), candidate_id="second")
    )
    assert calls == 1
    assert first.status == "partial"
    assert first.evidence[0].passage == "full"
    assert first.evidence[0].passage_locator == {
        "type": "char_range",
        "start": 0,
        "end": 4,
        "source_length": 12,
    }
    assert second.cached is True
    assert second.evidence[0].candidate_id == "second"
    assert budget.extract_urls_used == 1
    await http.aclose()


@pytest.mark.asyncio
async def test_extract_rejects_private_and_malformed_urls_without_transport():
    urls = (
        "http://127.0.0.1/",
        "http://10.0.0.5/",
        "http://169.254.169.254/latest",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "ftp://example.com",
        "http://localhost/",
        "https://user:pw@example.com",
        "https://private.example/page",
    )
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        return response({"results": []})

    async def resolver(host, port):
        if host == "private.example":
            return ["93.184.216.10", "192.168.1.10"]
        return ["93.184.216.34"]

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    connector = TavilyClient(config(), ResearchBudget(0, 20, 0, 0), client=http, resolver=resolver)
    page = await connector.extract(ResearchExtract(urls))
    assert calls == 0
    assert page.status == "failed"
    assert len(page.failures) == len(urls)
    assert len(page.evidence) == len(urls)
    for url in urls:
        with pytest.raises(UnsafeURLError):
            await validate_public_url(url, resolver)
    await validate_public_url("https://example.com", resolver)
    await http.aclose()


@pytest.mark.asyncio
async def test_validate_public_url_only_accepts_default_ports():
    with pytest.raises(UnsafeURLError) as exc:
        await validate_public_url("https://example.com:8443/page", public_resolver)
    assert exc.value.message == "non_default_port"
    await validate_public_url("http://example.com:80/page", public_resolver)
    await validate_public_url("https://example.com:443/page", public_resolver)


@pytest.mark.asyncio
async def test_extract_splits_more_than_twenty_urls_into_provider_batches():
    batches = []

    async def handler(request):
        payload = json.loads(request.content)
        batches.append(payload["urls"])
        return response(
            {
                "results": [{"url": url, "raw_content": "content"} for url in payload["urls"]],
                "failed_results": [],
            }
        )

    urls = tuple(f"https://example.com/{index}" for index in range(21))
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    budget = ResearchBudget(0, 21, 0, 0)
    connector = TavilyClient(config(), budget, client=http, resolver=public_resolver)
    page = await connector.extract(ResearchExtract(urls))
    assert [len(batch) for batch in batches] == [20, 1]
    assert len(page.evidence) == 21
    assert budget.extract_urls_used == 21
    await http.aclose()


@pytest.mark.asyncio
async def test_extract_budget_reserves_all_uncached_batches_before_http():
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        return response({"results": []})

    urls = tuple(f"https://example.com/{index}" for index in range(21))
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    connector = TavilyClient(
        config(), ResearchBudget(0, 20, 0, 0), client=http, resolver=public_resolver
    )
    with pytest.raises(BudgetExceededError):
        await connector.extract(ResearchExtract(urls))
    assert calls == 0
    await http.aclose()


@pytest.mark.asyncio
async def test_retry_after_is_honored_and_server_exhaustion_is_retryable():
    delays = []
    calls = 0

    async def record_sleep(delay):
        delays.append(delay)

    async def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return response({}, status=429, headers={"Retry-After": "2"})
        return response({"results": []})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    connector = TavilyClient(
        config(max_retries=1), ResearchBudget(1, 0, 0, 0), client=http, sleep=record_sleep
    )
    result = await connector.search(ResearchSearch("retry"))
    assert result.status == "complete"
    assert delays == [2]
    await http.aclose()

    async def failing(request):
        return response({}, status=503)

    http = httpx.AsyncClient(transport=httpx.MockTransport(failing))

    async def no_sleep(_):
        return None

    connector = TavilyClient(
        config(max_retries=1), ResearchBudget(1, 0, 0, 0), client=http, sleep=no_sleep
    )
    with pytest.raises(SourceUnavailableError):
        await connector.search(ResearchSearch("failure"))
    await http.aclose()


@pytest.mark.asyncio
async def test_provider_error_message_never_exposes_api_key():
    async def handler(request):
        return response({"detail": {"error": "rejected secret-key"}}, status=422)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    connector = TavilyClient(config(), ResearchBudget(1, 0, 0, 0), client=http)
    with pytest.raises(ConnectorError) as exc:
        await connector.search(ResearchSearch("query"))
    assert "secret-key" not in str(exc.value)
    await http.aclose()
