"""Citation markers in ``report.md``: ``[n]`` (ledger number) or ``[evidence_id]``.

Numbers follow the same rule as the exports (``app.export.run``): the n-th ledger entry that
is not ``unsupported``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from app.evidence.ledger import entry_status

_ID = r"(?:ev|ds|e|f)_?[A-Za-z0-9]+"
_TOKEN = rf"(?:\d+|{_ID})"
# ``[3]``, ``[3, 7]``, ``[ev_…]``; not Markdown links ``[3](...)`` or footnotes ``[^3]``.
CITATION = re.compile(rf"(?<!\\)\[({_TOKEN}(?:\s*,\s*{_TOKEN})*)\](?!\()")
_MARKER = re.compile(r"([ \t]*)" + CITATION.pattern)


@dataclass(frozen=True)
class StripReport:
    text: str
    kept: tuple[str, ...]
    stripped: tuple[str, ...]


def citation_tokens(text: str) -> list[str]:
    return [t.strip() for m in CITATION.finditer(text) for t in m.group(1).split(",")]


def citable(entries: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Map of every accepted marker token (number and id) to its ledger entry."""
    table: dict[str, dict[str, Any]] = {}
    number = 0
    for entry in entries:
        if entry_status(entry) == "unsupported":
            continue
        number += 1
        table[str(number)] = entry
        table[entry["evidence_id"]] = entry
    return table


def strip_unknown_citations(text: str, entries: Iterable[dict[str, Any]]) -> StripReport:
    """Remove markers with no citable ledger entry and report them."""
    allowed = citable(entries)
    kept: list[str] = []
    stripped: list[str] = []

    def replace(match: re.Match[str]) -> str:
        tokens = [t.strip() for t in match.group(2).split(",")]
        good = [t for t in tokens if t in allowed]
        kept.extend(good)
        stripped.extend(t for t in tokens if t not in allowed)
        return f"{match.group(1)}[{', '.join(good)}]" if good else ""

    return StripReport(_MARKER.sub(replace, text), tuple(kept), tuple(stripped))
