"""Place-name normalization for deterministic finding localization."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from app.application.address_search import normalize_street

_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
_DIRECTION = re.compile(
    r"\s+(?:Nordost|Nordwest|Südost|Südwest|Nord|Süd|Ost|West)\s*$", re.IGNORECASE
)
_HOUSE_NUMBER = re.compile(r"(?:^|\s)(?P<number>\d+[A-Za-z]?)\s*$")


def _tokens(text: str) -> tuple[str, ...]:
    normalized = []
    for token in _TOKEN.findall(text):
        value = normalize_street(token).replace("straße", "strasse")
        if value:
            normalized.append(value)
    return tuple(normalized)


def _term_key(term: str) -> tuple[str, ...]:
    return _tokens(term)


def _valid_term(term: str, minimum_length: int) -> bool:
    return bool(_term_key(term)) and len(normalize_street(term).replace("straße", "strasse")) >= (
        minimum_length
    )


def _dedupe(terms: Iterable[str], minimum_length: int = 4) -> list[str]:
    result = []
    seen = set()
    for term in terms:
        value = term.strip()
        key = _term_key(value)
        if not _valid_term(value, minimum_length) or key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def cell_terms(name: str | None, streets: Iterable[str] = ()) -> list[str]:
    candidates: list[str] = []
    if name:
        candidates.append(name)
        without_parentheses = re.sub(r"\s*\([^)]*\)", " ", name).strip()
        candidates.append(without_parentheses)
        for candidate in (name, without_parentheses):
            without_direction = _DIRECTION.sub("", candidate).strip()
            candidates.append(without_direction)
    candidates.extend(street for street in streets if street)
    return _dedupe(candidates)


def _address_parts(address: str | None) -> tuple[str, str | None]:
    if not address:
        return "", None
    street = address.split(",", 1)[0].strip()
    match = _HOUSE_NUMBER.search(street)
    if match is None:
        return street, None
    return street[: match.start()].strip(" ,"), match.group("number")


def address_street(address: str | None) -> str | None:
    street, _ = _address_parts(address)
    return street or None


def address_terms(address: str | None) -> list[str]:
    street, number = _address_parts(address)
    if not street or not number:
        return []
    return _dedupe([f"{street} {number}"], minimum_length=0)


def mentioned(text: str, terms: Sequence[str]) -> str | None:
    text_tokens = _tokens(text)
    for term in terms:
        term_tokens = _term_key(term)
        if not term_tokens or len(term_tokens) > len(text_tokens):
            continue
        width = len(term_tokens)
        if any(
            text_tokens[i : i + width] == term_tokens for i in range(len(text_tokens) - width + 1)
        ):
            return term
    return None
