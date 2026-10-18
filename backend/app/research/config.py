"""Research configuration and the event callback type."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel

    from app.domain.research import ResearchConnector

Emit = Callable[
    [str, dict], None
]  # (event type from contracts.events, payload); orchestrator adds seq/ts/run_id

DEFAULT_MODEL = "nvidia/Nemotron-3.5-Super-VL"
DEFAULT_SKEPTIC_MODEL = DEFAULT_MODEL


def _env(name: str, default: str) -> Callable[[], str]:
    return lambda: os.environ.get(name, default)


@dataclass
class ResearchConfig:
    model: str = field(default_factory=_env("RISALITE_RESEARCH_MODEL", DEFAULT_MODEL))
    skeptic_model: str = field(
        default_factory=_env("RISALITE_SKEPTIC_MODEL", DEFAULT_SKEPTIC_MODEL)
    )
    writer_model: str = field(default_factory=_env("RISALITE_WRITER_MODEL", DEFAULT_MODEL))
    max_searches: int = 8
    max_extracts: int = 6
    recursion_limit: int = 80
    timeout_s: float = 300.0
    fake: bool = field(default_factory=lambda: os.environ.get("RISALITE_RESEARCH_FAKE") == "1")
    # Optional additions (defaults keep the original interface).
    review_model: str = field(default_factory=_env("RISALITE_REVIEW_MODEL", DEFAULT_MODEL))
    max_results: int = 5
    extract_chars: int = 5000
    max_tool_errors: int = 5
    grace_model_calls: int = 2
    connector: ResearchConnector | None = None
    model_factory: Callable[[str], BaseChatModel] | None = None
