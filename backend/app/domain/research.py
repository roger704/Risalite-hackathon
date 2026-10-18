"""Framework-free contracts for bounded web research and inference."""

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Mapping, Protocol, Sequence, TypeVar
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .errors import ConfigError, ConnectorError, ValidationError


class ExtractionStatus(str, Enum):
    OK = "ok"
    FAILED = "failed"
    PARTIAL = "partial"
    NOT_ATTEMPTED = "not_attempted"


class EvidenceKind(str, Enum):
    SEARCH_RESULT = "search_result"
    EXTRACTED_PAGE = "extracted_page"


def _utc(value: datetime) -> None:
    if not isinstance(value, datetime) or value.utcoffset() != timedelta(0):
        raise ValidationError("retrieved_at must be a timezone-aware UTC datetime")


def _http_url(value: str, name: str) -> None:
    try:
        parsed = urlsplit(value)
        valid = parsed.scheme.lower() in {"http", "https"} and bool(parsed.hostname)
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise ValidationError(f"{name} must be an http(s) URL")


@dataclass(frozen=True)
class WebEvidence:
    evidence_id: str
    kind: EvidenceKind | str
    original_url: str
    canonical_url: str
    title: str | None
    query: str | None
    retrieved_at: datetime
    published_at: datetime | None
    published_at_raw: str | None
    snippet: str | None
    passage: str | None
    passage_locator: dict[str, Any] | None
    extraction_status: ExtractionStatus | str
    failure_reason: str | None
    content_hash: str | None
    candidate_id: str | None
    analysis_id: str | None
    provider: str
    provider_request_id: str | None
    score: float | None
    verification_status: str = "unverified"

    def __post_init__(self) -> None:
        try:
            kind = EvidenceKind(self.kind)
            status = ExtractionStatus(self.extraction_status)
        except ValueError as exc:
            raise ValidationError("Unsupported evidence kind or extraction status") from exc
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "extraction_status", status)
        rejected_url = (
            status is ExtractionStatus.FAILED
            and isinstance(self.failure_reason, str)
            and self.failure_reason.startswith("url_rejected:")
            and isinstance(self.original_url, str)
            and isinstance(self.canonical_url, str)
        )
        if not rejected_url:
            _http_url(self.original_url, "original_url")
            _http_url(self.canonical_url, "canonical_url")
        _utc(self.retrieved_at)
        if status is ExtractionStatus.FAILED and not self.failure_reason:
            raise ValidationError("failed evidence requires failure_reason")
        if status is ExtractionStatus.OK and not (self.passage or self.snippet):
            raise ValidationError("ok evidence requires passage or snippet")
        if self.verification_status != "unverified":
            raise ValidationError("Research evidence must remain unverified")

    def to_dict(self) -> dict[str, Any]:
        def json_value(value: Any) -> Any:
            if isinstance(value, datetime):
                return value.isoformat().replace("+00:00", "Z")
            if isinstance(value, Enum):
                return value.value
            if isinstance(value, dict):
                return {key: json_value(item) for key, item in value.items()}
            if isinstance(value, (tuple, list)):
                return [json_value(item) for item in value]
            return value

        return json_value(asdict(self))


def canonicalize_url(url: str) -> str:
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValidationError("URL has an invalid port") from exc
    netloc = hostname
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        netloc = f"{netloc}:{port}"
    query = urlencode(
        [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key.lower() not in {"gclid", "fbclid"} and not key.lower().startswith("utm_")
        ],
        doseq=True,
    )
    return urlunsplit((scheme, netloc, parsed.path, query, ""))


def make_evidence_id(
    kind: EvidenceKind | str,
    canonical_url: str,
    query: str | None,
    candidate_id: str | None,
    analysis_id: str | None,
) -> str:
    values = [
        kind.value if isinstance(kind, EvidenceKind) else kind,
        canonical_url,
        query,
        candidate_id,
        analysis_id,
    ]
    digest = hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()
    return f"ev_{digest[:20]}"


class BudgetExceededError(ConnectorError):
    """Maps to HTTP 429 later."""

    def __init__(self, resource: str, limit: int, used: int):
        super().__init__(
            "RESEARCH_BUDGET_EXCEEDED",
            f"Research budget exceeded for {resource}: limit {limit}, used {used}",
        )
        self.resource = resource
        self.limit = limit
        self.used = used


