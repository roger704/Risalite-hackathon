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


def pin_citations(text: str, entries: Iterable[dict[str, Any]]) -> str:
    """Replace ``[n]`` with ``[evidence_id]`` so later status changes cannot shift numbers."""
    table = citable(entries)

    def replace(match: re.Match[str]) -> str:
        tokens = [t.strip() for t in match.group(2).split(",")]
        pinned = [table[t]["evidence_id"] if t in table else t for t in tokens]
        return f"{match.group(1)}[{', '.join(pinned)}]"

    return _MARKER.sub(replace, text)


_SENTENCE = re.compile(r"(?<=[.!?])[ \t]+")


def render_citations(
    text: str, entries: Iterable[dict[str, Any]], exclude: Iterable[str] = ()
) -> StripReport:
    """Render pinned markers as current ledger numbers.

    Markers whose entry is unknown, unsupported or in ``exclude`` (evidence backing only
    unsupported findings) are removed; a sentence that loses all of its markers is dropped.
    """
    table = citable(entries)
    excluded = set(exclude)
    number = {e["evidence_id"]: n for n, e in table.items() if n.isdigit()}
    kept: list[str] = []
    stripped: list[str] = []

    def replace(match: re.Match[str]) -> str:
        tokens = [t.strip() for t in match.group(2).split(",")]
        good = []
        for token in tokens:
            entry = table.get(token)
            if entry is None or entry["evidence_id"] in excluded:
                stripped.append(token)
            else:
                good.append(number[entry["evidence_id"]])
        kept.extend(good)
        good = list(dict.fromkeys(good))
        return f"{match.group(1)}[{', '.join(good)}]" if good else ""

    lines = []
    for line in text.split("\n"):
        sentences = []
        for sentence in _SENTENCE.split(line):
            had_markers = bool(CITATION.search(sentence))
            rendered = _MARKER.sub(replace, sentence)
            if had_markers and not CITATION.search(rendered):
                continue
            sentences.append(rendered)
        if line.strip() and not sentences:
            continue
        lines.append(" ".join(sentences))
    return StripReport("\n".join(lines), tuple(kept), tuple(stripped))
