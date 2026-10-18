"""Tavily search and extraction adapter with bounded, SSRF-aware requests."""

import asyncio
import hashlib
import ipaddress
import json
import math
import os
import socket
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from inspect import isawaitable
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.domain.errors import (
    ConfigError,
    ConnectorError,
    SchemaError,
    SourceUnavailableError,
    ValidationError,
)
from app.domain.research import (
    EvidenceKind,
    EvidencePage,
    ExtractionStatus,
    ResearchBudget,
    ResearchExtract,
    ResearchSearch,
    UnsafeURLError,
    WebEvidence,
    canonicalize_url,
    make_evidence_id,
)


@dataclass(frozen=True)
class TavilyConfig:
    api_key: str = field(repr=False)
    base_url: str = "https://api.tavily.com"
    allowed_hosts: tuple[str, ...] = ("api.tavily.com",)
    search_depth: str = "basic"
    max_results: int = 5
    topic: str = "general"
    include_domains: tuple[str, ...] = ()
    exclude_domains: tuple[str, ...] = ()
    chunks_per_source: int | None = None
    extract_depth: str = "basic"
    extract_format: str = "markdown"
    timeout_seconds: float = 30.0
    extract_timeout_seconds: float | None = None
    max_retries: int = 3
    backoff_seconds: float = 0.5
    max_retry_after_seconds: int = 30
    max_passage_chars: int = 20000

    def __post_init__(self) -> None:
        if not isinstance(self.api_key, str) or not self.api_key.strip():
            raise ConfigError("TAVILY_API_KEY is not set")
        try:
            parsed = urlsplit(self.base_url)
        except ValueError as exc:
            raise ConfigError("Tavily base_url must be https") from exc
        allowed_hosts = {
            host.rstrip(".").lower() for host in self.allowed_hosts if isinstance(host, str)
        }
        if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.hostname.rstrip(".").lower() not in allowed_hosts
        ):
            raise ConfigError("Tavily base_url must use an allowed HTTPS host")
        if self.search_depth not in {"advanced", "basic", "fast", "ultra-fast"}:
            raise ConfigError("Invalid Tavily search_depth")
        if type(self.max_results) is not int or not 0 <= self.max_results <= 20:
            raise ConfigError("Tavily max_results must be between 0 and 20")
        if self.topic not in {"general", "news", "finance"}:
            raise ConfigError("Invalid Tavily topic")
        if len(self.include_domains) > 300 or len(self.exclude_domains) > 150:
            raise ConfigError("Tavily domain filter exceeds provider limits")
        if self.chunks_per_source is not None and (
            type(self.chunks_per_source) is not int or not 1 <= self.chunks_per_source <= 3
        ):
            raise ConfigError("Tavily chunks_per_source must be between 1 and 3")
        if self.extract_depth not in {"basic", "advanced"}:
            raise ConfigError("Invalid Tavily extract_depth")
        if self.extract_format not in {"markdown", "text"}:
            raise ConfigError("Invalid Tavily extract_format")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ConfigError("Tavily timeout_seconds must be finite and positive")
        if self.extract_timeout_seconds is not None:
            if (
                isinstance(self.extract_timeout_seconds, bool)
                or not isinstance(self.extract_timeout_seconds, (int, float))
                or not math.isfinite(self.extract_timeout_seconds)
                or not 1 <= self.extract_timeout_seconds <= 60
            ):
                raise ConfigError("Tavily extract_timeout_seconds must be between 1 and 60")
            if self.timeout_seconds <= self.extract_timeout_seconds:
                raise ConfigError("Tavily HTTP timeout_seconds must exceed extract_timeout_seconds")
        for name in ("max_retries", "max_retry_after_seconds", "max_passage_chars"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ConfigError(f"Tavily {name} must be a nonnegative integer")
        if (
            isinstance(self.backoff_seconds, bool)
            or not isinstance(self.backoff_seconds, (int, float))
            or not math.isfinite(self.backoff_seconds)
            or self.backoff_seconds < 0
        ):
            raise ConfigError("Tavily backoff_seconds must be nonnegative")

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ, **overrides: Any) -> "TavilyConfig":
        api_key = overrides.pop("api_key", env.get("TAVILY_API_KEY", ""))
        if not api_key:
            raise ConfigError("TAVILY_API_KEY is not set")
        return cls(api_key=api_key, **overrides)


