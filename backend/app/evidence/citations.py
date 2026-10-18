"""Writer post-processor: ``[n]`` markers must map to ledger entry ``e<n>``."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from app.contracts.evidence import LedgerEntry

# ``[3]``, ``[3, 7]``, ``[3][7]``; not Markdown links ``[3](...)`` or footnotes ``[^3]``.
CITATION = re.compile(r"(?<!\\)\[(\d+(?:\s*,\s*\d+)*)\](?!\()")
_MARKER = re.compile(r"([ \t]*)" + CITATION.pattern)


@dataclass(frozen=True)
class StripReport:
    text: str
    kept: tuple[int, ...]
    stripped: tuple[int, ...]


def citation_numbers(text: str) -> list[int]:
    return [int(n) for match in CITATION.finditer(text) for n in match.group(1).split(",")]


def citable_numbers(entries: Iterable[LedgerEntry]) -> set[int]:
    return {entry.citation_number for entry in entries if entry.status != "unsupported"}


def strip_unknown_citations(text: str, entries: Iterable[LedgerEntry]) -> StripReport:
    """Remove ``[n]`` with no ledger entry ``e<n>`` (or an unsupported one); report them."""
    allowed = citable_numbers(entries)
    kept: list[int] = []
    stripped: list[int] = []

    def replace(match: re.Match[str]) -> str:
        numbers = [int(n) for n in match.group(2).split(",")]
        good = [n for n in numbers if n in allowed]
        kept.extend(good)
        stripped.extend(n for n in numbers if n not in allowed)
        return f"{match.group(1)}[{', '.join(map(str, good))}]" if good else ""

    cleaned = _MARKER.sub(replace, text)
    return StripReport(cleaned, tuple(kept), tuple(stripped))
