"""Fuzzy quote matching with the standard library (``difflib``), no extra dependency.

``quote_ratio`` is a partial ratio: the quote is aligned against the best same-length window
of the fetched text. Quotes with ellipses (``…``/``...``) are split into fragments that are
matched separately and combined weighted by length.
"""

from __future__ import annotations

import html
import re
import unicodedata
from difflib import SequenceMatcher

VERIFIED_RATIO = 0.85
PARTIAL_RATIO = 0.60

_QUOTES = dict.fromkeys(map(ord, "„“”\"'‚‘’«»‹›`´"), " ")
_ELLIPSIS = re.compile(r"…|\.\.\.+|\[\s*…\s*\]|\[\.\.\.\]")
_TAGS = re.compile(r"<(script|style)\b.*?</\1>|<[^>]+>", re.S | re.I)
_SPACE = re.compile(r"\s+")
_MIN_FRAGMENT = 8


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(_QUOTES).casefold()
    text = text.replace("\u00ad", "").replace("ß", "ss")
    return _SPACE.sub(" ", text).strip()


def html_to_text(raw: str) -> str:
    return html.unescape(_TAGS.sub(" ", raw))


def _partial_ratio(needle: str, haystack: str) -> float:
    if not needle:
        return 0.0
    if needle in haystack:
        return 1.0
    if len(haystack) <= len(needle):
        return SequenceMatcher(None, needle, haystack, autojunk=False).ratio()
    matcher = SequenceMatcher(None, haystack, needle, autojunk=False)
    best = 0.0
    for block in matcher.get_matching_blocks():
        start = max(0, min(block.a - block.b, len(haystack) - len(needle)))
        window = haystack[start : start + len(needle)]
        best = max(best, SequenceMatcher(None, needle, window, autojunk=False).ratio())
        if best == 1.0:
            break
    return best


def quote_ratio(quote: str, text: str) -> float:
    haystack = normalize(text)
    fragments = [normalize(f) for f in _ELLIPSIS.split(quote)]
    fragments = [f for f in fragments if len(f) >= _MIN_FRAGMENT] or [normalize(quote)]
    total = sum(len(f) for f in fragments)
    if not total:
        return 0.0
    return sum(_partial_ratio(f, haystack) * len(f) for f in fragments) / total


def classify_ratio(ratio: float) -> str:
    if ratio >= VERIFIED_RATIO:
        return "verified"
    if ratio >= PARTIAL_RATIO:
        return "partial"
    return "unsupported"