class StructuredOutputError(ConnectorError):
    def __init__(self, attempts: int, validation_error: str):
        summary = validation_error[:1000]
        super().__init__("INFERENCE_OUTPUT_INVALID", f"Structured output invalid: {summary}")
        self.attempts = attempts
        self.validation_error = summary
        self.last_error = summary


class UnsafeURLError(ConnectorError):
    def __init__(self, message: str):
        super().__init__("URL_REJECTED", message)


@dataclass
class ResearchBudget:
    max_searches: int
    max_extract_urls: int
    max_inference_calls: int
    max_tokens: int
    searches_used: int = 0
    extract_urls_used: int = 0
    inference_calls_used: int = 0
    tokens_used: int = 0

    def __post_init__(self) -> None:
        for name in (
            "max_searches",
            "max_extract_urls",
            "max_inference_calls",
            "max_tokens",
            "searches_used",
            "extract_urls_used",
            "inference_calls_used",
            "tokens_used",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ConfigError(f"{name} must be a nonnegative integer")
        for used, limit in (
            (self.searches_used, self.max_searches),
            (self.extract_urls_used, self.max_extract_urls),
            (self.inference_calls_used, self.max_inference_calls),
        ):
            if used > limit:
                raise ConfigError("Budget usage cannot exceed its cap")

    def _consume(self, resource: str, amount: int, limit_name: str, used_name: str) -> None:
        if type(amount) is not int or amount < 0:
            raise ConfigError(f"{resource} consumption must be a nonnegative integer")
        limit = getattr(self, limit_name)
        used = getattr(self, used_name)
        if used + amount > limit:
            raise BudgetExceededError(resource, limit, used)
        setattr(self, used_name, used + amount)

    def consume_search(self) -> None:
        self._consume("searches", 1, "max_searches", "searches_used")

    def consume_extract_urls(self, n: int) -> None:
        self._consume("extract_urls", n, "max_extract_urls", "extract_urls_used")

    def consume_inference_call(self) -> None:
        self._consume("inference_calls", 1, "max_inference_calls", "inference_calls_used")

    def record_tokens(self, n: int) -> None:
        """Record provider usage; the final call may overshoot the token cap."""
        if type(n) is not int or n < 0:
            raise ConfigError("tokens must be a nonnegative integer")
        self.tokens_used += n

    def remaining_tokens(self) -> int:
        return max(0, self.max_tokens - self.tokens_used)

    def require_tokens(self) -> None:
        if self.tokens_used >= self.max_tokens:
            raise BudgetExceededError("tokens", self.max_tokens, self.tokens_used)

    def snapshot(self) -> dict[str, int]:
        return {
            "max_searches": self.max_searches,
            "max_extract_urls": self.max_extract_urls,
            "max_inference_calls": self.max_inference_calls,
            "max_tokens": self.max_tokens,
            "searches_used": self.searches_used,
            "extract_urls_used": self.extract_urls_used,
            "inference_calls_used": self.inference_calls_used,
            "tokens_used": self.tokens_used,
        }


@dataclass(frozen=True)
class ResearchSearch:
    query: str
    candidate_id: str | None = None
    analysis_id: str | None = None
    search_depth: str | None = None
    max_results: int | None = None
    include_domains: tuple[str, ...] = ()
    exclude_domains: tuple[str, ...] = ()
    topic: str | None = None
    time_range: str | None = None


@dataclass(frozen=True)
class ResearchExtract:
    urls: tuple[str, ...]
    query: str | None = None
    candidate_id: str | None = None
    analysis_id: str | None = None
    extract_depth: str | None = None


@dataclass(frozen=True)
class EvidencePage:
    evidence: tuple[WebEvidence, ...]
    status: str
    failures: tuple[dict[str, str], ...] = ()
    request_ids: tuple[str, ...] = ()
    cached: bool = False
    credits: float | None = None


class ResearchConnector(Protocol):
    async def search(self, request: ResearchSearch) -> EvidencePage: ...

    async def extract(self, request: ResearchExtract) -> EvidencePage: ...


T = TypeVar("T", bound=Any)


class InferenceGateway(Protocol):
    async def plan(
        self,
        context: Mapping[str, Any],
        output_model: type[T],
        evidence: Sequence[WebEvidence] = (),
    ) -> T: ...

    async def synthesize(
        self,
        context: Mapping[str, Any],
        output_model: type[T],
        evidence: Sequence[WebEvidence] = (),
    ) -> T: ...

    async def review(
        self,
        context: Mapping[str, Any],
        output_model: type[T],
        evidence: Sequence[WebEvidence] = (),
    ) -> T: ...