async def _default_resolver(host: str, port: int) -> list[tuple]:
    return await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)


def _address(value: Any) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    if isinstance(value, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
        return value
    if isinstance(value, str):
        return ipaddress.ip_address(value)
    if isinstance(value, tuple) and value:
        address = value[4] if len(value) >= 5 else value[0]
        if isinstance(address, tuple):
            address = address[0]
        return ipaddress.ip_address(address)
    if isinstance(value, dict) and "address" in value:
        return ipaddress.ip_address(value["address"])
    raise ValueError("unrecognized DNS address")


async def validate_public_url(url: str, resolver: Any = None) -> None:
    """Reject non-public targets before asking Tavily to fetch them.

    Tavily, not this adapter, fetches the page. This check prevents us from asking
    it to fetch internal/private targets and protects any later direct fetch.
    """
    try:
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        host = parsed.hostname
        explicit_port = parsed.port
        port = explicit_port if explicit_port is not None else (443 if scheme == "https" else 80)
    except (AttributeError, TypeError, ValueError) as exc:
        raise UnsafeURLError("invalid_url") from exc
    if scheme not in {"http", "https"}:
        raise UnsafeURLError("unsupported_scheme")
    if explicit_port is not None and explicit_port != (443 if scheme == "https" else 80):
        raise UnsafeURLError("non_default_port")
    if not host:
        raise UnsafeURLError("hostname_required")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeURLError("userinfo_not_allowed")
    normalized_host = host.rstrip(".").lower()
    if (
        normalized_host == "localhost"
        or normalized_host.endswith(".localhost")
        or normalized_host.endswith(".local")
        or normalized_host.endswith(".internal")
        or normalized_host.endswith(".home.arpa")
    ):
        raise UnsafeURLError("non_public_hostname")
    try:
        literal = ipaddress.ip_address(normalized_host)
    except ValueError:
        literal = None
    if literal is not None:
        literal = literal.ipv4_mapped if isinstance(literal, ipaddress.IPv6Address) else literal
        if literal is None or not literal.is_global:
            raise UnsafeURLError("non_public_ip")
        return
    try:
        lookup = resolver or _default_resolver
        resolved = lookup(normalized_host, port)
        if isawaitable(resolved):
            resolved = await resolved
        addresses = [_address(item) for item in resolved]
        if not addresses:
            raise ValueError("empty DNS response")
        if any(
            not (
                (address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) else address)
                is not None
                and (
                    address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) else address
                ).is_global
            )
            for address in addresses
        ):
            raise UnsafeURLError("non_public_ip")
    except UnsafeURLError:
        raise
    except Exception as exc:
        raise UnsafeURLError("dns_resolution_failed") from exc


class TavilyClient:
    def __init__(
        self,
        config: TavilyConfig,
        budget: ResearchBudget,
        client: httpx.AsyncClient | None = None,
        resolver: Any = None,
        sleep: Any = asyncio.sleep,
        clock: Any = lambda: datetime.now(timezone.utc),
    ):
        self.config = config
        self.budget = budget
        self._client = client
        self._owns_client = client is None
        self.resolver = resolver
        self.sleep = sleep
        self.clock = clock
        self._cache: dict[str, tuple[datetime, dict[str, Any]]] = {}

    async def __aenter__(self) -> "TavilyClient":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _http_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                follow_redirects=False,
                timeout=self.config.timeout_seconds,
            )
        return self._client

    def _cache_key(self, payload: dict[str, Any]) -> str:
        return json.dumps(payload, sort_keys=True)

    async def _post(
        self, endpoint: str, payload: dict[str, Any], *, extract: bool
    ) -> dict[str, Any]:
        client = await self._http_client()
        url = f"{self.config.base_url.rstrip('/')}/{endpoint}"
        headers = {"Authorization": f"Bearer {self.config.api_key}"}
        retry_statuses = {429, 500, 502, 503, 504}
        for attempt in range(self.config.max_retries + 1):
            try:
                response = await client.post(url, json=payload, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt >= self.config.max_retries:
                    raise SourceUnavailableError("Tavily request failed after retries") from exc
                await self.sleep(self.config.backoff_seconds * (2**attempt))
                continue
            if response.status_code in retry_statuses:
                if attempt >= self.config.max_retries:
                    raise SourceUnavailableError("Tavily provider unavailable after retries")
                delay = self.config.backoff_seconds * (2**attempt)
                retry_after = response.headers.get("Retry-After")
                if retry_after is not None:
                    try:
                        delay = max(0, min(int(retry_after), self.config.max_retry_after_seconds))
                    except ValueError:
                        pass
                await self.sleep(delay)
                continue
            if response.status_code in {401, 403}:
                raise ConnectorError("RESEARCH_AUTH_FAILED", "Tavily authentication failed")
            if response.status_code in {432, 433}:
                raise ConnectorError("RESEARCH_PROVIDER_LIMIT", "Tavily provider limit reached")
            if 400 <= response.status_code < 500:
                try:
                    data = response.json()
                except ValueError:
                    data = {}
                detail = data.get("detail", {}) if isinstance(data, dict) else {}
                if extract and response.status_code == 400 and isinstance(detail, dict):
                    failed_results = detail.get("failed_results")
                    if isinstance(failed_results, list):
                        return {
                            "__extract_http_400__": True,
                            "failed_results": failed_results,
                            "request_id": data.get("request_id"),
                        }
                error = detail.get("error", "") if isinstance(detail, dict) else ""
                message = str(error)[:300].replace(self.config.api_key, "[redacted]")
                raise ConnectorError("RESEARCH_REQUEST_INVALID", message)
            try:
                data = response.json()
            except ValueError as exc:
                raise SchemaError("Tavily returned a non-JSON response") from exc
            if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                raise SchemaError("Tavily response is missing results")
            return data
        raise SourceUnavailableError("Tavily provider unavailable after retries")

    @staticmethod
    def _credits(data: dict[str, Any]) -> float | None:
        usage = data.get("usage")
        credits = usage.get("credits") if isinstance(usage, dict) else None
        return float(credits) if isinstance(credits, (int, float)) else None

    @staticmethod
    def _request_id(data: dict[str, Any]) -> tuple[str, ...]:
        request_id = data.get("request_id")
        return (str(request_id),) if request_id is not None else ()

    def _evidence(
        self,
        *,
        kind: EvidenceKind,
        url: str,
        query: str | None,
        candidate_id: str | None,
        analysis_id: str | None,
        data: dict[str, Any],
        status: ExtractionStatus,
        snippet: str | None = None,
        passage: str | None = None,
        locator: dict[str, Any] | None = None,
        reason: str | None = None,
        content_hash: str | None = None,
        published_at: datetime | None = None,
        published_at_raw: str | None = None,
        score: float | None = None,
        retrieved_at: datetime | None = None,
    ) -> WebEvidence:
        canonical = canonicalize_url(url)
        return WebEvidence(
            evidence_id=make_evidence_id(kind, canonical, query, candidate_id, analysis_id),
            kind=kind,
            original_url=url,
            canonical_url=canonical,
            title=data.get("title"),
            query=query,
            retrieved_at=retrieved_at if retrieved_at is not None else self.clock(),
            published_at=published_at,
            published_at_raw=published_at_raw,
            snippet=snippet,
            passage=passage,
            passage_locator=locator,
            extraction_status=status,
            failure_reason=reason,
            content_hash=content_hash,
            candidate_id=candidate_id,
            analysis_id=analysis_id,
            provider="tavily",
            provider_request_id=str(data["request_id"])
            if data.get("request_id") is not None
            else None,
            score=score,
        )

    @staticmethod
    def _canonical_result_url(url: str) -> str | None:
        try:
            return canonicalize_url(url)
        except (TypeError, ValueError, ValidationError):
            return None

    async def search(self, request: ResearchSearch) -> EvidencePage:
        payload = {
            "query": request.query,
            "search_depth": request.search_depth or self.config.search_depth,
            "max_results": self.config.max_results
            if request.max_results is None
            else request.max_results,
            "topic": request.topic or self.config.topic,
            "include_domains": list(request.include_domains or self.config.include_domains),
            "exclude_domains": list(request.exclude_domains or self.config.exclude_domains),
            "include_usage": True,
        }
        if request.time_range is not None:
            payload["time_range"] = request.time_range
        if self.config.chunks_per_source is not None:
            payload["chunks_per_source"] = self.config.chunks_per_source
        key = self._cache_key(payload)
        cached = key in self._cache
        if cached:
            fetched_at, data = self._cache[key]
        else:
            self.budget.consume_search()
            data = await self._post("search", payload, extract=False)
            fetched_at = self.clock()
            self._cache[key] = (fetched_at, data)
        evidence: list[WebEvidence] = []
        failures: list[dict[str, str]] = []
        for result in data["results"]:
            if not isinstance(result, dict):
                raise SchemaError("Tavily search result is not an object")
            url = result.get("url")
            if not isinstance(url, str):
                failures.append({"url": str(url or ""), "reason": "invalid_url"})
                continue
            try:
                canonicalize_url(url)
                if urlsplit(url).scheme.lower() not in {"http", "https"}:
                    raise ValueError("unsupported scheme")
                if not urlsplit(url).hostname:
                    raise ValueError("missing hostname")
            except (TypeError, ValueError, ValidationError):
                failures.append({"url": url, "reason": "invalid_url"})
                continue
            content = result.get("content")
            if content is not None and not isinstance(content, str):
                raise SchemaError("Tavily search content is not text")
            raw_date = result.get("published_date")
            published_at = None
            if isinstance(raw_date, str):
                try:
                    published_at = parsedate_to_datetime(raw_date)
                except (TypeError, ValueError, OverflowError):
                    try:
                        published_at = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
                    except ValueError:
                        published_at = None
            evidence.append(
                self._evidence(
                    kind=EvidenceKind.SEARCH_RESULT,
                    url=url,
                    query=request.query,
                    candidate_id=request.candidate_id,
                    analysis_id=request.analysis_id,
                    data=result | {"request_id": data.get("request_id")},
                    status=ExtractionStatus.NOT_ATTEMPTED,
                    snippet=content,
                    content_hash=hashlib.sha256(content.encode()).hexdigest()
                    if content is not None
                    else None,
                    published_at=published_at,
                    published_at_raw=raw_date if isinstance(raw_date, str) else None,
                    score=result.get("score"),
                    retrieved_at=fetched_at,
                )
            )
        return EvidencePage(
            evidence=tuple(evidence),
            status="partial" if failures else "complete",
            failures=tuple(failures),
            request_ids=self._request_id(data),
            cached=cached,
            credits=self._credits(data),
        )

    def _failed_evidence(
        self,
        url: str,
        reason: str,
        request: ResearchExtract,
        request_id: Any = None,
        retrieved_at: datetime | None = None,
    ) -> WebEvidence | None:
        try:
            canonical = canonicalize_url(url)
        except (TypeError, ValueError):
            return None
        return WebEvidence(
            evidence_id=make_evidence_id(
                EvidenceKind.EXTRACTED_PAGE,
                canonical,
                request.query,
                request.candidate_id,
                request.analysis_id,
            ),
            kind=EvidenceKind.EXTRACTED_PAGE,
            original_url=url,
            canonical_url=canonical,
            title=None,
            query=request.query,
            retrieved_at=retrieved_at if retrieved_at is not None else self.clock(),
            published_at=None,
            published_at_raw=None,
            snippet=None,
            passage=None,
            passage_locator=None,
            extraction_status=ExtractionStatus.FAILED,
            failure_reason=reason,
            content_hash=None,
            candidate_id=request.candidate_id,
            analysis_id=request.analysis_id,
            provider="tavily",
            provider_request_id=str(request_id) if request_id is not None else None,
            score=None,
        )

    def _extract_payload(self, request: ResearchExtract, urls: tuple[str, ...]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "urls": list(urls),
            "extract_depth": request.extract_depth or self.config.extract_depth,
            "format": self.config.extract_format,
            "include_usage": True,
        }
        if self.config.extract_timeout_seconds is not None:
            payload["timeout"] = self.config.extract_timeout_seconds
        if request.query is not None:
            payload["query"] = request.query
            if self.config.chunks_per_source is not None:
                payload["chunks_per_source"] = self.config.chunks_per_source
        return payload

    @staticmethod
    def _index_extract_entries(
        items: list[Any], label: str
    ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        by_url: dict[str, dict[str, Any]] = {}
        by_canonical_url: dict[str, dict[str, Any]] = {}
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("url"), str):
                raise SchemaError(f"Tavily {label} is not an object")
            url = item["url"]
            by_url[url] = item
            canonical_url = TavilyClient._canonical_result_url(url)
            if canonical_url is not None:
                by_canonical_url.setdefault(canonical_url, item)
        return by_url, by_canonical_url

    def _match_extract_entry(
        self,
        url: str,
        by_url: dict[str, dict[str, Any]],
        by_canonical_url: dict[str, dict[str, Any]],
    ) -> dict[str, Any] | None:
        item = by_url.get(url)
        if item is not None:
            return item
        canonical_url = self._canonical_result_url(url)
        return by_canonical_url.get(canonical_url) if canonical_url is not None else None

    async def _extract_batch(self, request: ResearchExtract, urls: tuple[str, ...]) -> EvidencePage:
        payload = self._extract_payload(request, urls)
        key = self._cache_key(payload)
        cached = key in self._cache
        if cached:
            fetched_at, data = self._cache[key]
        else:
            data = await self._post("extract", payload, extract=True)
            fetched_at = self.clock()
            self._cache[key] = (fetched_at, data)
        if data.get("__extract_http_400__"):
            failures: list[dict[str, str]] = []
            evidence: list[WebEvidence] = []
            failed_by_url, failed_by_canonical_url = self._index_extract_entries(
                data["failed_results"], "failed result"
            )
            for url in urls:
                item = self._match_extract_entry(url, failed_by_url, failed_by_canonical_url)
                reason = (
                    str(item.get("error", "provider_error"))
                    if item is not None
                    else "missing_from_provider_response"
                )
                failures.append({"url": url, "reason": reason})
                failed = self._failed_evidence(
                    url, reason, request, data.get("request_id"), retrieved_at=fetched_at
                )
                if failed is not None:
                    evidence.append(failed)
            return EvidencePage(
                tuple(evidence), "failed", tuple(failures), self._request_id(data), cached, None
            )
        provider_results = data["results"]
        failed_results = data.get("failed_results", [])
        if not isinstance(failed_results, list):
            raise SchemaError("Tavily failed_results is not a list")
        result_by_url, result_by_canonical_url = self._index_extract_entries(
            provider_results, "extraction result"
        )
        failed_by_url, failed_by_canonical_url = self._index_extract_entries(
            failed_results, "failed result"
        )
        evidence: list[WebEvidence] = []
        failures: list[dict[str, str]] = []
        for url in urls:
            result = result_by_url.get(url)
            failed_result = failed_by_url.get(url)
            if result is None and failed_result is None:
                canonical_url = self._canonical_result_url(url)
                if canonical_url is not None:
                    result = result_by_canonical_url.get(canonical_url)
                    failed_result = failed_by_canonical_url.get(canonical_url)
            if result is not None:
                raw_content = result.get("raw_content")
                if not isinstance(raw_content, str):
                    raise SchemaError("Tavily extraction result is missing raw_content")
                if not raw_content.strip():
                    reason = "empty_content"
                    failures.append({"url": url, "reason": reason})
                    evidence.append(
                        self._failed_evidence(
                            url,
                            reason,
                            request,
                            data.get("request_id"),
                            retrieved_at=fetched_at,
                        )
                    )
                    continue
                truncated = len(raw_content) > self.config.max_passage_chars
                passage = raw_content[: self.config.max_passage_chars]
                evidence.append(
                    self._evidence(
                        kind=EvidenceKind.EXTRACTED_PAGE,
                        url=url,
                        query=request.query,
                        candidate_id=request.candidate_id,
                        analysis_id=request.analysis_id,
                        data=result | {"request_id": data.get("request_id")},
                        status=ExtractionStatus.PARTIAL if truncated else ExtractionStatus.OK,
                        passage=passage,
                        locator={
                            "type": "char_range",
                            "start": 0,
                            "end": len(passage),
                            "source_length": len(raw_content),
                        },
                        content_hash=hashlib.sha256(raw_content.encode()).hexdigest(),
                        retrieved_at=fetched_at,
                    )
                )
            elif failed_result is not None:
                reason = str(failed_result.get("error", "provider_error"))
                failures.append({"url": url, "reason": reason})
                evidence.append(
                    self._failed_evidence(
                        url, reason, request, data.get("request_id"), retrieved_at=fetched_at
                    )  # type: ignore[arg-type]
                )
            else:
                reason = "missing_from_provider_response"
                failures.append({"url": url, "reason": reason})
                evidence.append(
                    self._failed_evidence(
                        url, reason, request, data.get("request_id"), retrieved_at=fetched_at
                    )  # type: ignore[arg-type]
                )
        successes = sum(
            item.extraction_status in {ExtractionStatus.OK, ExtractionStatus.PARTIAL}
            for item in evidence
        )
        if not successes:
            status = "failed"
        elif successes == len(evidence) and all(
            item.extraction_status is ExtractionStatus.OK for item in evidence
        ):
            status = "complete"
        else:
            status = "partial"
        return EvidencePage(
            evidence=tuple(evidence),
            status=status,
            failures=tuple(failures),
            request_ids=self._request_id(data),
            cached=cached,
            credits=self._credits(data),
        )

    async def extract(self, request: ResearchExtract) -> EvidencePage:
        failures: list[dict[str, str]] = []
        evidence: list[WebEvidence] = []
        accepted: list[str] = []
        seen: set[str] = set()
        for url in request.urls:
            try:
                await validate_public_url(url, self.resolver)
                canonical = canonicalize_url(url)
            except UnsafeURLError as exc:
                reason = f"url_rejected: {exc.message}"
                failures.append({"url": url, "reason": reason})
                rejected = self._failed_evidence(url, reason, request)
                if rejected is not None:
                    evidence.append(rejected)
                continue
            if canonical not in seen:
                seen.add(canonical)
                accepted.append(url)
        batches = [tuple(accepted[start : start + 20]) for start in range(0, len(accepted), 20)]
        batch_keys = [self._cache_key(self._extract_payload(request, batch)) for batch in batches]
        uncached_batch_indices = {
            index for index, key in enumerate(batch_keys) if key not in self._cache
        }
        uncached_url_count = sum(
            len(batch) for index, batch in enumerate(batches) if index in uncached_batch_indices
        )
        if uncached_url_count:
            self.budget.consume_extract_urls(uncached_url_count)
        uncached_units_not_started = uncached_url_count
        batch_cached: list[bool] = []
        request_ids: list[str] = []
        credit_values: list[float] = []
        try:
            for index, batch in enumerate(batches):
                if index in uncached_batch_indices:
                    uncached_units_not_started -= len(batch)
                    if batch_keys[index] in self._cache:
                        self.budget.release_extract_urls(len(batch))
                page = await self._extract_batch(request, batch)
                evidence.extend(page.evidence)
                failures.extend(page.failures)
                batch_cached.append(page.cached)
                request_ids.extend(page.request_ids)
                if page.credits is not None:
                    credit_values.append(page.credits)
        except BaseException:
            if uncached_units_not_started:
                self.budget.release_extract_urls(uncached_units_not_started)
            raise
        if not accepted and not evidence:
            return EvidencePage(
                (), "failed" if failures else "complete", tuple(failures), (), False, None
            )
        if not accepted:
            status = (
                "failed"
                if all(item.extraction_status is ExtractionStatus.FAILED for item in evidence)
                else "partial"
            )
            return EvidencePage(tuple(evidence), status, tuple(failures))
        # A provider result marked partial (truncated) also makes the aggregate partial.
        okay = sum(
            item.extraction_status in {ExtractionStatus.OK, ExtractionStatus.PARTIAL}
            for item in evidence
        )
        status = (
            "failed"
            if okay == 0
            else "complete"
            if okay == len(evidence)
            and all(item.extraction_status is ExtractionStatus.OK for item in evidence)
            and not failures
            else "partial"
        )
        return EvidencePage(
            evidence=tuple(evidence),
            status=status,
            failures=tuple(failures),
            request_ids=tuple(request_ids),
            cached=all(batch_cached),
            credits=sum(credit_values) if credit_values else None,
        )
